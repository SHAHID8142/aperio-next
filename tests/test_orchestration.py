"""Controlled multi-agent orchestration: assessment, delegation, briefs.

The orchestrator is a supervising layer over the existing plan/scheduler/
adapters — it never launches anything, never grants authority, never
offers parallelism (write-scope isolation is unproven), and always
explains itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.adapters import builtin_adapters
from aperio.cli import main
from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.orchestrate import (
    OrchestrationStore,
    assess_orchestration,
    build_orchestration_plan,
    render_specialist_brief_markdown,
    specialist_brief,
    validate_orchestration,
)
from aperio.planstore import approve_plan, create_plan, read_plan


def goal(**overrides) -> dict:
    base = {
        "objective": "Build the garden website: design, frontend, backend, CMS",
        "intended_outcome": "A deployed site the committee can maintain",
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


PLAN_TASKS = [
    {"id": "TASK-001", "title": "Design pages", "objective": "Produce designs",
     "deliverables": ["design spec"], "acceptance_criteria": ["approved"],
     "depends_on": []},
    {"id": "TASK-002", "title": "Build frontend", "objective": "Pages",
     "deliverables": ["pages"], "acceptance_criteria": ["render"],
     "depends_on": ["TASK-001"]},
    {"id": "TASK-003", "title": "Build backend", "objective": "API + CMS",
     "deliverables": ["api"], "acceptance_criteria": ["works"],
     "depends_on": ["TASK-001"]},
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    main(["init", str(root)])
    aperio = root / ".aperio"
    create_plan(aperio, {
        "objective": "Build the garden website",
        "expected_outcome": "Deployed site",
        "scope": [], "exclusions": [], "assumptions": [], "risks": [],
        "strategy": "small", "acceptance_criteria": ["works"],
        "verification_strategy": "v",
        "tasks": [
            {**task, "priority": "normal"} for task in PLAN_TASKS
        ],
    })
    approve_plan(aperio)
    return aperio


def all_adapter_ids() -> list[str]:
    return [a.id for a in builtin_adapters()]


# ---------------------------------------------------------------------------
# assessment
# ---------------------------------------------------------------------------


def test_no_capability_declarations_mean_single_agent() -> None:
    assessment = assess_orchestration(
        goal(), plan_tasks=PLAN_TASKS, available_adapters=all_adapter_ids(),
    )
    assert assessment["recommendation"] == "single-agent"
    assert any("will not split work" in r for r in assessment["reasons"])
    assert assessment["parallel_offered"] is False


def test_declared_capabilities_drive_specialist_recommendation() -> None:
    assessment = assess_orchestration(
        goal(), plan_tasks=PLAN_TASKS,
        available_adapters=all_adapter_ids(),
        required_capabilities={
            "TASK-002": "coding", "TASK-003": "coding",
        },
    )
    assert assessment["recommendation"] == "specialist-delegation"
    assert any("coding" in r for r in assessment["reasons"])
    assert all("sequential" in r.lower() for r in assessment["reasons"][-1:])


def test_capability_gaps_fall_back_to_manual_not_silence() -> None:
    assessment = assess_orchestration(
        goal(), plan_tasks=PLAN_TASKS,
        available_adapters=["manual"],
        required_capabilities={"TASK-002": "coding"},
    )
    assert assessment["recommendation"] == "single-agent-with-manual-fallback"
    assert assessment["capability_gaps"][0]["task_id"] == "TASK-002"


def test_assessment_criteria_are_declared_input_only() -> None:
    assessment = assess_orchestration(
        goal(), plan_tasks=PLAN_TASKS, available_adapters=all_adapter_ids(),
        required_capabilities={"TASK-001": "coding"},
    )
    assert assessment["criteria"]["tasks_with_declared_capability"] == 1
    with pytest.raises(ValidationError, match="unknown task"):
        assess_orchestration(
            goal(), plan_tasks=PLAN_TASKS,
            available_adapters=all_adapter_ids(),
            required_capabilities={"TASK-999": "coding"},
        )


# ---------------------------------------------------------------------------
# orchestration plan document
# ---------------------------------------------------------------------------


def test_orchestration_plan_maps_specialists_and_records_basis(
    project: Path,
) -> None:
    g = goal()
    document = build_orchestration_plan(
        g, PLAN_TASKS,
        required_capabilities={"TASK-002": "coding", "TASK-003": "coding"},
        available_adapters=all_adapter_ids(),
        actor="operator",
    )
    validate_orchestration(document)
    by_task = {d["task_id"]: d for d in document["delegations"]}
    assert by_task["TASK-002"]["specialist_adapter"] in ("codex-cli",
                                                         "claude-cli")
    assert by_task["TASK-001"]["specialist_adapter"] == "manual"
    assert "neutral default" in by_task["TASK-001"]["selection_basis"]
    assert document["sequential_only"] is True
    assert "does not infer" in json.dumps(document["assessment"])


def test_adapter_override_respected(project: Path) -> None:
    document = build_orchestration_plan(
        goal(), PLAN_TASKS,
        adapter_override={"TASK-002": "local-process"},
        available_adapters=all_adapter_ids(),
    )
    by_task = {d["task_id"]: d for d in document["delegations"]}
    assert by_task["TASK-002"]["specialist_adapter"] == "local-process"


def test_unknown_override_or_capability_refused(project: Path) -> None:
    with pytest.raises(ValidationError, match="unknown task"):
        build_orchestration_plan(
            goal(), PLAN_TASKS, adapter_override={"TASK-999": "manual"},
            available_adapters=all_adapter_ids(),
        )
    with pytest.raises(ValidationError, match="unknown task"):
        build_orchestration_plan(
            goal(), PLAN_TASKS,
            required_capabilities={"TASK-999": "coding"},
            available_adapters=all_adapter_ids(),
        )


def test_orchestration_store_validates_and_persists(project: Path) -> None:
    store = OrchestrationStore(project)
    document = build_orchestration_plan(
        goal(), PLAN_TASKS, available_adapters=all_adapter_ids(),
    )
    store.write(document)
    loaded = store.read()
    assert loaded["orchestration_format"] == "aperio-orchestration"
    with pytest.raises(ValidationError, match="duplicate delegation"):
        broken = json.loads(json.dumps(document))
        broken["delegations"].append(dict(broken["delegations"][0]))
        validate_orchestration(broken)
    with pytest.raises(ValidationError, match="sequential_only"):
        tampered = json.loads(json.dumps(document))
        tampered["sequential_only"] = False
        validate_orchestration(tampered)


# ---------------------------------------------------------------------------
# specialist briefs: scoped context
# ---------------------------------------------------------------------------


def test_specialist_brief_scopes_context_to_its_own_task(
    project: Path,
) -> None:
    g = goal()
    document = build_orchestration_plan(
        g, PLAN_TASKS, available_adapters=all_adapter_ids(),
    )
    store = OrchestrationStore(project)
    store.write(document)
    plan = read_plan(project)
    brief = specialist_brief(document, plan, "TASK-002", g)
    markdown = render_specialist_brief_markdown(brief)
    assert "Build frontend" in markdown
    # other tasks appear only as dependency context, never deliverables
    assert "design spec" not in markdown
    assert "API + CMS" not in markdown
    assert "TASK-001" in markdown  # dependency context is present
    assert "cannot approve its own work" in markdown
    assert "sequential" in markdown


def test_specialist_brief_refuses_unknown_task_or_missing_plan(
    project: Path,
) -> None:
    document = build_orchestration_plan(
        goal(), PLAN_TASKS, available_adapters=all_adapter_ids(),
    )
    store = OrchestrationStore(project)
    store.write(document)
    plan = read_plan(project)
    with pytest.raises(ValidationError, match="no delegation"):
        specialist_brief(document, plan, "TASK-999", goal())
    with pytest.raises(ValidationError, match="not in the current plan"):
        specialist_brief(document, {"tasks": []}, "TASK-002", goal())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_orchestrate_assess_plan_and_brief(
    project: Path, tmp_path: Path, capsys
) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps(goal()), encoding="utf-8")
    root = project.parent

    code, out, err = run_cli(capsys, "orchestrate-assess", str(root),
                             str(goal_file))
    assert code == 0, err
    assert "single-agent" in out

    code, out, err = run_cli(
        capsys, "orchestrate-assess", str(root), str(goal_file),
        "--capability", "TASK-002:coding", "--capability", "TASK-003:coding",
    )
    assert code == 0, err
    assert "specialist-delegation" in out

    code, out, err = run_cli(
        capsys, "orchestrate-plan", str(root), str(goal_file),
        "--capability", "TASK-002:coding", "--capability", "TASK-003:coding",
        "--actor", "operator", "--json",
    )
    assert code == 0, err
    document = json.loads(out)
    assert document["sequential_only"] is True

    code, out, err = run_cli(
        capsys, "orchestrate-brief", str(root), "TASK-002", str(goal_file),
    )
    assert code == 0, err
    assert "Build frontend" in out
    assert "cannot approve its own work" in out

    # malformed capability -> clean error
    code, _, err = run_cli(capsys, "orchestrate-assess", str(root),
                           str(goal_file), "--capability", "nope")
    assert code == 1
    assert err.startswith("aperio: error:")
    assert "Traceback" not in err


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_orchestration_requires_approved_plan_tasks(project: Path) -> None:
    """Orchestration maps the CURRENT plan; an unapproved draft still
    assesses (assessment is advice), but the delegation explicitly notes
    that dispatch requires approval."""

    g = goal()
    document = build_orchestration_plan(
        g, PLAN_TASKS, available_adapters=all_adapter_ids(),
    )
    for delegation in document["delegations"]:
        assert "authorization" in delegation
        assert "--authorize-exec" in delegation["authorization"]["execution"]
