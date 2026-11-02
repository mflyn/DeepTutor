"""Explicit cross-checkout browser acceptance; real services, synthetic SDK transport."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

from tests.services.ai1.lms_acceptance import PROFILE, calls, local, serve
from tests.services.ai1.lms_business_acceptance import run_lms


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_native_browser_group_forced_close(tmp_path, engine):
    # Preserve Playwright's process-group contract, including its forced-close path.
    script = """
const { %s: browserType } = require('@playwright/test');
(async () => {
  const server = await browserType.launchServer({ headless: true });
  const pid = server.process().pid;
  process.kill(-pid, 0);
  console.log('BROWSER_PID=' + pid);
  await server.kill();
  console.log('NATIVE_KILL_COMPLETE');
})().catch(error => { console.error(error); process.exitCode = 1; });
""" % engine
    log = tmp_path / "forced-close.log"
    result = run_lms([os.environ["AI1_NODE"], "-e", script], cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": os.environ["HOME"], "NODE_PATH": os.environ["AI1_NODE_PATH"]},
        log_path=log, timeout=30)
    assert result == 0 and "NATIVE_KILL_COMPLETE" in log.read_text()


@pytest.mark.parametrize("source,fault", [("task", "none"), ("mistake", "none"), ("task", "drop-response")])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_actual_remote_browser(local, source, fault, engine):
    root, store = local
    lms = Path(os.environ["AI1_LMS_ROOT"])
    modules = Path(os.environ["AI1_NODE_PATH"])
    mongo, build = Path(os.environ["AI1_MONGO_BINARY"]), Path(os.environ["T2_WEB_BUILD"])
    assert mongo.is_absolute() and mongo.is_file()
    assert build.is_absolute() and (build / "index.html").is_file()
    now = time.time_ns() // 1000000
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    profile = copy.deepcopy(PROFILE)
    profile.update({"checkedAt": iso(now - 60000), "expiresAt": iso(now + 3600000)})
    config = root / "playwright.config.cjs"
    config.write_text("const base = require(" + json.dumps(str(lms / "playwright.t2.config.js")) + ");\n"
        + "module.exports={...base,testDir:" + json.dumps(str(lms / "tests/e2e/t2"))
        + ",globalTimeout:100000,testMatch:'ai1ActualRemote.acceptance.js',outputDir:" + json.dumps(str(root / "screenshots"))
        + ",use:{...base.use,browserName:" + json.dumps(engine)
        + ",viewport:{width:820,height:1180}}};\n")
    with serve(root, fault=fault, profile=profile, now=now) as (origin, _):
        env = {"PATH": os.defpath, "HOME": os.environ["HOME"], "NODE_ENV": "test", "NODE_PATH": str(modules),
            "MONGOMS_SYSTEM_BINARY": str(mongo), "MONGOMS_RUNTIME_DOWNLOAD": "false", "T2_WEB_BUILD": str(build),
            "AI1_ACTUAL_REMOTE_ORIGIN": origin, "AI1_ACTUAL_REMOTE_PROFILE": json.dumps(profile),
            "AI1_ACTUAL_REMOTE_FAULT": fault, "AI1_ACTUAL_SOURCE": source,
            "AI1_ACTUAL_REQUESTS": str(root / "requests.jsonl"), "AI1_ACTUAL_RESULT": str(root / "browser-result.json")}
        code = run_lms([os.environ["AI1_NODE"], str(modules / "@playwright/test/cli.js"), "test", "--config=" + str(config)],
                       cwd=lms, env=env, log_path=root / "playwright.log")
        assert code == 0, (root / "playwright.log").read_text()
        result = json.loads((root / "browser-result.json").read_text())
        remote = store.get(result["remoteOperationId"])
        assert remote["status"] == "succeeded" and remote["requestHash"] == result["requestHash"]
        assert len(calls(root)) == 1
        snapshot = store.snapshot(result["remoteOperationId"])
        assert snapshot["messages"] == result["previewMessages"] and snapshot["requestOptions"] == result["previewOptions"]
        assert len(list((root / "screenshots").rglob("approved.png"))) == 1
