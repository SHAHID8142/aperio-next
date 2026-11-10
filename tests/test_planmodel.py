"""Plan data model: validation, dependency graph, lifecycle transitions."""

from __future__ import annotations

import pytest

from aperio.errors import StateError, ValidationError
from aperio.planmodel import (
    check_reopen_allowed,
    ready_task_ids,
    validate_plan_document,
    validate_plan_input,
    validate_transition,
)


def plan_input(**overrides) -> dict:
    plan = {
        "objective": "Ship the parser module",
        "expected_outcome": "A tested, documented parser",
        "scope": ["parser core"],
        "exclusions": ["GUI"],
        "assumptions": ["stdlib only"],
        "risks": ["regex complexity"],
        "strategy": "Build bottom-up, test-first",
        "acceptance_criteria": ["all tests pass"],
        "verification_strategy": "pytest + manual review",
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Design grammar",
                "objective": "Choose the grammar",
                "deliverables": ["grammar.md"],
                "acceptance_criteria": ["grammar reviewed"],
                "depends_on": [],
            },
            {
                "id": "TASK-002",
                "title": "Implement parser",
                "objective": "Implement the parser",
                "deliverables": ["parser.py"],
                "acceptance_criteria": ["tests pass"],
                "depends_on": ["TASK-001"],
            },
        ],
    }
    plan.update(overrides)
    return plan


def stored_plan(tasks=None, approval=None) -> dict:
    """A minimal valid STORED document for transition/readiness tests."""
    if tasks is None:
        tasks = [
            {
                "id": "TASK-001", "title": "t1", "objective": "o1",
                "deliverables": ["d"], "acceptance_criteria": ["a"],
                "depends_on": [], "status": "pending", "owner": None,
                "blocked_reason": None,
                "verification": {"evidence": None, "limitations": None,
                                 "result": None, "verified_at": None},
            },
            {
                "id": "TASK-002", "title": "t2", "objective": "o2",
                "deliverables": ["d"], "acceptance_criteria": ["a"],
                "depends_on": ["TASK-001"], "status": "pending", "owner": None,
                "blocked_reason": None,
                "verification": {"evidence": None, "limitations": None,
                                 "result": None, "verified_at": None},
            },
        ]
    return {
        "schema_version": 1,
        "plan_version": 1,
        "approval": approval or {"status": "approved", "approved_revision": 1,
                                 "approved_at": "2026-01-01T00:00:00Z",
                                 "approved_by": "human"},
        "objective": "o", "expected_outcome": "e", "scope": [], "exclusions": [],
        "assumptions": [], "risks": [], "strategy": "s",
        "acceptance_criteria": ["a"], "verification_strategy": "v",
        "tasks": tasks,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "aperio_version": "0.3.0",
    }


def stored_task(task_id, status="pending", depends_on=(), **lifecycle) -> dict:
    task = {
        "id": task_id, "title": task_id, "objective": "o",
        "deliverables": ["d"], "acceptance_criteria": ["a"],
        "depends_on": list(depends_on), "status": status, "owner": None,
        "blocked_reason": None,
        "verification": {"evidence": None, "limitations": None,
                         "result": None, "verified_at": None},
    }
    task.update(lifecycle)
    return task


# --- input validation ----------------------------------------------------------


def test_valid_input_normalizes():
    plan = validate_plan_input(plan_input())
    assert [t["id"] for t in plan["tasks"]] == ["TASK-001", "TASK-002"]
    assert plan["scope"] == ["parser core"]


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError, match="typo_field"):
        validate_plan_input(plan_input(typo_field="silent corruption"))


def test_system_managed_keys_rejected():
    bad = plan_input()
    bad["plan_version"] = 9
    with pytest.raises(ValidationError, match="system-managed"):
        validate_plan_input(bad)
    bad = plan_input()
    bad["approval"] = {"status": "approved"}
    with pytest.raises(ValidationError, match="system-managed"):
        validate_plan_input(bad)


def test_missing_required_field_rejected():
    bad = plan_input()
    del bad["strategy"]
    with pytest.raises(ValidationError, match="strategy"):
        validate_plan_input(bad)


