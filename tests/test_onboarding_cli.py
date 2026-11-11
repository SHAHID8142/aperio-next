"""Onboarding CLI: protocol behavior, approvals, and backward compatibility."""

from __future__ import annotations

import json

import pytest

from aperio.onboarding_session import read_session
from aperio.project import init_project


@pytest.fixture
def ws(project_dir):
    project_dir.mkdir()
    return project_dir


def _json(out):
    return json.loads(out)


def test_start_on_empty_workspace_asks_once(run_cli, ws):
    code, out, err = run_cli("onboarding-start", str(ws))
    assert code == 0, err
    assert "Would you like to initialize a new project here?" in out
    assert sorted(p.name for p in ws.iterdir()) == []  # no mutation


def test_start_json_protocol(run_cli, ws):
    code, out, err = run_cli("onboarding-start", str(ws), "--json")
    assert code == 0, err
    doc = _json(out)
    assert doc["format"] == "aperio-onboarding-protocol"
    assert doc["classification"] == "empty_new"
    assert doc["action_required"] == "confirm_initialization"
    assert len(doc["choices"]) == 2


def test_next_is_canonical_read_only_onboarding_entry(run_cli, ws):
    before = sorted(p.name for p in ws.iterdir())
    code, out, err = run_cli("next", str(ws), "--json")
    assert code == 0, err
    doc = _json(out)
    assert doc["format"] == "aperio-onboarding-protocol"
    assert doc["classification"] == "empty_new"
    assert doc["action_required"] == "confirm_initialization"
    assert sorted(p.name for p in ws.iterdir()) == before


def test_next_routes_through_same_onboarding_facade(run_cli, ws):
    code, via_next, err = run_cli("next", str(ws), "--json")
    assert code == 0, err
    code, via_explicit_command, err = run_cli(
        "onboarding-start", str(ws), "--json"
    )
    assert code == 0, err
    assert _json(via_next) == _json(via_explicit_command)


