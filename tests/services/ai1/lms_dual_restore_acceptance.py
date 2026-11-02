"""Actual two-store synthetic rollback; never a production recovery procedure."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest
import psutil

from tests.services.ai1.lms_acceptance import PROFILE, calls, client, local, serve
from tests.services.ai1.lms_business_acceptance import run_lms


def test_restore_driver_failure_preserves_error_and_reaps_child(tmp_path):
    def failed():
        raise RuntimeError("SYNTHETIC_CONTROL_FAILURE")
    log = tmp_path / "control-failure.log"
    with pytest.raises(RuntimeError, match="SYNTHETIC_CONTROL_FAILURE"):
        run_lms([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path,
                env={"PATH": os.defpath}, log_path=log, on_poll=failed)
    assert "SURVIVORS=0" in log.read_text()


def test_control_callback_crossing_deadline_cannot_report_normal_child_exit(tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    script = ("import os,time; from pathlib import Path; "
              f"Path({str(ready)!r}).write_text(str(os.getpid())); "
              f"p=Path({str(release)!r})\nwhile not p.exists(): time.sleep(0.005)\n")
    completed = []
    def crossed():
        deadline = time.monotonic() + 2
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        process = psutil.Process(int(ready.read_text()))
        release.touch()
        completed.append(process.wait(timeout=2))
        time.sleep(0.06)
    log = tmp_path / "control-deadline.log"
    with pytest.raises(subprocess.TimeoutExpired):
        run_lms([sys.executable, "-c", script], cwd=tmp_path, env={"PATH": os.defpath},
                log_path=log, timeout=0.05, on_poll=crossed)
    assert completed == [0]
    assert "TIMEOUT=True SURVIVORS=0" in log.read_text()


@pytest.mark.parametrize("historical", ["accepted", "sending", "succeeded", "failed_not_sent", "empty"])
def test_actual_dual_store_rollback_retains_mapping_reservation_and_slot(local, historical):
    root, store = local
    mongo = Path(os.environ["AI1_MONGO_BINARY"])
    assert mongo.is_absolute() and mongo.is_file()
    lms, modules = Path(os.environ["AI1_LMS_ROOT"]), Path(os.environ["AI1_NODE_PATH"])
    now = time.time_ns() // 1000000
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    profile = copy.deepcopy(PROFILE)
    profile.update({"checkedAt": iso(now - 60000), "expiresAt": iso(now + 3600000)})
    current = None
    state = {}

    def stop():
        nonlocal current
        if current is not None:
            owned, current = current, None
            owned.__exit__(None, None, None)

    def start(mode):
        nonlocal current
        manager = serve(root, mode=mode, profile=profile, now=now)
        origin, _ = manager.__enter__()
        current = manager
        return origin

    def reply(phase, origin):
        temporary = root / (phase + ".response.tmp")
        temporary.write_text(json.dumps({"origin": origin}))
        temporary.chmod(0o600)
        temporary.rename(root / (phase + ".response.json"))

    def poll():
        prepare = root / "prepare.request.json"
        restore = root / "restore.request.json"
        if "id" not in state and prepare.exists():
            request = json.loads(prepare.read_text())
            operation_id, payload = request["id"], request["payload"]
            state.update({"id": operation_id, "payload": payload})
            seed_profile = copy.deepcopy(profile)
            if historical == "failed_not_sent":
                seed_profile["pricingProfileVersion"] += "-revoked"
            if historical != "empty":
                fault = {"accepted": "after-accept", "sending": "after-provider"}.get(historical, "none")
                with serve(root, fault=fault, profile=seed_profile, now=now) as (origin, process):
                    result = client(origin, payload=payload, operation_id=operation_id)
                    if fault != "none":
                        assert result["error"] == "AI_REMOTE_UNAVAILABLE"
                        assert process.wait(timeout=3) == 73
                    else:
                        assert result["operation"]["status"] == historical
                assert store.get(operation_id)["status"] == historical
                assert store.get(operation_id)["requestHash"] == payload["requestHash"]
            else:
                assert store.get(operation_id) is None
            state["row"] = store.get(operation_id)
            state["snapshot"] = store.path.read_bytes()
            # Advance the actual remote store after the snapshot, without a model call.
            advance_id = operation_id if historical in {"accepted", "empty"} else str(uuid4())
            with serve(root, profile=profile, now=now) as (origin, _):
                assert client(origin, payload=payload, operation_id=advance_id)["operation"]["status"] == "succeeded"
            state["attempts"] = 2 if historical in {"sending", "succeeded"} else 1
            assert len(calls(root)) == state["attempts"]
            reply("prepare", start("query-only"))
        if "restored" not in state and restore.exists():
            assert json.loads(restore.read_text()) == {"id": state["id"]}
            stop()
            store.path.write_bytes(state["snapshot"])
            assert store.get(state["id"]) == state["row"]
            state["request_count"] = len((root / "requests.jsonl").read_text().splitlines())
            state["restored"] = True
            reply("restore", start("store-isolated"))

    env = {"PATH": os.defpath, "HOME": str(root / "home"), "NODE_ENV": "test", "NODE_PATH": str(modules),
           "MONGOMS_SYSTEM_BINARY": str(mongo), "MONGOMS_RUNTIME_DOWNLOAD": "false",
           "AI1_RESTORE_CONTROL": str(root), "AI1_RESTORE_HISTORICAL": historical,
           "AI1_ACTUAL_REMOTE_PROFILE": json.dumps(profile)}
    try:
        code = run_lms([os.environ["AI1_NODE"], str(modules / "jest/bin/jest.js"),
            "--config=backend/tests/t2/jest.config.js", "--runInBand", "--coverage=false",
            "--testMatch=**/ai1RestoreRemote.acceptance.js", "--runTestsByPath",
            "backend/tests/t2/ai1RestoreRemote.acceptance.js"], cwd=lms, env=env,
            log_path=root / "lms-jest.log", on_poll=poll)
        assert code == 0, (root / "lms-jest.log").read_text()
        assert state["restored"] is True
        report = json.loads((root / "result.json").read_text())
        assert report == {"id": state["id"], "hash": state["payload"]["requestHash"],
                          "historical": historical, "noLocalChange": True}
        assert store.path.read_bytes() == state["snapshot"]
        assert store.get(state["id"]) == state["row"]
        assert len(calls(root)) == state["attempts"]
        requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
        isolated = requests[state["request_count"]:]
        assert [r["method"] for r in isolated] == ["GET", "PUT", "GET", "PUT"]
        assert isolated[0]["id"] == isolated[1]["id"] == state["id"]
        assert isolated[2]["id"] == isolated[3]["id"] != state["id"]
    finally:
        stop()
