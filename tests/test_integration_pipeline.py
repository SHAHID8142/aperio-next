"""Chunk 07: the complete end-to-end pipeline as one continuous workflow.

User objective → universal goal validation → preflight → relevant reviewed
memory → agent-ready guidance → (manual AI step, simulated) → quality
evaluation → feedback capture → explicit review → approved lesson → future
task improvement → cross-agent portable transfer → re-evaluation on the
receiving machine. Every step runs through the public CLI; no provider, no
project initialization, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from aperio.cli import main

from aperio.memstore import MemoryStore


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_full_pipeline_from_objective_to_cross_agent_reevaluation(
    tmp_path: Path, capsys
) -> None:
    machine_a = tmp_path / "machine-a"
    machine_a.mkdir()

    # 1. the operator writes the objective as a universal goal
    goal_file = machine_a / "goal.json"
    goal_file.write_text(json.dumps({
        "objective": "Summarize the evidence on urban heat islands for the "
        "council briefing",
        "intended_outcome": "A cited evidence summary with limitations",
        "profile": "research",
        "evidence_requirements": ["Cite every factual claim with a source"],
        "uncertainties": ["Local station data was not available"],
    }), encoding="utf-8")

    # 2. universal goal validation
    code, out, err = run_cli(capsys, "goal-check", str(goal_file))
    assert code == 0, err
    assert "Goal valid" in out

    # 3. mistake prevention before work begins
    code, out, err = run_cli(capsys, "preflight", str(goal_file))
    assert code == 0, err
    assert "insufficient-evidence" in out  # missing acceptance criteria etc.

    # 4. relevant reviewed memory from the operator's store
    store_file = machine_a / "notes.mem"
    store = MemoryStore(store_file)
    threshold = store.propose({
        "type": "requirement", "title": "Heat threshold",
        "content": "The council heat alert threshold is 32 C.",
        "scope": {"kind": "global"}, "tags": ["briefing"], "actor": "analyst",
    })["memory"]
    store.review(threshold["memory_id"], "accept", actor="ops-lead")

    # 5. agent-ready guidance (paste-ready package)
    brief_file = machine_a / "brief.md"
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--memory-store",
        str(store_file), "--output", str(brief_file),
    )
    assert code == 0, err
    brief_text = brief_file.read_text(encoding="utf-8")
    assert "## Objective" in brief_text
    assert "Heat threshold" in brief_text
    assert "Cite every factual claim" in brief_text

    # 6. the AI works manually (paste -> answer -> save); the answer has a
    #    real, detectable flaw on the first attempt
    answer_v1 = machine_a / "answer-v1.md"
    answer_v1.write_text(
        "# Heat islands\n\nStudies show cities are always warmer and the "
        "effect is proven largest at night.",
        encoding="utf-8",
    )

    # 7. quality evaluation of the supplied deliverable
    code, out, err = run_cli(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(answer_v1), "--json",
    )
    assert code == 0, err
    report_v1 = json.loads(out)
    fired = {i["rule_id"] for i in report_v1["content"]["issues"]}
    assert "Q-missing-attribution" in fired
    report_file = machine_a / "report-v1.json"
    report_file.write_text(out, encoding="utf-8")

    # 8. feedback capture about the real problem
    feedback_file = machine_a / "feedback.json"
    feedback_file.write_text(json.dumps({
        "problem": "Briefs must cite a source for every factual claim",
        "origin": "operator", "actor": "editor-1", "severity": "high",
        "lesson": "Every council evidence brief cites at least one source "
                  "per factual claim and names its limitations",
        "scope": {"kind": "profile", "profile": "research"},
        "created_at": "2026-11-10T09:00:00Z",
        "evaluation": {"source": "quality-report", "result": "failed",
                       "report_sha256": report_v1["content_sha256"]},
    }), encoding="utf-8")
    code, out, err = run_cli(capsys, "feedback-check", str(feedback_file))
    assert code == 0, err

    # 9. explicit review decision by a different declared editor
    reviewed_file = machine_a / "reviewed.json"
    code, out, err = run_cli(
        capsys, "feedback-review", str(feedback_file), "--decision",
        "accepted", "--actor", "editor-2", "--output", str(reviewed_file),
    )
    assert code == 0, err

    # 10. approved lesson stored as PROPOSED memory, then accepted (M3)
    lessons_file = machine_a / "lessons.mem"
    code, out, err = run_cli(
        capsys, "feedback-lesson", str(reviewed_file), "--store",
        str(lessons_file), "--actor", "editor-2",
    )
    assert code == 0, err
    lessons = MemoryStore(lessons_file)
    lesson_id = next(iter(lessons.snapshots()["memories"]))
    assert lessons.snapshots()["memories"][lesson_id]["state"] == "proposed"
    code, _, err = run_cli(
        capsys, "mem-review", str(lessons_file), lesson_id, "--accept",
        "--actor", "editor-3",
    )
    assert code == 0, err

    # 11. future task: the accepted lesson informs the next brief
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--memory-store",
        str(lessons_file),
    )
    assert code == 0, err
    assert "Accepted lessons from earlier feedback" in out

    # 12. cross-agent portable transfer to machine B
    answer_v2 = machine_a / "answer-v2.md"
    answer_v2.write_text(
        "# Heat islands\n\nCity centres tend to be warmer at night (Oke, "
        "1982). Differences of 1-7 C are reported in the cited reviews [2]."
        "\n\n## Limitations\n\nTwo reviews only; no local station data.",
        encoding="utf-8",
    )
    code, out, err = run_cli(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(answer_v2), "--json",
    )
    assert code == 0, err
    report_v2 = json.loads(out)
    report2_file = machine_a / "report-v2.json"
    report2_file.write_text(json.dumps(report_v2), encoding="utf-8")

    exchange_file = machine_a / "exchange.json"
    code, out, err = run_cli(
        capsys, "exchange-export",
        "--goal", str(goal_file),
        "--quality-report", str(report2_file),
        "--memory-store", str(lessons_file),
        "--memory-tag", "aperio-feedback",
        "--note", "Confirm the local station data question with the analyst",
        "--output", str(exchange_file),
    )
    assert code == 0, err

    machine_b = tmp_path / "machine-b"
    machine_b.mkdir()
    code, out, err = run_cli(
        capsys, "exchange-import", str(exchange_file),
        "--dest-dir", str(machine_b), "--memory-store",
        str(machine_b / "incoming.mem"), "--apply", "--json",
    )
    assert code == 0, err
    import_report = json.loads(out)
    planned = {e["kind"]: e for e in import_report["content"]["planned"]}
    assert planned["goal"]["action"] == "wrote"
    assert planned["memory"]["action"] == "memory-import"

    # 13. re-evaluation on the receiving machine, against the same goal
    imported_goal_file = machine_b / planned["goal"]["file"]
    code, out, err = run_cli(
        capsys, "goal-check", str(imported_goal_file),
    )
    assert code == 0, err
    incoming = MemoryStore(machine_b / "incoming.mem")
    snaps = incoming.snapshots()["memories"]
    assert all(snap["state"] == "proposed" for snap in snaps.values()), \
        "imported lessons never arrive promoted"
    code, out, err = run_cli(
        capsys, "quality-check", str(imported_goal_file), "--deliverable",
        str(answer_v2), "--json",
    )
    assert code == 0, err
    reevaluated = json.loads(out)
    re_fired = {i["rule_id"] for i in reevaluated["content"]["issues"]}
    assert "Q-missing-attribution" not in re_fired
    assert "Q-source-support-unknown" in re_fired, \
        "honest uncertainty survives the transfer"

    # 14. the comparison proves the improvement, regressions included
    code, out, err = run_cli(
        capsys, "feedback-compare", "--before", str(report_file),
        "--after", str(report2_file),
    )
    assert code == 0, err
    assert "improved" in out
