"""Explicit synthetic backup-isolation acceptance against the actual main/client.

Only this test's private SQLite bytes are restored, with both old processes
stopped. The external isolation decision is explicit, never inferred from rows.
"""

import copy
from uuid import uuid4

import pytest

from deeptutor.services.ai1_contract import request_hash
from tests.services.ai1.lms_acceptance import ID, NOW, PAYLOAD, calls, client, serve
from tests.services.ai1.lms_acceptance import local as local


def assert_isolated(root, store, saved, expected_calls):
    before = store.get(ID)
    with serve(root, mode="store-isolated") as (origin, _):
        for method, operation_id in [
            ("get", ID),
            ("put", ID),
            ("get", str(uuid4())),
            ("put", str(uuid4())),
        ]:
            assert client(origin, method, operation_id=operation_id) == {
                "error": "AI_STORE_ISOLATED",
                "status": 503,
            }
        assert store.path.read_bytes() == saved
        assert store.get(ID) == before
        assert len(calls(root)) == expected_calls
    assert store.path.read_bytes() == saved and len(calls(root)) == expected_calls


def test_old_accepted_backup_after_real_send_cannot_consume_again(local):
    root, store = local
    with serve(root, fault="after-accept") as (origin, process):
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
        assert process.wait(timeout=3) == 73
    accepted = store.get(ID)
    assert accepted["status"] == "accepted" and accepted["sendClaimed"] is False
    saved = store.path.read_bytes()
    with serve(root) as (origin, _):
        assert client(origin, "get")["operation"] == accepted and calls(root) == []
        assert client(origin)["operation"]["status"] == "succeeded"
    assert len(calls(root)) == 1 and store.get(ID)["sendClaimed"] is True
    store.path.write_bytes(saved)
    assert store.get(ID) == accepted
    assert_isolated(root, store, saved, 1)


@pytest.mark.parametrize("historical", ["sending", "succeeded", "failed_not_sent", "empty"])
def test_restored_old_terminal_or_empty_store_never_proves_continuity(local, historical):
    root, store = local
    expected_calls = 0
    if historical == "sending":
        with serve(root, fault="after-provider") as (origin, process):
            assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
            assert process.wait(timeout=3) == 73
        assert store.get(ID)["status"] == "sending" and store.get(ID)["sendClaimed"] is True
        expected_calls = 1
    elif historical == "succeeded":
        with serve(root) as (origin, _):
            assert client(origin)["operation"]["status"] == "succeeded"
        expected_calls = 1
    elif historical == "failed_not_sent":
        stale = copy.deepcopy(PAYLOAD)
        stale["sendNotAfter"] = "2030-01-01T00:00:00.000Z"
        stale["requestHash"] = request_hash(stale)
        with serve(root) as (origin, _):
            result = client(origin, payload=stale)["operation"]
            assert result["status"] == "failed_not_sent" and result["sendClaimed"] is False
    else:
        assert store.get(ID) is None
    saved = store.path.read_bytes()
    # A distinct real operation after the snapshot proves the restored file is old.
    with serve(root) as (origin, _):
        assert client(origin, operation_id=str(uuid4()))["operation"]["status"] == "succeeded"
    expected_calls += 1
    assert len(calls(root)) == expected_calls
    store.path.write_bytes(saved)
    assert_isolated(root, store, saved, expected_calls)


def test_historical_success_query_after_quote_expiry_is_exact_and_never_sends(local):
    root, store = local
    with serve(root) as (origin, _):
        original = client(origin)["operation"]
    assert original["status"] == "succeeded" and original["sendClaimed"] is True
    assert original["result"]["text"] == "6180"
    assert original["usage"] == {"inputTokens": 100, "outputTokens": 50}
    assert store.get(ID) == original
    before = store.path.read_bytes()
    with serve(root, mode="query-only", now=NOW + 7200000) as (origin, _):
        assert client(origin, "get")["operation"] == original
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
    assert store.path.read_bytes() == before and len(calls(root)) == 1


@pytest.mark.parametrize("mode", ["enabled", "query-only"])
@pytest.mark.parametrize("continuity", [False, None], ids=["false", "missing"])
def test_formal_start_without_external_continuity_cannot_open_ready_endpoint(
    local, mode, continuity
):
    root, store = local
    with serve(root, fault="after-accept") as (origin, process):
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
        assert process.wait(timeout=3) == 73
    before = store.path.read_bytes()
    logs = set(root.glob("server-*.log"))
    with pytest.raises(AssertionError, match="Formal main exited before readiness"):
        with serve(root, mode=mode, store_continuous=continuity):
            pytest.fail("Unknown continuity must fail actual main startup")
    failure_logs = set(root.glob("server-*.log")) - logs
    assert len(failure_logs) == 1
    failure = next(iter(failure_logs)).read_text()
    assert "deeptutor.services.ai1_contract.ContractError: AI_PROFILE_UNAVAILABLE" in failure
    assert "Application startup failed" in failure
    assert store.path.read_bytes() == before and calls(root) == []
    # Same mode/store/profile/credentials; only the external declaration differs.
    with serve(root, mode=mode, store_continuous=True) as (origin, _):
        assert client(origin, "get")["operation"] == store.get(ID)
        assert store.get(ID)["status"] == "accepted"
    assert store.path.read_bytes() == before and calls(root) == []
