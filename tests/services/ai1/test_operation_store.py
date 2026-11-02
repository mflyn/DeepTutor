"""Persistent single-send ownership, using only synthetic text and temporary storage."""

import copy
import importlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def store_type():
    spec = importlib.util.find_spec("deeptutor.services.ai1_store")
    assert spec is not None, "AI-1 persistent operation store is not implemented"
    return importlib.import_module("deeptutor.services.ai1_store").OperationStore


@pytest.fixture
def payload():
    return {
        "schemaVersion": "ai1-provider-operation-v1",
        "requestHash": "a" * 64,
        "modelProfileVersion": "synthetic-model-v1",
        "pricingProfileVersion": "synthetic-price-v1",
        "messages": [{"role": "system", "content": "Synthetic"},
                     {"role": "user", "content": "206 * 30"}],
        "requestOptions": {"model": "synthetic", "maxInputTokens": 8192,
                           "maxOutputTokens": 4096, "thinking": "disabled",
                           "tools": [], "attemptLimit": 1},
        "reservedCeilingMicros": 184320,
        "sendNotAfter": "2030-01-01T00:00:00.000Z",
    }


ID = "11111111-1111-4111-8111-111111111111"


def test_accept_snapshot_replay_and_hash_conflict(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    original = copy.deepcopy(payload)
    op, created = store.accept(ID, payload)
    assert created and op["status"] == "accepted" and op["sendClaimed"] is False
    payload["messages"][1]["content"] = "changed caller object"
    assert store.snapshot(ID) == original
    reopened = cls(store.path)
    assert reopened.accept(ID, original) == (op, False)
    changed = {**original, "requestHash": "b" * 64}
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED"):
        reopened.accept(ID, changed)
    assert reopened.get(ID) == op


def test_concurrent_claim_has_one_durable_winner(tmp_path, payload):
    cls = store_type()
    path = tmp_path / "operations.sqlite"
    store = cls(path, initialize=True)
    store.accept(ID, payload)
    def claim(_):
        return cls(path).claim(ID, lambda snapshot: None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [value for value in pool.map(claim, range(16)) if value]
    assert len(winners) == 1
    assert cls(path).get(ID)["status"] == "sending"
    assert cls(path).get(ID)["sendClaimed"] is True
    assert cls(path).claim(ID, lambda snapshot: None) is None


def test_rejected_claim_is_atomic_and_never_sent(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    observed = []
    def expired(snapshot):
        observed.append(snapshot)
        return "PREVIEW_STALE"
    assert store.claim(ID, expired) is None
    assert observed == [payload]
    op = store.get(ID)
    assert (op["status"], op["error"], op["sendClaimed"]) == (
        "failed_not_sent", "PREVIEW_STALE", False)
    assert store.claim(ID, lambda snapshot: None) is None


def test_policy_exception_rolls_back_without_claim(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    before, _ = store.accept(ID, payload)
    def broken(snapshot):
        raise RuntimeError("synthetic configuration unavailable")
    with pytest.raises(RuntimeError):
        store.claim(ID, broken)
    assert store.get(ID) == before


def test_unknown_cannot_be_reclaimed_and_first_terminal_is_immutable(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    owner = store.claim(ID, lambda snapshot: None)
    assert not store.finish(ID, "wrong-owner", "unknown")
    assert store.finish(ID, owner, "unknown")
    assert cls(store.path).claim(ID, lambda snapshot: None) is None
    result = {"text": "6180", "contentHash": "b" * 64}
    assert store.finish(ID, owner, "succeeded", result=result, usage=None)
    terminal = store.get(ID)
    assert not store.finish(ID, owner, "failed_sent")
    assert not store.finish(ID, owner, "unknown")
    assert cls(store.path).get(ID) == terminal
    assert store.accept(ID, payload) == (terminal, False)


def test_missing_database_is_not_silently_recreated(tmp_path):
    cls = store_type()
    with pytest.raises(FileNotFoundError):
        cls(tmp_path / "missing.sqlite")
    assert not (tmp_path / "missing.sqlite").exists()


def test_read_does_not_write_or_reset_sending(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    store.claim(ID, lambda snapshot: None)
    before = store.path.read_bytes()
    assert store.get(ID)["status"] == "sending"
    assert store.snapshot(ID) == payload
    assert store.get("22222222-2222-4222-8222-222222222222") is None
    assert store.path.read_bytes() == before


def test_private_files_and_symlink_rejection(tmp_path):
    cls = store_type()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = cls(directory / "operations.sqlite", initialize=True)
    assert store.path.stat().st_mode & 0o777 == 0o600
    link = directory / "linked.sqlite"
    link.symlink_to(store.path)
    with pytest.raises(ValueError, match="AI_STORE_UNSAFE"):
        cls(link)


def test_process_exit_after_claim_preserves_no_resend(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    script = """
import os, sys
from deeptutor.services.ai1_store import OperationStore
s = OperationStore(sys.argv[1])
assert s.claim(sys.argv[2], lambda snapshot: None)
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(store.path), ID],
                            cwd=Path(__file__).resolve().parents[3],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 23, result.stderr
    reopened = cls(store.path)
    before = reopened.get(ID)
    assert before["status"] == "sending" and before["sendClaimed"] is True
    assert reopened.claim(ID, lambda snapshot: None) is None
    assert reopened.accept(ID, payload) == (before, False)


def test_claim_checks_time_after_waiting_for_transaction(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    clock = [0]
    checked = threading.Event()
    reached_begin = threading.Event()
    class ObservedStore(cls):
        def _connection(self, **kwargs):
            from contextlib import contextmanager
            @contextmanager
            def connect():
                with super(ObservedStore, self)._connection(**kwargs) as db:
                    db.set_trace_callback(lambda sql: reached_begin.set() if sql == "BEGIN IMMEDIATE" else None)
                    yield db
            return connect()
    delayed = ObservedStore(store.path)
    def policy(snapshot):
        checked.set()
        return "PREVIEW_STALE" if clock[0] >= 1 else None
    with sqlite3.connect(store.path) as lock:
        lock.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delayed.claim, ID, policy)
            try:
                assert reached_begin.wait(2)
                assert not checked.is_set()
                clock[0] = 1
            finally:
                lock.rollback()
            assert future.result(timeout=3) is None
    assert checked.is_set()
    assert store.get(ID)["status"] == "failed_not_sent"


def test_same_hash_different_snapshot_is_rejected(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    before, _ = store.accept(ID, payload)
    changed = copy.deepcopy(payload)
    changed["messages"][1]["content"] = "altered with unchanged hash"
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED"):
        store.accept(ID, changed)
    assert store.snapshot(ID) == payload
    assert store.get(ID) == before


def test_terminal_and_claim_race_preserves_first_audit(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    owner = store.claim(ID, lambda snapshot: None)
    gate = threading.Barrier(2)
    def finish(status):
        other = cls(store.path)
        gate.wait(timeout=3)
        return other.finish(ID, owner, status,
                            result={"text": "6180", "contentHash": "b" * 64} if status == "succeeded" else None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ["succeeded", "failed_sent"]))
    assert sum(results) == 1
    before = store.get(ID)
    assert before["status"] in {"succeeded", "failed_sent"}
    assert not store.finish(ID, owner, "unknown")
    assert store.get(ID) == before


def test_database_constraints_reject_false_unsent_claim(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    store.claim(ID, lambda snapshot: None)
    before = store.get(ID)
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE operations SET status='failed_not_sent' WHERE id=?", (ID,))
    assert store.get(ID) == before


def test_lost_claim_commit_acknowledgement_never_reissues_owner(tmp_path, payload):
    from contextlib import contextmanager
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    class LostAcknowledgement(cls):
        @contextmanager
        def _write(self):
            with super()._write() as db:
                yield db
            raise OSError("synthetic commit acknowledgement lost")
    with pytest.raises(OSError, match="acknowledgement lost"):
        LostAcknowledgement(store.path).claim(ID, lambda snapshot: None)
    before = store.get(ID)
    assert before["status"] == "sending"
    assert before["sendClaimed"] is True
    assert store.claim(ID, lambda snapshot: None) is None
    assert store.get(ID) == before


def test_running_store_rejects_parent_permission_change_before_claim(tmp_path, payload):
    cls = store_type()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = cls(directory / "operations.sqlite", initialize=True)
    before, _ = store.accept(ID, payload)
    directory.chmod(0o755)
    try:
        with pytest.raises(ValueError, match="AI_STORE_UNSAFE"):
            store.claim(ID, lambda snapshot: None)
    finally:
        directory.chmod(0o700)
    assert store.get(ID) == before


def test_reject_and_claim_race_only_one_wins(tmp_path, payload):
    cls = store_type()
    store = cls(tmp_path / "operations.sqlite", initialize=True)
    store.accept(ID, payload)
    gate = threading.Barrier(2)
    def act(reject):
        other = cls(store.path)
        gate.wait(timeout=3)
        return other.reject(ID, "PREVIEW_STALE") if reject else other.claim(ID, lambda _: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rejected, owner = list(pool.map(act, [True, False]))
    assert int(rejected) + int(owner is not None) == 1
    operation = store.get(ID)
    assert operation["status"] == ("failed_not_sent" if rejected else "sending")
    assert operation["sendClaimed"] is (not rejected)
    assert not store.reject(ID, "VALIDATION_ERROR")
    assert store.get(ID) == operation
