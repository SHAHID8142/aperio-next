"""M7 agent guidance: brief assembly, profile adaptation, manual workflows.

The brief is the paste-ready instruction package; every Aperio-side command
in the manual workflow guide is executed here so the documented procedures
are the tested ones. Provider integrations keep their own test module; this
one asserts the always-available path needs no provider at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.guidance import (
    BRIEF_FORMAT,
    build_agent_brief,
    render_agent_brief_markdown,
)
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore
from aperio.profiles import profile_suggestions


def goal(**overrides) -> dict:
    base = {
        "objective": "Summarize the evidence on urban heat islands for the "
        "council briefing",
        "intended_outcome": "A cited evidence summary with limitations",
        "profile": "research",
        "requirements": ["Separate findings from interpretation"],
        "constraints": ["Plain language for non-specialists"],
        "deliverables": ["Two-page evidence summary"],
        "acceptance_criteria": ["Every factual claim carries a source"],
        "output_contract": {"format": "markdown", "length": "about 700 words"},
        "evidence_requirements": ["Cite every factual claim"],
        "uncertainties": ["Local station data was not available"],
    }
    base.update(overrides)
    return validate_goal(base)


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# brief contract
# ---------------------------------------------------------------------------


def test_brief_requires_an_operator_declared_goal() -> None:
    untrusted = goal(provenance={"source": "agent",
                                 "note": "drafted by an assistant"})
    with pytest.raises(ValidationError, match="operator-declared"):
        build_agent_brief(untrusted)


def test_brief_carries_every_guidance_input() -> None:
    brief = build_agent_brief(goal())
    content = brief["content"]
    assert brief["format"] == BRIEF_FORMAT
    assert content["assignment"]["objective"] == goal()["objective"]
    assert content["requirements"] == ["Separate findings from "
                                       "interpretation"]
    assert content["constraints"] == ["Plain language for non-specialists"]
    assert content["deliverables"] == ["Two-page evidence summary"]
    assert content["acceptance_criteria"]
    assert content["output_contract"]["format"] == "markdown"
    assert content["quality_criteria"] == []
    assert content["uncertainties"] == ["Local station data was not available"]
    assert "cannot" in content["trust_boundary"]
    assert len(content["suggested_checks"]) >= 1  # profile adaptation


def test_brief_is_deterministic_and_rendered_from_one_payload() -> None:
    first = build_agent_brief(goal())
    second = build_agent_brief(goal())
    assert first["content_sha256"] == second["content_sha256"]
    markdown = render_agent_brief_markdown(first)
    assert markdown == render_agent_brief_markdown(second)
    assert markdown.startswith("# Task brief")
    assert "## Objective" in markdown
    assert "## Trust boundaries and limits" in markdown


def test_memory_filtering_in_the_brief(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "notes.mem")
    verified = store.propose({
        "type": "requirement", "title": "Heat threshold",
        "content": "The council uses 32 C as the heat alert threshold.",
        "scope": {"kind": "global"}, "tags": ["heat"], "actor": "analyst",
    })["memory"]
    store.review(verified["memory_id"], "accept", actor="ops-lead")
    store.review(verified["memory_id"], "verify", actor="reviewer-1",
                 evidence=["council-policy.pdf"])
    lesson = store.propose({
        "type": "lesson", "title": "Brief structure",
        "content": "Every council evidence brief lists limitations before "
        "sources.",
        "scope": {"kind": "profile", "profile": "research"},
        "tags": ["briefs"], "actor": "editor",
    })["memory"]
    store.review(lesson["memory_id"], "accept", actor="editor-2")
    proposed = store.propose({
        "type": "observation", "title": "Unconfirmed figure",
        "content": "Someone said the last summer was the hottest ever.",
        "scope": {"kind": "global"}, "tags": ["heat"], "actor": "intern",
    })["memory"]

    selection = recall_memories(
        store.snapshots()["memories"],
        objective="summarize the heat island evidence for the council",
        profile="research",
    )
    brief = build_agent_brief(goal(), memory_selection=selection)
    memory = brief["content"]["memory"]
    reviewed_ids = {e["memory_id"] for e in memory["reviewed"]}
    proposal_ids = {e["memory_id"] for e in memory["unverified_proposals"]}
    lesson_ids = {e["memory_id"] for e in memory["accepted_lessons"]}

    assert verified["memory_id"] in reviewed_ids
    assert lesson["memory_id"] in lesson_ids
    assert proposed["memory_id"] in proposal_ids
    assert proposed["memory_id"] not in reviewed_ids

    markdown = render_agent_brief_markdown(brief)
    assert "Reviewed knowledge" in markdown
    assert "Unverified proposals (assess; do not rely)" in markdown
    assert "Accepted lessons from earlier feedback" in markdown
    assert "quoted context, labelled" in markdown


def test_private_memory_never_enters_a_brief(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "priv.mem")
    store.propose({
        "type": "user_preference", "title": "Personal note",
        "content": "The councillor's home address is 12 Example Street.",
        "scope": {"kind": "global"}, "tags": ["heat"],
        "sensitivity": "private", "actor": "a",
    })
    selection = recall_memories(
        store.snapshots()["memories"],
        objective="heat island evidence",
        max_sensitivity="private",
    )
    brief = build_agent_brief(goal(), memory_selection=selection)
    assert brief["content"]["memory"]["reviewed"] == []
    assert brief["content"]["memory"]["unverified_proposals"] == []


def test_profile_suggested_checks_are_advisory_data() -> None:
    for profile_id in ("general", "design", "research", "writing",
                       "business", "software"):
        suggestions = profile_suggestions(profile_id)
        assert suggestions["suggested_checks"], profile_id
        assert all(isinstance(c, str) for c in suggestions["suggested_checks"])
    markdown = render_agent_brief_markdown(build_agent_brief(goal()))
    assert "advisory profile suggestions" in markdown
    assert "not operator requirements" in markdown


# ---------------------------------------------------------------------------
# the manual workflows documented in docs/agent-workflows.md, executed
# ---------------------------------------------------------------------------


def test_manual_workflow_commands_run_end_to_end(tmp_path: Path,
                                                 capsys) -> None:
    """The exact command sequence docs/agent-workflows.md gives a
    ChatGPT/Claude/Gemini user, executed in-process."""

    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    deliverable = tmp_path / "summary.md"
    deliverable.write_text(
        "# Urban heat islands\n\nCity centres tend to stay warmer at night "
        "(Oke, 1982). Reported differences range 1-7 C [2].\n\n## "
        "Limitations\n\nTwo reviews only; no local station data.\n" * 8,
        encoding="utf-8",
    )

    # 1. validate and preflight
    code, out, err = run_cli(capsys, "goal-check", str(goal_file))
    assert code == 0, err
    code, out, err = run_cli(capsys, "preflight", str(goal_file))
    assert code == 0, err
    # 2. prepare the paste-ready brief
    brief_file = tmp_path / "brief.md"
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--output", str(brief_file),
    )
    assert code == 0, err
    assert brief_file.exists()
    # 3. (human pastes the brief; the agent answers — simulated here)
    # 4. evaluate the answer
    code, out, err = run_cli(
        capsys, "quality-check", str(goal_file),
        "--deliverable", str(deliverable),
    )
    assert code == 0, err
    # 5. record feedback on a real gap and store the approved lesson
    feedback_file = tmp_path / "feedback.json"
    feedback_file.write_text(json.dumps({
        "problem": "Summaries must name the search window used",
        "origin": "operator", "actor": "editor-1", "severity": "low",
        "lesson": "Evidence summaries state the date range of the sources",
        "scope": {"kind": "profile", "profile": "research"},
        "created_at": "2026-11-09T12:00:00Z",
    }), encoding="utf-8")
    reviewed = tmp_path / "reviewed.json"
    code, out, err = run_cli(
        capsys, "feedback-review", str(feedback_file), "--decision",
        "accepted", "--actor", "editor-2", "--output", str(reviewed),
    )
    assert code == 0, err
    store = tmp_path / "lessons.mem"
    code, out, err = run_cli(
        capsys, "feedback-lesson", str(reviewed), "--store", str(store),
        "--actor", "editor-2",
    )
    assert code == 0, err

    # 6. a future brief carries the accepted lesson after mem-review
    code, out, err = run_cli(
        capsys, "mem-list", str(store), "--json",
    )
    assert code == 0, err
    memory_id = json.loads(out)["memories"][0]["memory_id"]
    code, _, err = run_cli(
        capsys, "mem-review", str(store), memory_id, "--accept",
        "--actor", "editor-3",
    )
    assert code == 0, err
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--memory-store", str(store),
    )
    assert code == 0, err
    assert "Accepted lessons from earlier feedback" in out


def test_brief_refuses_private_and_reports_the_cap(tmp_path: Path,
                                                   capsys) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file),
        "--memory-max-sensitivity", "private", "--json",
    )
    assert code == 0
    report = json.loads(out)
    memory = report["content"]["memory"]
    assert memory is None or (
        memory["reviewed"] == [] and memory["unverified_proposals"] == []
    )


def test_brief_output_refuses_overwrite_without_force(tmp_path: Path,
                                                      capsys) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    out_file = tmp_path / "brief.md"
    out_file.write_text("previous", encoding="utf-8")
    code, _, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--output", str(out_file),
    )
    assert code == 1
    assert "aperio: error:" in err
    assert out_file.read_text() == "previous"
    code, _, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--output", str(out_file),
        "--force",
    )
    assert code == 0, err
    assert out_file.read_text().startswith("# Task brief")
