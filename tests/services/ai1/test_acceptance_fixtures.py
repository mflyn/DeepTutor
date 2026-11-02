"""Keep explicit cross-checkout fixtures registered without running LMS or a model."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module,test_name",
    [
        ("lms_browser_acceptance", "test_actual_remote_browser"),
        (
            "lms_business_acceptance",
            "test_formal_lms_business_versions_permissions_and_settlement",
        ),
        (
            "lms_dual_restore_acceptance",
            "test_actual_dual_store_rollback_retains_mapping_reservation_and_slot",
        ),
        (
            "lms_recovery_acceptance",
            "test_formal_start_without_external_continuity_cannot_open_ready_endpoint",
        ),
    ],
)
def test_cross_checkout_fixture_rejects_missing_prerequisites(tmp_path, module, test_name):
    env = dict(os.environ)
    for name in ("AI1_LMS_ROOT", "AI1_NODE", "AI1_NODE_PATH"):
        env.pop(name, None)
    env.update(
        HOME=str(tmp_path),
        DEEPTUTOR_HOME=str(tmp_path / "deeptutor"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--setup-only",
            "-xq",
            f"tests/services/ai1/{module}.py::{test_name}",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Explicit cross-checkout prerequisite missing: AI1_LMS_ROOT" in output, output
    assert "fixture 'local' not found" not in output, output