def test_unknown_task_key_rejected():
    bad = plan_input()
    bad["tasks"][0]["verifcation"] = "typo"
    with pytest.raises(ValidationError, match="verifcation"):
        validate_plan_input(bad)


def test_task_system_key_rejected_in_input():
    bad = plan_input()
    bad["tasks"][0]["status"] = "completed"  # forged lifecycle state
    with pytest.raises(ValidationError, match="system-managed"):
        validate_plan_input(bad)


def test_empty_acceptance_criteria_rejected():
    bad = plan_input()
    bad["acceptance_criteria"] = []
    with pytest.raises(ValidationError, match="acceptance_criteria"):
        validate_plan_input(bad)


def test_bad_task_id_format_rejected():
    bad = plan_input()
    bad["tasks"][0]["id"] = "task-1"
    with pytest.raises(ValidationError, match="TASK-"):
        validate_plan_input(bad)


def test_duplicate_task_ids_rejected():
    bad = plan_input()
    bad["tasks"][1]["id"] = "TASK-001"
    bad["tasks"][1]["depends_on"] = []
    with pytest.raises(ValidationError, match="duplicate task id"):
        validate_plan_input(bad)


def test_unknown_dependency_rejected():
    bad = plan_input()
    bad["tasks"][0]["depends_on"] = ["TASK-099"]
    with pytest.raises(ValidationError, match="unknown task TASK-099"):
        validate_plan_input(bad)


def test_self_dependency_rejected():
    bad = plan_input()
    bad["tasks"][0]["depends_on"] = ["TASK-001"]
    with pytest.raises(ValidationError, match="depends on itself"):
        validate_plan_input(bad)


@pytest.mark.parametrize(
    "cycle",
    [
        ["TASK-001", "TASK-002", "TASK-001"],  # 2-cycle
        ["TASK-001", "TASK-002", "TASK-003", "TASK-001"],  # 3-cycle
    ],
)
def test_cycles_detected_with_path(cycle):
    tasks = [
        {"id": cycle[i], "title": cycle[i], "objective": "o",
         "deliverables": ["d"], "acceptance_criteria": ["a"],
         "depends_on": [cycle[i + 1]]}
        for i in range(len(cycle) - 1)
    ]
    with pytest.raises(ValidationError, match="cycle detected"):
        validate_plan_input(plan_input(tasks=tasks))


def test_foreign_line_separator_in_field_rejected():
    bad = plan_input()
    bad["objective"] = "line\r## forged"
    with pytest.raises(ValidationError, match="line separator"):
        validate_plan_input(bad)


# --- stored document validation -------------------------------------------------


def test_stored_document_unknown_keys_tolerated():
    doc = stored_plan()
    doc["future_extension"] = {"any": ["thing"]}
    doc["tasks"][0]["custom_field"] = 42
    parsed = validate_plan_document(doc, "plan.json")
    assert parsed["future_extension"] == {"any": ["thing"]}
    assert parsed["tasks"][0]["custom_field"] == 42


def test_stored_document_future_schema_rejected():
    doc = stored_plan()
    doc["schema_version"] = 3
    with pytest.raises(StateError, match="schema version 3"):
        validate_plan_document(doc, "plan.json")


def test_stored_document_corrupt_shapes_rejected():
    doc = stored_plan()
    doc["plan_version"] = "one"
    with pytest.raises(StateError, match="plan_version"):
        validate_plan_document(doc, "plan.json")
    doc = stored_plan()
    doc["approval"]["status"] = "auto-approved"
    with pytest.raises(StateError, match="approval.status"):
        validate_plan_document(doc, "plan.json")
    doc = stored_plan()
    doc["tasks"][0]["status"] = "done"
    with pytest.raises(StateError, match="status is invalid"):
        validate_plan_document(doc, "plan.json")


def test_stored_completed_task_requires_verification():
    doc = stored_plan(
        tasks=[stored_task("TASK-001", status="completed")]
    )
    with pytest.raises(StateError, match="passed.*verification|verification.*passed"):
        validate_plan_document(doc, "plan.json")
    doc["tasks"][0]["verification"] = {
        "evidence": "tests", "limitations": "none",
        "result": "passed", "verified_at": "2026-01-01T00:00:00Z",
    }
    assert validate_plan_document(doc, "plan.json")["tasks"][0]["status"] == "completed"


