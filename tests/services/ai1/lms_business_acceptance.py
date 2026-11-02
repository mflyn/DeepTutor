"""Explicit real LMS service acceptance; driven with synthetic remote/DB resources."""

import copy
from datetime import datetime, timezone
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

import pytest
import psutil

from tests.services.ai1.lms_acceptance import PROFILE, calls, local, serve


def run_lms(args, *, cwd, env, log_path, timeout=120, on_poll=None):
    """Keep partial output; contain Node helpers and reap the runner's private group."""
    observed = {}
    process = None
    timed_out = False
    # NODE_OPTIONS paths containing spaces require quotes for Node's own parser.
    env = {**env, "NODE_OPTIONS": '--require="' + str(Path(__file__).with_name("node_owned_group.cjs")) + '"'}
    with log_path.open("w") as output:
        os.chmod(log_path, 0o600)
        try:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            parent = psutil.Process(process.pid)
            observed[parent.pid] = parent
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                for owned in list(observed.values()):
                    try:
                        if owned.is_running():
                            for child in owned.children(recursive=True):
                                observed.setdefault(child.pid, child)
                    except psutil.NoSuchProcess:
                        pass
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if on_poll is not None:
                    on_poll()
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                time.sleep(0.05)
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # psutil signals check PID reuse; never signal processes by name or broad group.
            for owned in observed.values():
                try:
                    owned.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(list(observed.values()), timeout=3)
            if process is not None:
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        # Darwin may retain a disappearing orphan group briefly.
                        # This is not success: require ESRCH within the same bound.
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
            for owned in alive:
                try:
                    owned.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=3)
            if process is not None:
                process.wait(timeout=3)
                # A group can briefly contain reparented zombies before init reaps them.
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    assert time.monotonic() < deadline, "Owned process group did not exit"
                    time.sleep(0.05)
            output.write(f"\nEXIT={process.returncode if process else 'NOT_STARTED'} TIMEOUT={timed_out} SURVIVORS={len(alive)}\n")
            output.flush()
            os.fsync(output.fileno())
            assert not alive, "Owned test descendants did not exit"
    if timed_out:
        raise subprocess.TimeoutExpired(args, timeout)
    return process.returncode


def test_lms_timeout_preserves_output_and_stops_detached_descendant(tmp_path):
    log = tmp_path / "timeout.log"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent = ("import subprocess,sys,time; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
              "print('DETACHED_PID='+str(p.pid),flush=True); time.sleep(60)")
    with pytest.raises(subprocess.TimeoutExpired):
        run_lms([sys.executable, "-c", parent], cwd=tmp_path, env={"PATH": os.defpath}, log_path=log, timeout=1)
    text = log.read_text()
    pid = int(text.split("DETACHED_PID=")[1].splitlines()[0])
    assert not psutil.pid_exists(pid)
    assert "TIMEOUT=True SURVIVORS=0" in text


def test_lms_failure_preserves_nonzero_and_partial_output(tmp_path):
    log = tmp_path / "failed.log"
    result = run_lms([sys.executable, "-c", "print('PERMANENT_FAILURE',flush=True); raise SystemExit(17)"],
                     cwd=tmp_path, env={"PATH": os.defpath}, log_path=log)
    assert result == 17
    assert "PERMANENT_FAILURE" in log.read_text() and "EXIT=17 TIMEOUT=False SURVIVORS=0" in log.read_text()


def test_lms_immediate_node_exit_cannot_leave_detached_helper(tmp_path):
    log = tmp_path / "immediate.log"
    source = ("const cp=require('node:child_process');"
              "const p=cp.spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{detached:true,stdio:'ignore'});"
              "console.log('HELPER_PID='+p.pid);p.unref();process.exit(17)")
    code = run_lms([os.environ["AI1_NODE"], "-e", source], cwd=tmp_path, env={"PATH": os.defpath}, log_path=log)
    assert code == 17
    pid = int(log.read_text().split("HELPER_PID=")[1].splitlines()[0])
    assert not psutil.pid_exists(pid)


@pytest.mark.parametrize("source,fault", [("task", "none"), ("mistake", "none"), ("task", "drop-response")])
def test_formal_lms_business_versions_permissions_and_settlement(local, source, fault):
    root, store = local
    mongo = Path(os.environ["AI1_MONGO_BINARY"])
    assert mongo.is_absolute() and mongo.is_file(), "Existing local Mongo binary required; no download"
    lms = Path(os.environ["AI1_LMS_ROOT"])
    node_modules = Path(os.environ["AI1_NODE_PATH"])
    profile = copy.deepcopy(PROFILE)
    now = time.time_ns() // 1000000
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    profile.update({"checkedAt": iso(now - 60000), "expiresAt": iso(now + 3600000)})
    with serve(root, fault=fault, profile=profile, now=now) as (origin, _):
        env = {"PATH": os.defpath, "HOME": str(root / "home"), "NODE_ENV": "test", "NODE_PATH": str(node_modules),
               "MONGOMS_SYSTEM_BINARY": str(mongo), "MONGOMS_RUNTIME_DOWNLOAD": "false",
               "AI1_ACTUAL_REMOTE_ORIGIN": origin, "AI1_ACTUAL_REMOTE_PROFILE": json.dumps(profile),
               "AI1_ACTUAL_REMOTE_FAULT": fault, "AI1_ACTUAL_SOURCE": source,
               "AI1_ACTUAL_REQUESTS": str(root / "requests.jsonl"),
               "AI1_ACTUAL_RESULT": str(root / "business-result.json")}
        code = run_lms([os.environ["AI1_NODE"], str(node_modules / "jest/bin/jest.js"),
            "--config=backend/tests/t2/jest.config.js", "--runInBand", "--coverage=false",
            "--testMatch=**/ai1ActualRemote.acceptance.js", "--runTestsByPath",
            "backend/tests/t2/ai1ActualRemote.acceptance.js"], cwd=lms, env=env, log_path=root / "lms-jest.log")
        assert code == 0, (root / "lms-jest.log").read_text()
        report = json.loads((root / "business-result.json").read_text())
        remote = store.get(report["remoteOperationId"])
        assert remote["status"] == "succeeded" and remote["requestHash"] == report["remoteRequestHash"]
        assert remote["usage"] == {"inputTokens": 100, "outputTokens": 50}
        assert remote["result"]["text"] == "6180" and len(calls(root)) == 1
        snapshot = store.snapshot(report["remoteOperationId"])
        assert snapshot["messages"] == report["previewMessages"] and snapshot["requestOptions"] == report["previewOptions"]
        assert all(value not in json.dumps(snapshot) for value in report["privateValues"])
        assert report["settledMicros"] == 200 and report["outstandingMicros"] == 0
        assert report["artifactCount"] == 2 and report["reviewedBy"] == report["coparentId"]
