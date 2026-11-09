"""M5 controlled feedback and improvement: the full controlled loop.

Acceptance: task → AI output → quality issue → feedback → approved lesson →
future task → measurable comparison, with positive and negative cases, no
silent promotion, agent self-reports flagged, and M3 as the only memory
authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.feedback import (
    apply_proposal_to_goal,
    apply_proposal_to_request,
    compare_quality_reports,
    feedback_identity,
    feedback_revision,
    feedback_trust,
    lesson_memory_record,
    load_feedback_file,
    record_review,
    render_comparison_markdown,
    validate_feedback,
    validate_improvement_proposal,
)
from aperio.goals import validate_goal
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore
from aperio.quality import run_quality_check


def feedback(**overrides) -> dict:
    base = {
        "problem": "The deliverable omitted the required Results section",
        "origin": "operator",
        "actor": "editor-1",
        "severity": "high",
        "proposed_correction": "Add a Results section with the measured numbers",
        "lesson": "Every evidence summary carries Results, Limitations and "
                  "Sources sections",
        "scope": {"kind": "profile", "profile": "research"},
        "evidence": ["quality report QI-abc123"],
        "created_at": "2026-11-09T12:00:00Z",
        "task_ref": {"kind": "goal", "id": "GOAL-council-brief",
                     "note": "council evidence brief"},
        "output_ref": {"kind": "quality-report",
                       "id": "QI-abc123", "note": None},
        "evaluation": {"source": "quality-report", "result": "failed"},
    }
    base.update(overrides)
    return validate_feedback(base)


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# schema and identity
# ---------------------------------------------------------------------------


def test_identity_is_content_derived_and_review_stable() -> None:
    fb = feedback()
    reviewed = record_review(fb, decision="accepted", reviewer="editor-2",
                             at="2026-11-09T13:00:00Z")
    assert feedback_identity(fb) == feedback_identity(reviewed)
    assert feedback_revision(fb) == 1
    assert feedback_revision(reviewed) == 2


def test_declared_identity_must_match_content() -> None:
    fb = feedback()
    with pytest.raises(ValidationError, match="does not match"):
        validate_feedback({**fb, "feedback_id": "FB-000000000000"})
    declared = validate_feedback({**fb, "feedback_id": feedback_identity(fb)})
    assert declared["feedback_id"] == feedback_identity(fb)


def test_unknown_fields_and_bad_enums_are_refused() -> None:
    with pytest.raises(ValidationError, match="unknown fields"):
        validate_feedback({**feedback(), "status": "verified"})
    with pytest.raises(ValidationError, match="origin"):
        validate_feedback({**feedback(), "origin": "model"})
    with pytest.raises(ValidationError, match="severity"):
        validate_feedback({**feedback(), "severity": "catastrophic"})
    with pytest.raises(ValidationError, match="ISO-8601 UTC"):
        validate_feedback({**feedback(),
                           "reviews": [{"decision": "accepted",
                                        "reviewer": "r",
                                        "at": "yesterday"}]})


def test_state_must_match_the_recorded_reviews() -> None:
    with pytest.raises(ValidationError, match="must be 'proposed'"):
        validate_feedback({**feedback(), "state": "accepted"})
    with pytest.raises(ValidationError, match="correction_accepted"):
        validate_feedback({**feedback(), "correction_accepted": True})
    with pytest.raises(ValidationError, match="must match"):
        validate_feedback({
            **feedback(),
            "state": "accepted",
            "reviews": [{"decision": "rejected", "reviewer": "r",
                         "at": "2026-11-09T13:00:00Z"}],
        })


def test_decided_feedback_is_final() -> None:
    reviewed = record_review(feedback(), decision="rejected",
                             reviewer="editor-2", at="2026-11-09T13:00:00Z")
    with pytest.raises(ValidationError, match="already decided"):
        record_review(reviewed, decision="accepted", reviewer="editor-3",
                      at="2026-11-09T14:00:00Z")


def test_trust_follows_origin_and_explicit_review() -> None:
    fb = feedback()
    assert feedback_trust(fb) == "proposed"
    accepted = record_review(fb, decision="accepted", reviewer="r2",
                             at="2026-11-09T13:00:00Z")
    assert feedback_trust(accepted) == "authoritative"
    agent_accepted = record_review(
        feedback(origin="agent"), decision="accepted", reviewer="r2",
        at="2026-11-09T13:00:00Z",
    )
    assert feedback_trust(agent_accepted) == "attested"


# ---------------------------------------------------------------------------
# lessons through the M3 contract
# ---------------------------------------------------------------------------


def test_only_accepted_feedback_produces_a_lesson() -> None:
    with pytest.raises(ValidationError, match="only an accepted feedback"):
        lesson_memory_record(feedback())
    rejected = record_review(feedback(), decision="rejected",
                             reviewer="editor-2", at="2026-11-09T13:00:00Z")
    with pytest.raises(ValidationError, match="only an accepted feedback"):
        lesson_memory_record(rejected)


def test_lesson_enters_memory_as_proposed_and_never_promotes(tmp_path) -> None:
    accepted = record_review(feedback(), decision="accepted",
                             reviewer="editor-2", at="2026-11-09T13:00:00Z")
    store = MemoryStore(tmp_path / "lessons.mem")
    record = lesson_memory_record(accepted, actor="editor-2")
    memory = store.propose(record)["memory"]

    assert memory["state"] == "proposed"
    assert memory["trust"] == "proposed"
    assert memory["type"] == "lesson"
    # the lesson carries the feedback's evidence references
    assert memory["evidence"] == accepted["evidence"]

    # agent-origin accepted feedback is equally non-promoting
    agent_fb = record_review(
        feedback(origin="agent"), decision="accepted", reviewer="editor-2",
        at="2026-11-09T13:00:00Z",
    )
    agent_memory = store.propose(
        lesson_memory_record(agent_fb, actor="agent-1")
    )["memory"]
    assert agent_memory["state"] == "proposed"


def test_full_loop_with_measurable_comparison(tmp_path, capsys) -> None:
    """Task → output → quality issue → feedback → approved lesson → future
    task → measurable comparison (positive case)."""

    goal = validate_goal({
        "objective": "Summarize the evidence on urban heat islands for the "
        "council briefing",
        "intended_outcome": "A cited evidence summary",
        "profile": "research",
        "evidence_requirements": ["Cite every factual claim"],
    })
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal), encoding="utf-8")

    # 1. AI output with a real, detectable mistake
    deliverable = tmp_path / "brief-1.md"
    deliverable.write_text(
        "# Heat islands\n\nStudies show cities are always warmer and the "
        "effect is proven largest at night.",
        encoding="utf-8",
    )

    # 2. Quality evaluation finds the issue
    report = run_quality_check(goal, deliverable_text=deliverable.read_text())
    before_file = tmp_path / "report-before.json"
    before_file.write_text(json.dumps(report), encoding="utf-8")
    fired = {i["rule_id"] for i in report["content"]["issues"]}
    assert "Q-missing-attribution" in fired

    # 3. Feedback proposal about the real problem
    fb_file = tmp_path / "feedback.json"
    fb_file.write_text(json.dumps(feedback(
        problem="The brief cites nothing although the goal requires sources",
        lesson="Every research brief cites at least one source per factual "
               "claim and names its limitations",
        scope={"kind": "profile", "profile": "research"},
        evaluation={"source": "quality-report", "result": "failed",
                    "report_sha256": report["content_sha256"]},
    )), encoding="utf-8")
    code, out, err = run_cli(capsys, "feedback-check", str(fb_file))
    assert code == 0, err
    assert "Feedback valid" in out

    # 4. Explicit review (by someone other than the proposer)
    code, out, err = run_cli(
        capsys, "feedback-review", str(fb_file), "--decision", "accepted",
        "--actor", "editor-2", "--output", str(tmp_path / "reviewed.json"),
    )
    assert code == 0, err
    reviewed = json.loads((tmp_path / "reviewed.json").read_text())
    assert reviewed["state"] == "accepted"
    assert reviewed["correction_accepted"] is True

    # 5. Lesson stored as PROPOSED memory, then accepted through M3
    store_path = tmp_path / "lessons.mem"
    code, out, err = run_cli(
        capsys, "feedback-lesson", str(tmp_path / "reviewed.json"),
        "--store", str(store_path), "--actor", "editor-2",
    )
    assert code == 0, err
    assert "PROPOSED" in out
    store = MemoryStore(store_path)
    memory_id = store.snapshots()["memories"].popitem()[1]["memory_id"]
    store.review(memory_id, "accept", actor="editor-3")

    # 6. Future task retrieves the accepted lesson
    future_goal = validate_goal({
        "objective": "Summarize the evidence on sleep debt for the "
        "patient newsletter",
        "intended_outcome": "A cited article",
        "profile": "research",
        "evidence_requirements": ["Cite every factual claim"],
    })
    selection = recall_memories(
        store.snapshots()["memories"],
        objective=future_goal["objective"],
        profile="research",
    )
    assert selection["selected"], "the lesson must be retrievable"
    assert all(m["state"] in ("attested", "verified")
               for m in selection["selected"])

    # 7. Improved output → measurable comparison on the same goal
    improved = (
        "# Heat islands\n\nCity centres tend to be warmer at night "
        "(Oke, 1982). Differences of 1-7 C are reported in the cited "
        "reviews [2].\n\n## Limitations\n\nTwo reviews only."
    )
    after_report = run_quality_check(goal, deliverable_text=improved)
    after_file = tmp_path / "report-after.json"
    after_file.write_text(json.dumps(after_report), encoding="utf-8")

    code, out, err = run_cli(
        capsys, "feedback-compare", "--before", str(before_file),
        "--after", str(after_file),
    )
    assert code == 0, err
    assert "improved" in out
    comparison = compare_quality_reports(before_file, after_file)
    assert comparison["content"]["outcome"] == "improved"
    # the only new finding is the honest 'unknown' informational one
    assert comparison["content"]["regression_issue_ids"] == []
    assert len(comparison["content"]["informational_issue_ids"]) == 1

    # negative case: a worse deliverable regresses and the comparison says so
    worse_report = run_quality_check(
        goal, deliverable_text="A few unattributed notes with no structure."
    )
    worse_file = tmp_path / "report-worse.json"
    worse_file.write_text(json.dumps(worse_report), encoding="utf-8")
    regression = compare_quality_reports(after_file, worse_file)
    assert regression["content"]["outcome"] == "regressed"
    assert regression["content"]["new_issue_ids"]
    markdown = render_comparison_markdown(regression)
    assert "potential regressions" in markdown


def test_comparison_refuses_different_goals(tmp_path) -> None:
    goal_a = validate_goal({"objective": "Summarize the heat island evidence",
                            "intended_outcome": "A cited summary"})
    goal_b = validate_goal({"objective": "Write a sleep debt explainer",
                            "intended_outcome": "An article"})
    report_a = tmp_path / "a.json"
    report_a.write_text(json.dumps(
        run_quality_check(goal_a, deliverable_text="Some text.")
    ), encoding="utf-8")
    report_b = tmp_path / "b.json"
    report_b.write_text(json.dumps(
        run_quality_check(goal_b, deliverable_text="Some text.")
    ), encoding="utf-8")
    with pytest.raises(ValidationError, match="different goals"):
        compare_quality_reports(report_a, report_b)


def test_comparison_refuses_non_quality_reports(tmp_path) -> None:
    not_a_report = tmp_path / "x.json"
    not_a_report.write_text(json.dumps({"format": "something-else"}),
                            encoding="utf-8")
    with pytest.raises(ValidationError, match="aperio-quality-report"):
        compare_quality_reports(not_a_report, not_a_report)


# ---------------------------------------------------------------------------
# improvement proposals
# ---------------------------------------------------------------------------


def test_proposal_validation_refuses_bad_targets() -> None:
    with pytest.raises(ValidationError, match="'target' must be one of"):
        validate_improvement_proposal({
            "target": "profiles-registry",
            "change": {"action": "set", "field": "design", "value": "x"},
            "reason": "why",
        })
    with pytest.raises(ValidationError, match="reason"):
        validate_improvement_proposal({
            "target": "goal",
            "change": {"action": "add", "field": "requirements",
                       "value": "text"},
            "reason": "   ",
        })


def test_goal_proposal_applies_to_a_copy_and_is_reversible(tmp_path) -> None:
    original_goal = {
        "objective": "Summarize the evidence on urban heat islands",
        "intended_outcome": "A cited summary",
    }
    proposal = validate_improvement_proposal({
        "target": "goal",
        "change": {"action": "add", "field": "requirements",
                   "value": "Cite every factual claim"},
        "reason": "Two briefs arrived without citations",
        "feedback_id": feedback_identity(feedback()),
    })
    updated = apply_proposal_to_goal(proposal, original_goal)
    # pure: the input is untouched
    assert original_goal == {"objective":
                             "Summarize the evidence on urban heat islands",
                             "intended_outcome": "A cited summary"}
    assert "Cite every factual claim" in updated["requirements"]
    # reversible by construction: drop the copy, the operator's goal is intact
    reverted = apply_proposal_to_goal(
        validate_improvement_proposal({
            "target": "goal",
            "change": {"action": "remove", "field": "requirements",
                       "value": "Cite every factual claim"},
            "reason": "undo",
        }),
        updated,
    )
    assert reverted["requirements"] == []


def test_proposal_refuses_noop_and_duplicate_instructions() -> None:
    goal = {"objective": "Do the thing", "intended_outcome": "It is done",
            "requirements": ["Use the 2026 palette"]}
    with pytest.raises(ValidationError, match="already present"):
        apply_proposal_to_goal(
            validate_improvement_proposal({
                "target": "goal",
                "change": {"action": "add", "field": "requirements",
                           "value": "Use the 2026 palette"},
                "reason": "why",
            }),
            goal,
        )
    with pytest.raises(ValidationError, match="not present"):
        apply_proposal_to_goal(
            validate_improvement_proposal({
                "target": "goal",
                "change": {"action": "remove", "field": "requirements",
                           "value": "Use the 1999 palette"},
                "reason": "why",
            }),
            goal,
        )


def test_request_proposal_adds_required_section() -> None:
    proposal = validate_improvement_proposal({
        "target": "quality-request",
        "change": {"action": "add", "field": "required_sections",
                   "value": "Results"},
        "reason": "Missing Results section detected twice",
    })
    updated = apply_proposal_to_request(proposal, {})
    assert updated["required_sections"] == ["Results"]
    with pytest.raises(ValidationError, match="already in"):
        apply_proposal_to_request(proposal, updated)


def test_agent_self_report_feedback_is_flagged_not_silent() -> None:
    from aperio.feedback import feedback_issues

    fb = feedback(evaluation={"source": "agent-self-report",
                              "result": "passed"})
    warnings = {w["code"] for w in feedback_issues(fb)}
    assert "evaluation_self_reported" in warnings


# ---------------------------------------------------------------------------
# CLI safety
# ---------------------------------------------------------------------------


def test_feedback_review_replace_requires_expected_revision(
    tmp_path, capsys
) -> None:
    fb_file = tmp_path / "fb.json"
    fb_file.write_text(json.dumps(feedback()), encoding="utf-8")
    code, _, err = run_cli(
        capsys, "feedback-review", str(fb_file), "--decision", "accepted",
        "--actor", "editor-2", "--replace",
    )
    assert code == 1
    assert "expected-revision" in err

    code, _, err = run_cli(
        capsys, "feedback-review", str(fb_file), "--decision", "accepted",
        "--actor", "editor-2", "--replace", "--expected-revision", "7",
    )
    assert code == 1
    assert "revision 1" in err

    # correct revision replaces in place
    code, out, err = run_cli(
        capsys, "feedback-review", str(fb_file), "--decision", "accepted",
        "--actor", "editor-2", "--replace", "--expected-revision", "1",
    )
    assert code == 0, err
    on_disk = json.loads(fb_file.read_text())
    assert on_disk["state"] == "accepted"
    # and a second replace now conflicts (revision moved)
    code, _, err = run_cli(
        capsys, "feedback-review", str(fb_file), "--decision", "rejected",
        "--actor", "editor-3", "--replace", "--expected-revision", "1",
    )
    assert code == 1


def test_improvement_apply_writes_a_new_file_and_keeps_the_original(
    tmp_path, capsys
) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps({
        "objective": "Summarize the evidence on urban heat islands",
        "intended_outcome": "A cited summary",
    }), encoding="utf-8")
    original_bytes = goal_file.read_bytes()
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps({
        "proposal_format": "aperio-improvement-proposal",
        "proposal_version": 1,
        "target": "goal",
        "change": {"action": "add", "field": "requirements",
                   "value": "Cite every factual claim"},
        "reason": "Two briefs arrived without citations",
    }), encoding="utf-8")
    output = tmp_path / "goal-updated.json"
    code, out, err = run_cli(
        capsys, "improvement-apply", "--file", str(proposal_file),
        "--target-file", str(goal_file), "--output", str(output),
    )
    assert code == 0, err
    assert goal_file.read_bytes() == original_bytes
    updated = json.loads(output.read_text())
    assert "Cite every factual claim" in updated["requirements"]
    assert "the original file is unchanged" in out


def test_feedback_commands_reject_malformed_files(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    code, _, err = run_cli(capsys, "feedback-check", str(bad))
    assert code == 1
    assert err.startswith("aperio: error:")
    assert "Traceback" not in err