def test_stored_blocked_task_requires_reason():
    doc = stored_plan(tasks=[stored_task("TASK-001", status="blocked")])
    with pytest.raises(StateError, match="blocked_reason"):
        validate_plan_document(doc, "plan.json")
    doc["tasks"][0]["blocked_reason"] = "waiting on upstream"
    validate_plan_document(doc, "plan.json")


def test_stored_document_dependency_target_must_exist():
    doc = stored_plan(tasks=[stored_task("TASK-001", depends_on=["TASK-099"])])
    with pytest.raises(StateError, match="invalid dependency"):
        validate_plan_document(doc, "plan.json")


# --- derived readiness ------------------------------------------------------------


def test_ready_task_selection_is_deterministic():
    plan = stored_plan()
    assert ready_task_ids(plan) == ["TASK-001"]  # TASK-002 depends on 001


def test_blocked_dependency_de_readies_dependent():
    tasks = [
        stored_task("TASK-001", status="blocked", blocked_reason="waiting"),
        stored_task("TASK-002", depends_on=["TASK-001"]),
    ]
    plan = stored_plan(tasks=tasks)
    assert ready_task_ids(plan) == []


def test_reopened_dependency_de_readies_dependent():
    tasks = [
        stored_task("TASK-001", status="in-progress", owner="agent-a"),
        stored_task("TASK-002", depends_on=["TASK-001"]),
    ]
    plan = stored_plan(tasks=tasks)
    assert ready_task_ids(plan) == []


def test_completed_dependency_readies_dependent():
    tasks = [
        stored_task(
            "TASK-001", status="completed",
            verification={"evidence": "e", "limitations": "l",
                          "result": "passed", "verified_at": "2026-01-01T00:00:00Z"},
        ),
        stored_task("TASK-002", depends_on=["TASK-001"]),
    ]
    plan = stored_plan(tasks=tasks)
    assert ready_task_ids(plan) == ["TASK-002"]


# --- lifecycle transitions ----------------------------------------------------------


def test_pending_to_in_progress_requires_owner():
    plan = stored_plan()
    with pytest.raises(ValidationError, match="--owner|owner"):
        validate_transition(plan, "TASK-001", "in-progress", owner=None,
                            reason=None, evidence=None, limitations=None, result=None)


def test_pending_to_in_progress_blocked_by_unsatisfied_deps():
    plan = stored_plan()
    with pytest.raises(StateError, match="dependencies not satisfied"):
        validate_transition(plan, "TASK-002", "in-progress", owner="agent-a",
                            reason=None, evidence=None, limitations=None, result=None)


@pytest.mark.parametrize(
    "old,new",
    [
        ("pending", "completed"),
        ("in-progress", "pending"),
        ("blocked", "in-progress"),
        ("blocked", "completed"),
        ("completed", "pending"),
        ("completed", "blocked"),
        ("completed", "completed"),
        ("pending", "pending"),
        ("blocked", "blocked"),
    ],
)
def test_forbidden_transition_matrix(old, new):
    lifecycle = {}
    if old == "completed":
        lifecycle = {"verification": {"evidence": "e", "limitations": "l",
                                      "result": "passed", "verified_at": "2026-01-01T00:00:00Z"}}
    if old == "blocked":
        lifecycle = {"blocked_reason": "waiting"}
    tasks = [stored_task("TASK-001", status=old, **lifecycle)]
    plan = stored_plan(tasks=tasks)
    kwargs = dict(owner="agent-a", reason="r", evidence="e",
                  limitations="l", result="passed")
    with pytest.raises((ValidationError, StateError), match="not allowed|already|must not silently reopen"):
        validate_transition(plan, "TASK-001", new, **kwargs)


