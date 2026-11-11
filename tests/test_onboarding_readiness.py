"""Readiness gate and structured report."""

from __future__ import annotations

import json

import pytest

from aperio.onboarding_readiness import (
    OUTCOME_BLOCKED,
    OUTCOME_CANCELLED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_READY,
    OUTCOME_READY_WITH_PENDING,
    build_report,
    evaluate_readiness,
    render_report_markdown,
)
from aperio.onboarding_session import (
    SEVERITY_CRITICAL,
    STAGE_INTERVIEW,
    SEVERITY_OPTIONAL,
    SEVERITY_PENDING_DECISION,
    SEVERITY_REQUIRED_BEFORE_ACTION,
    add_blocker,
    add_unresolved,
    cancel_session,
    create_session,
    read_session,
    set_stage,
)
from aperio.project import init_project


@pytest.fixture
def apdir(project_dir):
    init_project(str(project_dir))
    return project_dir / ".aperio"


def _session(apdir):
    return create_session(
        apdir, workspace=str(apdir.parent), classification="empty_new"
    )


def _complete_session(apdir):
    """A session with full required coverage (general profile)."""
    create_session(apdir, workspace=str(apdir.parent), classification="empty_new")
    from aperio.onboarding_session import set_stage as _set_stage

    _set_stage(apdir, STAGE_INTERVIEW)  # past discovery/intent
    for qid, answer in (
        ("project.category", "Something else / general"),
        ("foundation.goal", "A tiny CLI tool"),
        ("foundation.target_users", "Just me"),
        ("foundation.success", "It runs my checks"),
        ("quality.testing", "Automated tests"),
    ):
        from aperio.onboarding_session import record_answer

        record_answer(apdir, qid, answer=answer, kind="confirmed")
    return read_session(apdir)


def test_cancelled_session_reports_cancelled(apdir):
    _session(apdir)
    cancel_session(apdir)
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_CANCELLED


def test_critical_unresolved_blocks(apdir):
    _session(apdir)
    add_unresolved(apdir, topic="no goal at all", severity=SEVERITY_CRITICAL)
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_BLOCKED
    assert "no goal at all" in result.open_critical


def test_open_blocker_blocks(apdir):
    _session(apdir)
    add_blocker(apdir, description="required MCP server missing and needed")
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_BLOCKED


def test_outstanding_required_is_in_progress(apdir):
    _session(apdir)  # no answers at all
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_IN_PROGRESS
    assert "project.category" in result.outstanding_required


def test_pending_decisions_give_ready_with_pending(apdir):
    _complete_session(apdir)
    add_unresolved(apdir, topic="logo choice", severity=SEVERITY_PENDING_DECISION)
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_READY_WITH_PENDING
    assert "logo choice" in result.open_pending


def test_required_before_action_gives_ready_with_pending(apdir):
    _complete_session(apdir)
    add_unresolved(
        apdir, topic="hosting provider", severity=SEVERITY_REQUIRED_BEFORE_ACTION
    )
    result = evaluate_readiness(read_session(apdir))
    assert result.outcome == OUTCOME_READY_WITH_PENDING


def test_optional_unresolved_gives_ready_with_pending(apdir):
    _complete_session(apdir)
    add_unresolved(apdir, topic="nice icon", severity=SEVERITY_OPTIONAL)
    assert evaluate_readiness(read_session(apdir)).outcome == OUTCOME_READY_WITH_PENDING


def test_full_coverage_gives_ready(apdir):
    session = _complete_session(apdir)
    result = evaluate_readiness(session)
    assert result.outcome == OUTCOME_READY
    assert result.open_critical == ()
    assert result.outstanding_required == ()


def test_ready_is_not_development_permission(apdir):
    _complete_session(apdir)
    report = build_report(read_session(apdir))
    actions = report["20_suggested_next_actions"]
    assert any("not that development may start automatically" in a for a in actions)
    markdown = render_report_markdown(report)
    assert "not permission to start executing development tasks" in markdown


def test_report_has_all_twenty_sections(apdir):
    _complete_session(apdir)
    report = build_report(read_session(apdir))
    for i in range(1, 21):
        keys = [k for k in report if k.startswith(f"{i}_")]
        assert keys, f"missing section {i}"


def test_report_does_not_fabricate_missing_inputs(apdir):
    _complete_session(apdir)
    report = build_report(read_session(apdir))
    assert report["16_tool_discovery_results"] is None
    assert report["13_existing_implementation_status"]["note"]
    assert report["15_documentation_inconsistencies"] == []


def test_report_marks_assumptions_not_confirmed(apdir):
    _complete_session(apdir)
    from aperio.onboarding_session import record_answer

    record_answer(apdir, "foundation.scope_exclusions", answer="later", kind="deferred")
    report = build_report(read_session(apdir))
    assumptions = report["10_known_assumptions"]
    assert any(a["question"] == "foundation.scope_exclusions" for a in assumptions)


def test_report_json_safe_and_renderable(apdir):
    _complete_session(apdir)
    report = build_report(read_session(apdir))
    decoded = json.loads(json.dumps(report))
    assert decoded["format"] == "aperio-onboarding-report"
    markdown = render_report_markdown(report)
    assert "# Project readiness report" in markdown
    assert "READY" in markdown