def test_next_resumes_existing_onboarding_session(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    before = (ws / ".aperio/onboarding/session.json").read_bytes()
    code, out, err = run_cli("next", str(ws), "--json")
    assert code == 0, err
    doc = _json(out)
    assert doc["action_required"] == "answer_question"
    assert doc["question"]["id"] == "project.category"
    assert (ws / ".aperio/onboarding/session.json").read_bytes() == before


def test_decline_changes_nothing(run_cli, ws):
    run_cli("onboarding-start", str(ws))
    code, out, _ = run_cli("onboarding-decline", str(ws), "--json")
    assert code == 0
    assert _json(out)["action_required"] == "none"
    assert sorted(p.name for p in ws.iterdir()) == []


def test_confirm_init_requires_explicit_approval(run_cli, ws):
    code, out, _ = run_cli("onboarding-confirm-init", str(ws), "--json")
    assert code == 0
    doc = _json(out)
    assert doc["action_required"] == "confirm_initialization"
    assert "Nothing was modified" in doc["prompt"]
    assert sorted(p.name for p in ws.iterdir()) == []


def test_confirm_init_creates_foundation_after_approval(run_cli, ws):
    code, out, err = run_cli("onboarding-confirm-init", str(ws), "--approve", "--json")
    assert code == 0, err
    doc = _json(out)
    assert doc["action_required"] == "answer_question"
    assert doc["question"]["id"] == "project.category"
    assert (ws / ".aperio/state.json").is_file()
    assert (ws / ".aperio/onboarding/session.json").is_file()


def test_one_question_per_turn_through_cli(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, _ = run_cli("onboarding-answer", str(ws), "project.category",
                           "--answer", "Research", "--json")
    assert code == 0
    doc = _json(out)
    assert doc["question"]["id"] == "foundation.goal"
    assert doc["question"] is not None


def test_answer_persists_and_next_question_selected(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    run_cli("onboarding-answer", str(ws), "project.category", "--answer", "Research")
    code, out, _ = run_cli("onboarding-answer", str(ws), "foundation.goal",
                           "--answer", "Evaluate soil samples", "--json")
    doc = _json(out)
    session = read_session(ws / ".aperio")
    assert session["answers"]["foundation.goal"]["answer"] == "Evaluate soil samples"
    assert session["project_category"] == "research"
    # Adaptive: research questions follow.
    remaining = [q["id"] for q in [doc["question"]] if q]
    assert "research.question" in [doc["question"]["id"]] or doc["action_required"] != "answer_question"


def test_unsure_answer_records_unresolved_via_cli(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, _ = run_cli("onboarding-answer", str(ws), "foundation.goal",
                           "--answer", "Not sure yet", "--json")
    assert code == 0
    session = read_session(ws / ".aperio")
    assert session["answers"]["foundation.goal"]["kind"] == "unsure"
    assert any(u["topic"] == "question:foundation.goal" for u in session["unresolved"])


def test_revise_via_cli(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    run_cli("onboarding-answer", str(ws), "project.category", "--answer", "Research")
    run_cli("onboarding-revise", str(ws), "project.category", "--answer", "Writing")
    session = read_session(ws / ".aperio")
    assert session["project_category"] == "writing"
    assert session["answers"]["project.category"]["revision"] == 2


def test_pause_and_cancel(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, _ = run_cli("onboarding-pause", str(ws), "--json")
    assert "resume any time" in _json(out)["prompt"]
    code, out, _ = run_cli("onboarding-cancel", str(ws), "--json")
    doc = _json(out)
    assert doc["action_required"] == "confirm_cancellation"
    code, out, _ = run_cli("onboarding-cancel", str(ws), "--confirm", "--json")
    session = read_session(ws / ".aperio")
    assert session["cancelled"] is True
    assert session["stage"] == "cancelled"


def test_status_is_read_only(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    before = (ws / ".aperio/onboarding/session.json").read_bytes()
    code, out, _ = run_cli("onboarding-status", str(ws), "--json")
    assert code == 0
    assert _json(out)["stage"] == "interview"
    assert (ws / ".aperio/onboarding/session.json").read_bytes() == before


def test_reconstruct_is_read_only_and_records(run_cli, ws):
    (ws / "README.md").write_text("# demo\n- [ ] task one\n")
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in ws.rglob("*") if "onboarding" not in str(p))
    code, out, _ = run_cli("onboarding-reconstruct", str(ws), "--json")
    assert code == 0
    payload = _json(out)
    assert payload["recorded_in_session"] is True
    assert len(payload["pending_tasks"]) == 1
    after = sorted((p.name, p.stat().st_mtime_ns) for p in ws.rglob("*") if "onboarding" not in str(p))
    assert before == after


def test_reconstruct_without_session_still_works(run_cli, tmp_path):
    (tmp_path / "README.md").write_text("# raw project\n")
    code, out, _ = run_cli("onboarding-reconstruct", str(tmp_path), "--json")
    assert code == 0
    payload = _json(out)
    assert payload["recorded_in_session"] is False


def test_standardization_flow_through_cli(run_cli, ws):
    (ws / "README.md").write_text("# app\nlanguage: Python\n")
    (ws / "PRD.md").write_text("language: Go\n")
    init_project(str(ws))
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, _ = run_cli("onboarding-propose-standardization", str(ws), "--json")
    assert code == 0
    doc = _json(out)
    proposal_id = doc["proposal_id"]
    assert doc["action_required"] == "review_proposal"
    # Without --approve: nothing applied.
    code, out, _ = run_cli("onboarding-apply-standardization", str(ws),
                           "--proposal", proposal_id, "--json")
    doc = _json(out)
    assert doc["action_required"] == "approve_standardization"
    assert not (ws / ".aperio/onboarding/standardization-mapping.json").exists()
    # With --approve: applied, originals intact.
    readme_before = (ws / "README.md").read_bytes()
    code, out, err = run_cli("onboarding-apply-standardization", str(ws),
                             "--proposal", proposal_id, "--approve", "--json")
    assert code == 0, err
    doc = _json(out)
    assert doc["verification"]["all_ok"] is True
    assert (ws / ".aperio/onboarding/standardization-mapping.json").is_file()
    assert (ws / "README.md").read_bytes() == readme_before


def test_discover_tools_json_and_probe(run_cli, ws):
    code, out, _ = run_cli("onboarding-discover-tools", str(ws), "--json")
    assert code == 0
    payload = _json(out)
    assert payload["format"] == "aperio-onboarding-tools"
    assert payload["user_config_scanned"] is False
    code, out, _ = run_cli("onboarding-discover-tools", str(ws), "--probe", "python3", "--json")
    payload = _json(out)
    probed = next(t for t in payload["executables"] if t["tool_id"] == "python3")
    assert probed["status"] == "verified_by_probe"


def test_tool_plan_and_install_gate(run_cli, ws):
    code, out, _ = run_cli("onboarding-tool-plan", str(ws), "chrome-devtools-mcp", "--json")
    assert code == 0
    plan = _json(out)
    assert plan["1_name"]
    # Install without flags: refused, nothing executed.
    code, out, _ = run_cli("onboarding-tool-install", str(ws), "chrome-devtools-mcp", "--json")
    doc = _json(out)
    assert doc["action_required"] == "approve_install"
    # Install with flags but a manual-only tool: refused honestly.
    init_project(str(ws))
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, err = run_cli("onboarding-tool-install", str(ws), "pytest",
                             "--approve", "--authorize-install")
    assert code == 1
    assert "manual-only" in err


def test_readiness_gate_via_cli(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    code, out, _ = run_cli("onboarding-readiness", str(ws), "--json")
    assert code == 0
    assert _json(out)["outcome"] == "IN_PROGRESS"
    for qid, answer in (
        ("project.category", "Something else / general"),
        ("foundation.goal", "A tiny tool"),
        ("foundation.target_users", "Just me"),
        ("foundation.success", "It works"),
        ("quality.testing", "Automated tests"),
    ):
        run_cli("onboarding-answer", str(ws), qid, "--answer", answer)
    code, out, _ = run_cli("onboarding-readiness", str(ws), "--json")
    assert _json(out)["outcome"] == "READY"


def test_report_via_cli_output_file(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    run_cli("onboarding-answer", str(ws), "project.category", "--answer", "Writing")
    out_path = ws.parent / "report-out.md"
    code, out, _ = run_cli("onboarding-report", str(ws), "--output", str(out_path))
    assert code == 0
    assert out_path.is_file()
    assert "# Project readiness report" in out_path.read_text()
    # No-clobber without --force:
    code, out, err = run_cli("onboarding-report", str(ws), "--output", str(out_path))
    assert code == 1
    assert "existing" in err or "force" in err


def test_start_on_aperio_project_resumes(run_cli, ws):
    run_cli("onboarding-confirm-init", str(ws), "--approve")
    run_cli("onboarding-answer", str(ws), "project.category", "--answer", "Writing")
    code, out, _ = run_cli("onboarding-start", str(ws), "--json")
    doc = _json(out)
    assert doc["action_required"] == "answer_question"
    assert doc["question"]["id"] == "foundation.goal"


def test_start_on_ambiguous_state_refuses_to_proceed(run_cli, ws):
    (ws / ".aperio").mkdir()
    (ws / ".aperio/state.json").write_text("{ corrupt")
    code, out, _ = run_cli("onboarding-start", str(ws), "--json")
    assert code == 0
    doc = _json(out)
    assert doc["action_required"] == "resolve_state"
    assert doc["classification"] == "ambiguous"


def test_existing_commands_still_work(run_cli, ws):
    """Backward compatibility smoke: the classic CLI surface is intact."""
    code, out, err = run_cli("init", str(ws))
    assert code == 0, err
    assert "initialized" in out
    code, out, err = run_cli("status", str(ws))
    assert code == 0, err


def test_status_without_session_is_clean_and_actionable(run_cli, ws):
    """Regression: onboarding-status on a session-less project used to raise
    NameError (NotInitializedError was not imported in cli.py), printing a
    raw traceback. It must return a clean, actionable status instead."""
    code, out, err = run_cli("onboarding-status", str(ws), "--json")
    assert code == 0, err
    assert "Traceback" not in err
    doc = _json(out)
    assert doc["action_required"] == "none"
    assert "onboarding-start" in doc["prompt"]