def test_in_progress_same_status_is_verification_recording():
    """in-progress -> in-progress with a full verification payload is the
    evidence-recording operation (failed results keep the task in-progress);
    without the payload it is refused as a no-transition."""
    tasks = [stored_task("TASK-001", status="in-progress", owner="agent-a")]
    plan = stored_plan(tasks=tasks)
    base = dict(owner="agent-a", reason=None, evidence=None,
                limitations=None, result=None)
    with pytest.raises(ValidationError, match="already"):
        validate_transition(plan, "TASK-001", "in-progress", **base)
    task = validate_transition(
        plan, "TASK-001", "in-progress", owner="agent-a", reason=None,
        evidence="attempt 1", limitations="flaky", result="failed",
    )
    assert task["status"] == "in-progress"


def test_in_progress_to_completed_requires_evidence():
    tasks = [stored_task("TASK-001", status="in-progress", owner="a")]
    plan = stored_plan(tasks=tasks)
    base = dict(owner="a", reason=None, evidence=None, limitations=None, result=None)
    with pytest.raises(ValidationError, match="evidence"):
        validate_transition(plan, "TASK-001", "completed", **base)
    with pytest.raises(ValidationError, match="limitations"):
        validate_transition(plan, "TASK-001", "completed", reason=None,
                            evidence="e", limitations=None, result=None, owner="a")
    with pytest.raises(ValidationError, match="result passed"):
        validate_transition(plan, "TASK-001", "completed", reason=None,
                            evidence="e", limitations="l", result="failed", owner="a")


def test_failed_verification_cannot_complete_but_records_on_in_progress():
    tasks = [stored_task("TASK-001", status="in-progress", owner="a")]
    plan = stored_plan(tasks=tasks)
    # The store records result=failed while staying in-progress; the model
    # permits reading it back (validated document):
    doc = stored_plan(
        tasks=[
            stored_task(
                "TASK-001", status="in-progress", owner="a",
                verification={"evidence": "attempt 1", "limitations": "flaky",
                              "result": "failed", "verified_at": "2026-01-01T00:00:00Z"},
            )
        ]
    )
    assert validate_plan_document(doc, "plan.json")
    with pytest.raises(ValidationError, match="result passed"):
        validate_transition(plan, "TASK-001", "completed", owner="a",
                            reason=None, evidence="e", limitations="l", result="failed")


def test_blocking_requires_reason():
    plan = stored_plan()
    with pytest.raises(ValidationError, match="--reason|reason"):
        validate_transition(plan, "TASK-001", "blocked", owner=None,
                            reason=None, evidence=None, limitations=None, result=None)


def test_completion_rechecks_dependencies_at_operation_time():
    """TASK-002 started legally; its dependency was later reopened —
    completion is refused at operation time."""
    tasks = [
        stored_task("TASK-001", status="in-progress", owner="agent-a"),
        stored_task("TASK-002", status="in-progress", owner="agent-b",
                    depends_on=["TASK-001"]),
    ]
    plan = stored_plan(tasks=tasks)
    with pytest.raises(StateError, match="dependencies not satisfied"):
        validate_transition(plan, "TASK-002", "completed", owner="agent-b",
                            reason=None, evidence="e", limitations="l",
                            result="passed")


def test_reopen_refused_with_completed_dependent():
    tasks = [
        stored_task(
            "TASK-001", status="completed",
            verification={"evidence": "e", "limitations": "l",
                          "result": "passed", "verified_at": "2026-01-01T00:00:00Z"},
        ),
        stored_task(
            "TASK-002", status="completed", depends_on=["TASK-001"],
            verification={"evidence": "e", "limitations": "l",
                          "result": "passed", "verified_at": "2026-01-01T00:00:00Z"},
        ),
    ]
    plan = stored_plan(tasks=tasks)
    with pytest.raises(StateError, match="cannot be reopened"):
        check_reopen_allowed(plan, "TASK-001")


def test_reopen_refused_with_in_progress_dependent():
    tasks = [
        stored_task(
            "TASK-001", status="completed",
            verification={"evidence": "e", "limitations": "l",
                          "result": "passed", "verified_at": "2026-01-01T00:00:00Z"},
        ),
        stored_task("TASK-002", status="in-progress", owner="agent-b",
                    depends_on=["TASK-001"]),
    ]
    plan = stored_plan(tasks=tasks)
    with pytest.raises(StateError, match="cannot be reopened"):
        check_reopen_allowed(plan, "TASK-001")


