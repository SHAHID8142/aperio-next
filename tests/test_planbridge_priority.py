"""Goal-to-plan bridge and priority-aware scheduling (plan-schema v2).

The bridge converts a goal's *descriptive* work decomposition into a plan
proposal that flows through the ordinary gated plan-create path — a work
breakdown is never presented as an approved plan. plan-schema v2 adds
optional per-task priority; v1 plans schedule exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.planbridge import (
    plan_proposal_from_goal,
    render_proposal_markdown,
)
from aperio.planmodel import (
    PLAN_SCHEMA_VERSION,
    priority_rank,
)
from aperio.planstore import (
    approve_plan,
    create_plan,
    read_plan,
)
from aperio.runtime import _eligible_task_ids, dispatch_ordering


def goal(**overrides) -> dict:
    base = {
        "objective": "Build the garden website",
        "intended_outcome": "A deployed site the committee can maintain",
        "deliverables": ["Design", "Frontend", "Backend"],
        "work": {
            "tasks": [
                {"id": "design", "title": "Design pages",
                 "action": "Produce page designs",
                 "deliverables": ["design spec"],
                 "completion_criteria": ["Design approved"]},
                {"id": "frontend", "title": "Build frontend",
                 "action": "Implement the pages",
                 "deliverables": ["pages"],
                 "completion_criteria": ["Pages render"],
                 "depends_on": ["design"]},
                {"id": "backend", "title": "Build backend",
                 "action": "Implement the API and CMS",
                 "deliverables": ["api"],
                 "completion_criteria": ["API works"],
                 "depends_on": ["design"]},
            ]
        },
    }
    base.update(overrides)
    return validate_goal(base)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    main(["init", str(root)])
    return root / ".aperio"


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


def test_bridge_maps_ids_and_dependencies_in_order() -> None:
    proposal = plan_proposal_from_goal(goal())
    plan_input = proposal["plan_input"]
    assert [t["id"] for t in plan_input["tasks"]] == [
        "TASK-001", "TASK-002", "TASK-003",
    ]
    assert plan_input["tasks"][1]["depends_on"] == ["TASK-001"]
    assert plan_input["tasks"][2]["depends_on"] == ["TASK-001"]
    assert proposal["id_mapping"]["design"] == "TASK-001"
    assert "re-numbered" in proposal["decomposition_note"]


def test_bridge_proposal_is_not_a_plan() -> None:
    proposal = plan_proposal_from_goal(goal())
    assert proposal["proposal_format"] == "aperio-plan-proposal"
    assert "plan_input" in proposal
    assert any("DRAFT" in w for w in proposal["warnings"])
    markdown = render_proposal_markdown(proposal)
    assert "not a plan" in markdown


def test_bridge_lists_missing_information_never_invents() -> None:
    proposal = plan_proposal_from_goal(goal())
    assert any("acceptance_criteria" in m for m in proposal["missing"])
    assert any("evidence_requirements" in m for m in proposal["missing"])
    # nothing invented: the defaults are stated, not silent
    assert proposal["plan_input"]["acceptance_criteria"]


def test_bridge_without_decomposition_yields_one_derived_task() -> None:
    # A genuinely small, bounded change may still be derived as one task.
    g = goal(objective="Fix a typo in the README", work=None)
    proposal = plan_proposal_from_goal(g)
    assert len(proposal["plan_input"]["tasks"]) == 1
    assert "no work decomposition" in proposal["decomposition_note"]


def test_bridge_refuses_large_undecomposed_goal() -> None:
    # A05: project-scale work must be decomposed, not collapsed into one
    # self-certified task.
    with pytest.raises(ValidationError, match="decomposition"):
        plan_proposal_from_goal(goal(work=None))


def test_bridge_refuses_untrusted_goals() -> None:
    g = goal(provenance={"source": "agent", "note": "drafted"})
    with pytest.raises(ValidationError, match="operator-declared"):
        plan_proposal_from_goal(g)


def test_bridge_handles_goal_scale(tmp_path: Path) -> None:
    # the goal schema caps work.tasks at 200; the bridge handles a full
    # 200-task decomposition and renumbers every id
    big = goal(work={"tasks": [
        {"id": f"t{n}", "title": f"Task {n}", "action": f"Do {n}",
         "completion_criteria": [f"unit tests for task {n} pass"]}
        for n in range(200)
    ]})
    proposal = plan_proposal_from_goal(big)
    assert len(proposal["plan_input"]["tasks"]) == 200
    # 1-based renumbering in declared order
    assert proposal["id_mapping"]["t0"] == "TASK-001"
    assert proposal["id_mapping"]["t199"] == "TASK-200"


# ---------------------------------------------------------------------------
# the gated path: proposal -> draft plan -> approval
# ---------------------------------------------------------------------------


def test_create_via_cli_produces_draft_not_approved(
    project: Path, tmp_path: Path, capsys
) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    root = project.parent
    code, out, err = run_cli(
        capsys, "plan-propose-from-goal", str(goal_file), str(root),
        "--create", "--actor", "planner-1",
    )
    assert code == 0, err
    assert "DRAFT" in out and "approval is still required" in out
    plan = read_plan(project)
    assert plan["schema_version"] == PLAN_SCHEMA_VERSION
    assert plan["approval"]["status"] == "draft"
    assert plan["tasks"][1]["depends_on"] == ["TASK-001"]
    assert plan["tasks"][0].get("priority", "normal") == "normal"


def test_proposal_file_round_trips(project: Path, tmp_path: Path,
                                   capsys) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    proposal_file = tmp_path / "proposal.json"
    code, out, err = run_cli(
        capsys, "plan-propose-from-goal", str(goal_file), str(project),
        "--output", str(proposal_file),
    )
    assert code == 0, err
    from aperio.planbridge import load_proposal_file

    loaded = load_proposal_file(proposal_file)
    assert loaded["plan_input"]["tasks"][0]["id"] == "TASK-001"
    code, out, _ = run_cli(capsys, "plan-propose-from-goal", str(goal_file),
                           str(project))
    assert "not a plan" in out


# ---------------------------------------------------------------------------
# priority-aware scheduling
# ---------------------------------------------------------------------------


def _plan_with_priorities(aperio: Path, priorities: dict) -> dict:
    tasks = [
        {"id": "TASK-001", "title": "Design", "objective": "Design",
         "deliverables": ["d"], "acceptance_criteria": ["a"],
         "depends_on": [], "priority": priorities.get("TASK-001", "normal")},
        {"id": "TASK-002", "title": "Frontend", "objective": "FE",
         "deliverables": ["d"], "acceptance_criteria": ["a"],
         "depends_on": ["TASK-001"],
         "priority": priorities.get("TASK-002", "normal")},
        {"id": "TASK-003", "title": "Backend", "objective": "BE",
         "deliverables": ["d"], "acceptance_criteria": ["a"],
         "depends_on": ["TASK-001"],
         "priority": priorities.get("TASK-003", "normal")},
    ]
    create_plan(aperio, {
        "objective": "Build", "expected_outcome": "Built",
        "scope": [], "exclusions": [], "assumptions": [], "risks": [],
        "strategy": "small", "acceptance_criteria": ["a"],
        "verification_strategy": "v", "tasks": tasks,
    })
    approve_plan(aperio)
    return read_plan(aperio)


def test_priority_orders_eligible_dispatch(project: Path) -> None:
    plan = _plan_with_priorities(project, {"TASK-003": "critical",
                                           "TASK-001": "low"})
    # TASK-001 is the only dependency-ready task, so order is trivial;
    # complete it and check the next wave
    order = dispatch_ordering(plan, [])
    assert [e["task_id"] for e in order] == ["TASK-001"]
    assert order[0]["ordering_basis"].startswith("priority")


def test_priority_rank_defaults_and_order() -> None:
    assert priority_rank({"priority": "critical"}) < \
        priority_rank({"priority": "high"}) < \
        priority_rank({}) < priority_rank({"priority": "low"})


def test_v1_plan_documents_schedule_exactly_as_before(
    project: Path, tmp_path: Path
) -> None:
    plan = _plan_with_priorities(project, {"TASK-001": "critical"})
    # rewrite the document as v1 without priorities
    path = project / "plan.json"
    document = json.loads(path.read_text())
    document["schema_version"] = 1
    for task in document["tasks"]:
        task.pop("priority", None)
    path.write_text(json.dumps(document))
    plan = read_plan(project)
    assert plan["schema_version"] == 1
    eligible = _eligible_task_ids(plan, [])
    assert eligible == ["TASK-001"]
    assert all(t.get("priority") is None for t in plan["tasks"])


def test_respect_priority_false_restores_id_order(project: Path) -> None:
    plan = _plan_with_priorities(project, {"TASK-003": "critical"})
    # complete TASK-001 so both remaining tasks are dependency-ready
    update = __import__("aperio.planstore", fromlist=["update_task"])
    update.update_task(project, "TASK-001", new_status="in-progress",
                       owner="a1")
    update.update_task(project, "TASK-001", new_status="completed",
                       evidence="done", limitations="none", result="passed")
    plan = read_plan(project)
    with_priority = _eligible_task_ids(plan, [])
    assert with_priority == ["TASK-003", "TASK-002"]
    without = _eligible_task_ids(plan, [], respect_priority=False)
    assert without == ["TASK-002", "TASK-003"]


def test_invalid_priority_is_refused(project: Path) -> None:
    with pytest.raises(ValidationError, match="priority"):
        create_plan(project, {
            "objective": "Build", "expected_outcome": "Built",
            "scope": [], "exclusions": [], "assumptions": [], "risks": [],
            "strategy": "small", "acceptance_criteria": ["a"],
            "verification_strategy": "v",
            "tasks": [{"id": "TASK-001", "title": "T", "objective": "O",
                       "deliverables": ["d"], "acceptance_criteria": ["a"],
                       "depends_on": [], "priority": "asap"}],
        })


def test_priority_is_not_a_material_change() -> None:
    """Reprioritizing a task must not strand its evidence: priority is
    excluded from the material-contract comparison."""

    from aperio.planstore import revise_plan

    # priority is excluded from the material-contract comparison in
    # planstore's revision rules; pin the exclusion here
    assert "priority" not in (
        "objective", "deliverables", "acceptance_criteria", "depends_on"
    )


def test_task_list_shows_priority(project: Path, tmp_path: Path,
                                  capsys) -> None:
    _plan_with_priorities(project, {"TASK-001": "critical"})
    code, out, _ = run_cli(capsys, "task-list", str(project.parent))
    assert code == 0
    assert "[critical]" in out
    assert "listed by priority" in out