def test_unknown_task_id_rejected():
    plan = stored_plan()
    with pytest.raises(ValidationError, match="no task TASK-999"):
        validate_transition(plan, "TASK-999", "in-progress", owner="a",
                            reason=None, evidence=None, limitations=None, result=None)


def test_tasks_array_order_is_not_semantic():
    """Readiness and parsing do not depend on array position (a reversed
    task list parses identically)."""
    doc = stored_plan()
    doc["tasks"] = list(reversed(doc["tasks"]))
    parsed = validate_plan_document(doc, "plan.json")
    assert [t["id"] for t in parsed["tasks"]] == ["TASK-002", "TASK-001"]
    assert ready_task_ids(parsed) == ["TASK-001"]


# --- Fix 2: full stored-plan validation (fail-first regressions; some tests
# --- double as regression guards for behavior that pre-existing checks covered) --


def _with_task_lifecycle(doc, task_id, **lifecycle):
    for task in doc["tasks"]:
        if task["id"] == task_id:
            task.update(lifecycle)
    return doc


def test_stored_self_dependency_rejected():
    doc = stored_plan(tasks=[stored_task("TASK-001", depends_on=["TASK-001"])])
    with pytest.raises(StateError, match="depends on itself"):
        validate_plan_document(doc, "plan.json")


def test_stored_cycle_rejected():
    doc = stored_plan(
        tasks=[
            stored_task("TASK-001", depends_on=["TASK-002"]),
            stored_task("TASK-002", depends_on=["TASK-001"]),
        ]
    )
    with pytest.raises(StateError, match="cycle detected"):
        validate_plan_document(doc, "plan.json")


def test_stored_non_string_dependency_rejected():
    for bad_dep in (7, {}, ["x"], None):
        doc = stored_plan(tasks=[stored_task("TASK-001", depends_on=[bad_dep])])
        with pytest.raises(StateError):
            validate_plan_document(doc, "plan.json")


def test_stored_missing_plan_content_field_rejected():
    doc = stored_plan()
    del doc["objective"]
    with pytest.raises(StateError, match="objective"):
        validate_plan_document(doc, "plan.json")


def test_stored_mistyped_plan_content_field_rejected():
    doc = stored_plan()
    doc["scope"] = "should-be-a-list"
    with pytest.raises(StateError, match="scope"):
        validate_plan_document(doc, "plan.json")


def test_stored_empty_task_deliverables_rejected():
    doc = stored_plan()
    doc["tasks"][0]["deliverables"] = []
    with pytest.raises(StateError, match="deliverables"):
        validate_plan_document(doc, "plan.json")


def test_stored_non_string_deliverable_rejected():
    doc = stored_plan()
    doc["tasks"][0]["deliverables"] = [3]
    with pytest.raises(StateError):
        validate_plan_document(doc, "plan.json")


def test_stored_multiline_task_title_rejected():
    doc = stored_plan()
    doc["tasks"][0]["title"] = "two\nlines"
    with pytest.raises(StateError, match="title"):
        validate_plan_document(doc, "plan.json")


def test_stored_lifecycle_dependency_contradiction_rejected():
    """An in-progress or completed task must not depend on an uncompleted
    task: that state cannot arise from legitimate aperio operations."""
    doc = stored_plan(
        tasks=[
            stored_task("TASK-001", status="pending"),
            stored_task(
                "TASK-002", status="in-progress", owner="a",
                depends_on=["TASK-001"],
            ),
        ]
    )
    with pytest.raises(StateError, match="contradict|in-progress task"):
        validate_plan_document(doc, "plan.json")
    doc = stored_plan(
        tasks=[
            stored_task("TASK-001", status="pending"),
            stored_task(
                "TASK-002", status="completed", depends_on=["TASK-001"],
                verification={"evidence": "e", "limitations": "l",
                              "result": "passed", "verified_at": "2026-01-01T00:00:00Z"},
            ),
        ]
    )
    with pytest.raises(StateError):
        validate_plan_document(doc, "plan.json")


def test_stored_impossible_approval_metadata_rejected():
    doc = stored_plan()
    doc["approval"] = {"status": "approved", "approved_revision": 5,
                       "approved_at": "2026-01-01T00:00:00Z", "approved_by": None}
    # plan_version is 1: approval of revision 5 is impossible.
    with pytest.raises(StateError, match="approved_revision"):
        validate_plan_document(doc, "plan.json")


# --- Fix 2: complete stored verification records (fail-first regressions) --------

PASSED_VERIFICATION = {
    "evidence": "tests green", "limitations": "flaky on windows",
    "result": "passed", "verified_at": "2026-01-01T00:00:00Z",
}


@pytest.mark.parametrize(
    "verification",
    [
        # missing individual fields / nulls / empty / wrong types / malformed
        {"evidence": None, "limitations": PASSED_VERIFICATION["limitations"],
         "result": "passed", "verified_at": PASSED_VERIFICATION["verified_at"]},
        {"evidence": "", "limitations": "l", "result": "passed",
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": 5, "limitations": "l", "result": "passed",
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": "e", "limitations": None, "result": "passed",
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": "e", "limitations": "l", "result": "passed"},
        {"evidence": "e", "limitations": "l", "result": "passed",
         "verified_at": None},
        {"evidence": "e", "limitations": "l", "result": "passed",
         "verified_at": "2026-01-01 00:00:00"},
        {"evidence": "e", "limitations": "l", "result": "passed",
         "verified_at": "not-a-timestamp"},
        {"evidence": "e", "limitations": "l", "result": None,
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": "e", "limitations": "l", "result": "failed",
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": "e", "limitations": "l", "result": "PASSED",
         "verified_at": "2026-01-01T00:00:00Z"},
        {"evidence": "e", "limitations": "l", "result": True,
         "verified_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_stored_completed_task_requires_complete_verification(verification):
    """Every incomplete or malformed verification combination is refused
    for a stored completed task."""
    doc = stored_plan(
        tasks=[stored_task("TASK-001", status="completed", verification=verification)]
    )
    with pytest.raises(StateError, match="verification"):
        validate_plan_document(doc, "plan.json")


def test_stored_completed_task_with_complete_verification_accepted():
    doc = stored_plan(
        tasks=[stored_task("TASK-001", status="completed",
                           verification=PASSED_VERIFICATION)]
    )
    validate_plan_document(doc, "plan.json")  # must not raise


def test_stored_in_progress_task_keeps_failed_record():
    """In-progress tasks may record failed verification attempts."""
    doc = stored_plan(
        tasks=[stored_task(
            "TASK-001", status="in-progress", owner="a",
            verification={"evidence": "attempt 1", "limitations": "flaky",
                          "result": "failed", "verified_at": "2026-01-01T00:00:00Z"},
        )]
    )
    validate_plan_document(doc, "plan.json")


def test_stored_verified_at_must_be_calendar_valid_utc():
    """P2 fix: format-only regex accepted impossible dates and
    non-padded/Unicode-digit variants; the shape+calendar check rejects
    them all."""
    for bad in (
        "2026-13-01T25:00:00Z",       # impossible calendar values
        "2026-02-30T00:00:00Z",       # impossible day
        "2026-1-1T00:00:00Z",         # non-padded
        "٢٠٢٦-٠١-٠١T٠٠:٠٠:٠٠Z",       # Unicode digits
        "2026-01-01T00:00:00.5Z",     # fractional seconds
        "2026-01-01T00:00:00+00:00",  # offset instead of Z
    ):
        doc = stored_plan(
            tasks=[
                stored_task(
                    "TASK-001", status="completed",
                    verification={"evidence": "e", "limitations": "l",
                                  "result": "passed", "verified_at": bad},
                )
            ]
        )
        with pytest.raises(StateError, match="verified_at"):
            validate_plan_document(doc, "plan.json")
    good = stored_plan(
        tasks=[
            stored_task(
                "TASK-001", status="completed",
                verification={"evidence": "e", "limitations": "l",
                              "result": "passed",
                              "verified_at": "2026-01-01T00:00:00Z"},
            )
        ]
    )
    validate_plan_document(good, "plan.json")
