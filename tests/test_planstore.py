"""Plan persistence, approval workflow, CLI, and concurrency."""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys

import pytest

from aperio.errors import PathError, StateError, ValidationError
from aperio.planmodel import ready_task_ids
from aperio.planstore import (
    approve_plan,
    create_plan,
    plan_exists,
    plan_summary,
    read_plan,
    revise_plan,
    update_task,
)

PLAN_INPUT = {
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
        {"id": "TASK-001", "title": "Design grammar", "objective": "Choose",
         "deliverables": ["grammar.md"], "acceptance_criteria": ["reviewed"],
         "depends_on": []},
        {"id": "TASK-002", "title": "Implement", "objective": "Implement",
         "deliverables": ["parser.py"], "acceptance_criteria": ["tests pass"],
         "depends_on": ["TASK-001"]},
    ],
}


def _create(aperio_dir, content=None):
    return create_plan(aperio_dir, content or PLAN_INPUT)


def _start_and_complete(aperio_dir, task_id, owner="agent-a"):
    approve_plan(aperio_dir)
    update_task(aperio_dir, task_id, new_status="in-progress", owner=owner)
    return update_task(
        aperio_dir, task_id, new_status="completed",
        evidence="e", limitations="l", result="passed",
    )


def _write_json(tmp_path, name, data) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


# --- create / read ----------------------------------------------------------------


def test_create_starts_draft_revision_1_all_pending(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    document = _create(aperio_dir)
    assert document["plan_version"] == 1
    assert document["approval"]["status"] == "draft"
    assert all(task["status"] == "pending" for task in document["tasks"])
    assert document["created_at"].endswith("Z")
    assert (aprio := aperio_dir / "plan.json").is_file()


def test_create_on_existing_plan_conflicts(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    before = (aprio := aperio_dir / "plan.json").read_bytes()
    with pytest.raises(StateError, match="plan-revise"):
        _create(aperio_dir)
    assert (aprio := aperio_dir / "plan.json").read_bytes() == before


def test_create_on_project_with_existing_plan_file(run_cli, project_dir):
    """A pre-existing user file at plan.json (no aperio plan) is preserved:
    creation refuses instead of overwriting."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "plan.json").write_text('{"my": "user content"}')
    with pytest.raises(StateError, match="already exists"):
        _create(aperio_dir)
    assert (aprio := aperio_dir / "plan.json").read_text() == '{"my": "user content"}'


def test_create_requires_initialized_project(run_cli, project_dir):
    from aperio.errors import NotInitializedError

    run_cli("init", str(project_dir))
    (project_dir / ".aperio/state.json").unlink()
    with pytest.raises(NotInitializedError):
        _create(project_dir / ".aperio")


def test_read_plan_missing_gives_plan_create_guidance(run_cli, project_dir):
    run_cli("init", str(project_dir))
    with pytest.raises(StateError, match="plan-create"):
        read_plan(project_dir / ".aperio")


def test_corrupt_plan_rejected_on_every_operation(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "plan.json").write_text("{broken")
    for operation in (
        lambda: read_plan(aperio_dir),
        lambda: revise_plan(aperio_dir, PLAN_INPUT, revises=1),
        lambda: approve_plan(aperio_dir),
        lambda: update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a"),
    ):
        with pytest.raises(StateError, match="corrupt"):
            operation()
    assert (aprio := aperio_dir / "plan.json").read_text() == "{broken"


def test_future_plan_schema_rejected_on_operations(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    future = json.dumps({"schema_version": 3, "anything": True})
    (aprio := aperio_dir / "plan.json").write_text(future)
    for operation in (
        lambda: read_plan(aperio_dir),
        lambda: approve_plan(aperio_dir),
        lambda: update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a"),
    ):
        with pytest.raises(StateError, match="schema version 3"):
            operation()
    assert (aprio := aperio_dir / "plan.json").read_text() == future


def test_symlinked_plan_refused_on_read_and_write(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside-plan.json"
    outside.write_text(json.dumps({"secret": "outside"}))
    (aprio := aperio_dir / "plan.json").symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_plan(aperio_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        _create(aperio_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        plan_summary(aperio_dir)
    assert json.loads(outside.read_text())["secret"] == "outside"


def test_plan_tmp_leftover_is_conflict(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    (aprio := aperio_dir / "plan.json.tmp").write_text("crashed write leftover")
    with pytest.raises(StateError, match="plan.json.tmp"):
        revise_plan(aperio_dir, PLAN_INPUT, revises=1)
    assert (aprio := aperio_dir / "plan.json.tmp").read_text() == "crashed write leftover"


# --- revision / approval ------------------------------------------------------------


def test_approve_records_metadata_and_gates_tasks(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    with pytest.raises(StateError, match="approved plan"):
        update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    document = approve_plan(aperio_dir, by="human reviewer")
    assert document["approval"]["status"] == "approved"
    assert document["approval"]["approved_revision"] == 1
    assert document["approval"]["approved_by"] == "human reviewer"
    # Approval does NOT bump plan_version (it is a stamp, not content).
    assert document["plan_version"] == 1
    task = update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    assert task["status"] == "in-progress"


def test_approve_specific_revision_mismatch_conflicts(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    with pytest.raises(StateError, match="plan moved on"):
        approve_plan(aperio_dir, revision=9)
    document = approve_plan(aperio_dir, revision=1)
    assert document["approval"]["approved_revision"] == 1


def test_reapprove_restamps(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir, by="first")
    document = approve_plan(aperio_dir, by="second")
    assert document["approval"]["approved_by"] == "second"
    assert document["plan_version"] == 1


def test_revise_resets_approval_and_bumps_version(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    revised_input = json.loads(json.dumps(PLAN_INPUT))
    revised_input["scope"] = ["parser core", "docs"]
    document = revise_plan(aperio_dir, revised_input, revises=1)
    assert document["plan_version"] == 2
    assert document["approval"]["status"] == "draft"
    assert document["approval"]["approved_revision"] is None
    assert document["scope"] == ["parser core", "docs"]
    with pytest.raises(StateError, match="approved plan"):
        update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")


def test_revise_carries_lifecycle_state_verbatim(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="agent-a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    revised_input = json.loads(json.dumps(PLAN_INPUT))
    revised_input["tasks"][0]["title"] = "Design grammar (renamed)"
    document = revise_plan(aperio_dir, revised_input, revises=3)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["title"] == "Design grammar (renamed)"
    assert task["status"] == "completed"  # carried verbatim, not reset
    assert task["verification"]["result"] == "passed"
    assert task["owner"] == "agent-a"


def test_revise_refuses_removing_non_pending_task(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    revised_input = json.loads(json.dumps(PLAN_INPUT))
    revised_input["tasks"] = [revised_input["tasks"][1]]  # drop TASK-001
    revised_input["tasks"][0]["depends_on"] = []
    with pytest.raises(StateError, match="non-pending task"):
        revise_plan(aperio_dir, revised_input, revises=2)


def test_stale_revision_conflicts(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    # plan_version is now 2; a revision declaring 1 is stale.
    with pytest.raises(StateError, match="plan moved on"):
        revise_plan(aperio_dir, PLAN_INPUT, revises=1)
    document = read_plan(aperio_dir)
    assert document["plan_version"] == 2
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["status"] == "in-progress"  # progress not reverted


def test_revise_preserves_unknown_keys(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    plan_file = aperio_dir / "plan.json"
    document = json.loads(plan_file.read_text())
    document["orchestrator_note"] = {"custom": [1, 2]}
    plan_file.write_text(json.dumps(document))
    revise_plan(aperio_dir, PLAN_INPUT, revises=1)
    after = json.loads(plan_file.read_text())
    assert after["orchestrator_note"] == {"custom": [1, 2]}


# --- lifecycle via store --------------------------------------------------------------


def test_full_lifecycle_happy_path(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(aperio_dir, "TASK-001", new_status="blocked", reason="waiting")
    update_task(aperio_dir, "TASK-001", new_status="pending")
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    task = update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    assert task["status"] == "completed"
    assert task["verification"]["result"] == "passed"
    # Unblock cleared owner and reason along the way; completion re-set them.
    assert ready_task_ids(read_plan(aperio_dir)) == ["TASK-002"]


def test_failed_verification_recorded_keeps_in_progress(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    task = update_task(
        aperio_dir, "TASK-001", new_status="in-progress",
        evidence="attempt 1", limitations="flaky test", result="failed",
    )
    assert task["status"] == "in-progress"
    assert task["verification"]["result"] == "failed"
    with pytest.raises(ValidationError, match="result passed"):
        update_task(
            aperio_dir, "TASK-001", new_status="completed",
            evidence="e", limitations="l", result="failed",
        )


def test_reopen_clears_verification(run_cli, project_dir):
    """Reopening a completed task with no in-progress/completed dependents
    is allowed and clears the stale verification block (durable history
    belongs in the decision log)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    reopened = update_task(
        aperio_dir, "TASK-001", new_status="in-progress",
        owner="b", reopen=True,
    )
    assert reopened["status"] == "in-progress"
    assert reopened["verification"]["result"] is None
    assert reopened["verification"]["evidence"] is None
    assert reopened["owner"] == "b"


def test_reopen_refused_with_completed_dependent(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    for task_id in ("TASK-001", "TASK-002"):
        update_task(aperio_dir, task_id, new_status="in-progress", owner="a")
        update_task(
            aperio_dir, task_id, new_status="completed",
            evidence="e", limitations="l", result="passed",
        )
    with pytest.raises(StateError, match="cannot be reopened"):
        update_task(
            aperio_dir, "TASK-001", new_status="in-progress",
            owner="b", reopen=True,
        )


def test_unknown_task_id_conflict(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    with pytest.raises(ValidationError, match="no task TASK-999"):
        update_task(aperio_dir, "TASK-999", new_status="in-progress", owner="a")


def test_unapproved_plan_blocks_all_transitions(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    for new_status, kwargs in (
        ("in-progress", {"owner": "a"}),
        ("blocked", {"reason": "r"}),
    ):
        with pytest.raises(StateError, match="approved plan"):
            update_task(aperio_dir, "TASK-001", new_status=new_status, **kwargs)


# --- concurrency (real processes) ------------------------------------------------------


def _task_worker(project_dir: str, task_id: str, owner: str) -> int:
    """One process: claim the task, then complete it with evidence."""
    def run(cmd):
        return subprocess.run(
            [sys.executable, "-m", "aperio", *cmd],
            capture_output=True, text=True, check=False,
        )

    result = run([
        "task-status", project_dir, task_id,
        "--status", "in-progress", "--owner", owner,
    ])
    if result.returncode != 0:
        return result.returncode
    result = run([
        "task-status", project_dir, task_id,
        "--status", "completed", "--evidence", f"e-{owner}",
        "--limitations", "l", "--result", "passed",
    ])
    return result.returncode


def test_concurrent_task_updates_serialize(run_cli, project_dir):
    """Genuinely concurrent processes claiming the SAME task: the
    coordination lock serializes them, exactly one full claim+complete
    succeeds, and the rest are refused — a delayed claim can never
    silently reopen the completed task (explicit --reopen is required)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    workers = 6
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _task_worker, str(project_dir), "TASK-001", f"agent-{i}"
            )
            for i in range(workers)
        ]
        codes = [f.result(timeout=120) for f in futures]
    assert codes.count(0) == 1, "exactly one agent wins the single claim"
    assert codes.count(1) == workers - 1
    document = read_plan(aperio_dir)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["status"] == "completed"
    assert task["verification"]["evidence"] == "e-agent-0" or task[
        "verification"
    ]["evidence"].startswith("e-agent-")
    assert document["plan_version"] == 3  # create=1, claim=2, complete=3
    assert not (aprio := aperio_dir / "plan.lock").exists()
    assert not (aprio := aperio_dir / "plan.json.tmp").exists()


def test_reopen_requires_explicit_flag(run_cli, project_dir):
    """A delayed/retried claim must not silently reopen a completed task
    and destroy its verification evidence: --reopen is required."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="REAL EVIDENCE v1", limitations="l", result="passed",
    )
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "agent-b",
    )
    assert code == 1
    assert "--reopen" in err
    document = read_plan(aperio_dir)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["status"] == "completed"
    assert task["verification"]["evidence"] == "REAL EVIDENCE v1"
    # Explicit reopen: succeeds, clears the block, surfaces the superseded
    # evidence on stdout so durable history can be recorded.
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "agent-b", "--reopen",
    )
    assert code == 0, err
    assert "REAL EVIDENCE v1" in out
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    fresh = read_plan(aperio_dir)
    task = next(t for t in fresh["tasks"] if t["id"] == "TASK-001")
    assert task["status"] == "in-progress"
    assert task["verification"]["result"] is None
    assert task["owner"] == "agent-b"
    # --reopen on a non-completed transition is refused.
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "completed", "--evidence", "e", "--limitations", "l",
        "--result", "passed", "--reopen",
    )
    assert code == 1
    assert "only valid with --status in-progress" in err


def test_payload_line_separators_rejected(run_cli, project_dir):
    """Payload fields respect the shared line model: CR/NEL/LS would forge
    task-list output lines."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    with pytest.raises(ValidationError, match="line separator"):
        update_task(
            aperio_dir, "TASK-001", new_status="in-progress",
            owner="agent-a\r## FORGED LINE",
        )
    with pytest.raises(ValidationError, match="line separator"):
        update_task(
            aperio_dir, "TASK-001", new_status="blocked",
            reason="waiting\r## FORGED",
        )
    # Nothing was written.
    document = read_plan(aperio_dir)
    assert document["tasks"][0]["status"] == "pending"


def test_unknown_keys_preserved_by_approve_and_task_status(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    plan_file = aperio_dir / "plan.json"
    document = json.loads(plan_file.read_text())
    document["orchestrator_note"] = {"custom": [1, 2]}
    document["tasks"][0]["custom_field"] = "keep me"
    plan_file.write_text(json.dumps(document))

    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    after = json.loads(plan_file.read_text())
    assert after["orchestrator_note"] == {"custom": [1, 2]}
    assert after["tasks"][0]["custom_field"] == "keep me"


def test_concurrent_distinct_task_updates_all_succeed(run_cli, project_dir):
    """Six processes claim+complete six DIFFERENT tasks concurrently:
    serialization means every update lands (no lost updates) and the
    version counter advances exactly once per operation."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    big_input = json.loads(json.dumps(PLAN_INPUT))
    big_input["tasks"] = [
        {
            "id": f"TASK-{i:03d}", "title": f"task {i}", "objective": "o",
            "deliverables": ["d"], "acceptance_criteria": ["a"],
            "depends_on": [],
        }
        for i in range(1, 7)
    ]
    _create(aperio_dir, big_input)
    approve_plan(aperio_dir)
    workers = 6
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _task_worker, str(project_dir), f"TASK-{i:03d}", f"agent-{i}"
            )
            for i in range(1, workers + 1)
        ]
        codes = [f.result(timeout=180) for f in futures]
    assert codes == [0] * workers, "distinct-task updates must all succeed"
    document = read_plan(aperio_dir)
    assert all(t["status"] == "completed" for t in document["tasks"])
    assert all(t["verification"]["result"] == "passed" for t in document["tasks"])
    assert document["plan_version"] == 1 + 2 * workers  # claim + complete each


# --- CLI --------------------------------------------------------------------------------


def test_cli_plan_lifecycle_with_json_output(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    plan_file = _write_json(tmp_path, "plan.json", PLAN_INPUT)
    code, out, err = run_cli("plan-create", str(project_dir), "--file", plan_file)
    assert code == 0, err
    # Draft plan: dependency-ready tasks are NOT executable yet.
    code, out, err = run_cli("task-list", str(project_dir))
    assert code == 0
    assert "[dep-ready]" in out and "TASK-001" in out
    assert "NOT executable" in out
    code, out, err = run_cli("task-list", str(project_dir), "--json")
    assert code == 0
    listing = json.loads(out)
    assert listing[0]["is_ready"] is True
    assert listing[0]["is_executable"] is False
    assert listing[1]["is_ready"] is False
    code, out, err = run_cli("plan-show", str(project_dir), "--json")
    assert code == 0
    document = json.loads(out)
    assert document["plan_version"] == 1
    assert document["approval"]["status"] == "draft"
    code, out, err = run_cli("plan-approve", str(project_dir), "--by", "human")
    assert code == 0, err
    # Approved: the dependency-ready task is now executable.
    code, out, err = run_cli("task-list", str(project_dir))
    assert code == 0
    assert "[ready]" in out and "dep-ready" not in out
    code, out, err = run_cli("task-status", str(project_dir), "TASK-001",
                             "--status", "in-progress", "--owner", "agent-a")
    assert code == 0, err
    code, out, err = run_cli("task-status", str(project_dir), "TASK-001",
                             "--status", "completed", "--evidence", "e",
                             "--limitations", "l", "--result", "passed")
    assert code == 0, err
    assert "completed" in out
    code, out, err = run_cli("plan-show", str(project_dir))
    assert code == 0
    assert "approved" in out


def test_cli_task_status_file_payload_and_xor(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    _create(project_dir / ".aperio")
    approve_plan(project_dir / ".aperio")
    payload = _write_json(
        tmp_path, "payload.json",
        {"status": "in-progress", "owner": "agent-file"},
    )
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--file", payload, "--owner", "agent-cli",
    )
    assert code == 1
    assert "mutually exclusive" in err
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--file", payload,
    )
    assert code == 0, err
    task = next(
        t for t in read_plan(project_dir / ".aperio")["tasks"]
        if t["id"] == "TASK-001"
    )
    assert task["owner"] == "agent-file"


def test_cli_plan_create_unknown_key_rejected(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    bad = json.loads(json.dumps(PLAN_INPUT))
    bad["typo_key"] = "value"
    plan_file = _write_json(tmp_path, "bad.json", bad)
    code, _, err = run_cli("plan-create", str(project_dir), "--file", plan_file)
    assert code == 1
    assert "typo_key" in err


def test_cli_help_documents_plan_commands():
    result = subprocess.run(
        [sys.executable, "-m", "aperio", "--help"],
        capture_output=True, text=True, check=False,
    )
    for command in ("plan-create", "plan-show", "plan-approve",
                    "plan-revise", "task-list", "task-status"):
        assert command in result.stdout


def test_memory_summary_integrates_plan(run_cli, project_dir):
    """F02 portable memory gains the additive plan summary; absence is
    null and does not break loading (F02 compat)."""
    run_cli("init", str(project_dir))
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 0
    assert json.loads(out)["plan"] is None
    _create(project_dir / ".aperio")
    approve_plan(project_dir / ".aperio")
    update_task(project_dir / ".aperio", "TASK-001",
                new_status="in-progress", owner="agent-a")
    update_task(project_dir / ".aperio", "TASK-001", new_status="blocked",
                reason="waiting")
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 0
    plan = json.loads(out)["plan"]
    assert plan["present"] is True
    assert plan["approval_required"] is False
    assert plan["task_counts"]["blocked"] == 1
    assert plan["ready_task_ids"] == []  # dependency is blocked, not completed
    assert plan["blocked_task_ids"] == ["TASK-001"]


def test_memory_with_future_plan_schema_degrades_gracefully(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/plan.json").write_text(
        json.dumps({"schema_version": 3, "future": True})
    )
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 0, err  # non-fatal: an optional artifact must not brick memory
    plan = json.loads(out)["plan"]
    assert plan["present"] is True
    assert plan["unsupported_schema"] is True
    assert plan['schema_version'] == 3  # the document's own version


def test_memory_with_non_object_plan_is_corrupt(run_cli, project_dir):
    """Only a genuine newer integer schema degrades; non-object garbage is
    corrupt and fatal (not mislabeled as unsupported)."""
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/plan.json").write_text("123")
    with pytest.raises(StateError, match="corrupt"):
        plan_summary(project_dir / ".aperio")
    code, _, err = run_cli("memory", str(project_dir), "--json")
    assert code == 1
    assert "corrupt" in err


def test_plan_summary_absent_and_corrupt(run_cli, project_dir):
    run_cli("init", str(project_dir))
    assert plan_summary(project_dir / ".aperio") is None
    (project_dir / ".aperio/plan.json").write_text("{nope")
    with pytest.raises(StateError, match="corrupt"):
        plan_summary(project_dir / ".aperio")
    assert plan_exists(project_dir / ".aperio") is True


# --- Fix 1: stale completion evidence after plan revision -----------------------


def _revised_content(base, task_id, **content_changes):
    revised = json.loads(json.dumps(base))
    for task in revised["tasks"]:
        if task["id"] == task_id:
            task.update(content_changes)
    return revised


def test_revise_refuses_material_change_to_completed_task(run_cli, project_dir):
    """Changing a completed task's acceptance criteria (or any material
    content) must not silently preserve its 'passed' verification: the
    revision is refused with actionable guidance and nothing changes."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    plan_file = aperio_dir / "plan.json"
    before = plan_file.read_bytes()

    revised = _revised_content(
        PLAN_INPUT, "TASK-001", acceptance_criteria=["NEW stricter criteria"]
    )
    with pytest.raises(StateError, match="material|reopen|new task"):
        revise_plan(aperio_dir, revised, revises=3)
    assert plan_file.read_bytes() == before
    task = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-001"
    )
    assert task["verification"]["evidence"] == "e"  # evidence preserved
    assert task["acceptance_criteria"] == ["reviewed"]


def test_revise_refuses_material_change_to_in_progress_task(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", objective="Changed objective"
    )
    with pytest.raises(StateError, match="material|reopen|new task"):
        revise_plan(aperio_dir, revised, revises=2)
    task = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-001"
    )
    assert task["objective"] == "Choose"  # original content preserved


def test_revise_refuses_material_dependency_change_to_completed_task(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    _start_and_complete(aperio_dir, "TASK-002")
    revised = _revised_content(
        PLAN_INPUT, "TASK-002", depends_on=[]  # dependency removed
    )
    # TASK-002 is completed; removing its dependency is a material change.
    with pytest.raises(StateError, match="material|reopen|new task"):
        revise_plan(aperio_dir, revised, revises=5)


def test_revise_allows_title_only_edit_on_completed_task(run_cli, project_dir):
    """Non-semantic changes (title-only) may preserve lifecycle state."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", title="Design grammar (renamed)"
    )
    document = revise_plan(aperio_dir, revised, revises=3)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["title"] == "Design grammar (renamed)"
    assert task["status"] == "completed"
    assert task["verification"]["result"] == "passed"  # evidence preserved


def test_revise_allows_material_change_to_pending_task(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    revised = _revised_content(
        PLAN_INPUT, "TASK-002", acceptance_criteria=["new criteria"]
    )
    document = revise_plan(aperio_dir, revised, revises=1)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-002")
    assert task["acceptance_criteria"] == ["new criteria"]
    assert task["status"] == "pending"


def test_revise_refuses_leaving_completed_task_with_unmet_dependency(
    run_cli, project_dir
):
    """Replacing a completed task's dependency with a brand-new pending
    task would leave the completed task contradictory — refused."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    _start_and_complete(aperio_dir, "TASK-002")
    # Remove TASK-001 (pending-ed first is impossible: it is completed) —
    # so instead re-point TASK-002 at a brand-new task: TASK-001 stays
    # completed but the plan now requires TASK-003 before it.
    revised = json.loads(json.dumps(PLAN_INPUT))
    revised["tasks"].append(
        {
            "id": "TASK-003", "title": "New gate", "objective": "o",
            "deliverables": ["d"], "acceptance_criteria": ["a"],
            "depends_on": [],
        }
    )
    for task in revised["tasks"]:
        if task["id"] == "TASK-001":
            task["depends_on"] = ["TASK-003"]  # material change → refused
    with pytest.raises(StateError, match="material|reopen|new task"):
        revise_plan(aperio_dir, revised, revises=5)


def test_revise_refuses_unmet_dependency_for_in_progress_task(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    update_task(aperio_dir, "TASK-002", new_status="in-progress", owner="b")
    revised = _revised_content(
        PLAN_INPUT, "TASK-002", depends_on=[]  # drop the dependency
    )
    # TASK-002 legally in-progress (TASK-001 completed); dropping its
    # dependency is a material change to an in-progress task.
    with pytest.raises(StateError, match="material|reopen|new task"):
        revise_plan(aperio_dir, revised, revises=4)


# --- Fix 3: dependency readiness vs execution authorization ---------------------


def test_draft_plan_tasks_are_dep_ready_but_not_executable(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)  # draft
    summary = plan_summary(aperio_dir)
    assert summary["ready_task_ids"] == ["TASK-001"]  # dependency-ready
    assert summary["executable_task_ids"] == []  # draft: not authorized
    assert summary["approval_required"] is True
    code, out, err = run_cli("task-list", str(project_dir), "--json")
    assert code == 0
    listing = json.loads(out)
    assert listing[0]["is_ready"] is True
    assert listing[0]["is_executable"] is False
    code, out, err = run_cli("task-list", str(project_dir))
    assert code == 0
    assert "[dep-ready]" in out
    assert "NOT executable" in out


def test_approved_plan_tasks_are_executable(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    summary = plan_summary(aperio_dir)
    assert summary["approval_required"] is False
    assert summary["executable_task_ids"] == ["TASK-001"]
    code, out, err = run_cli("task-list", str(project_dir))
    assert code == 0
    assert "[ready]" in out
    assert "dep-ready" not in out


def test_revised_draft_tasks_lose_executability(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    revise_plan(aperio_dir, PLAN_INPUT, revises=1)  # approval reset to draft
    summary = plan_summary(aperio_dir)
    assert summary["ready_task_ids"] == ["TASK-001"]  # still dependency-ready
    assert summary["executable_task_ids"] == []  # draft again: not authorized
    code, out, err = run_cli("task-list", str(project_dir))
    assert code == 0
    assert "[dep-ready]" in out


def test_blocked_and_completed_dependency_cases(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(aperio_dir, "TASK-001", new_status="blocked", reason="waiting")
    summary = plan_summary(aperio_dir)
    assert summary["ready_task_ids"] == []  # blocked dep de-readies
    assert summary["executable_task_ids"] == []
    update_task(aperio_dir, "TASK-001", new_status="pending")  # unblock
    summary = plan_summary(aperio_dir)
    assert summary["executable_task_ids"] == ["TASK-001"]  # dep-ready again
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    summary = plan_summary(aperio_dir)
    assert summary["executable_task_ids"] == ["TASK-002"]  # completed dep


# --- Fix 1: unknown per-task extension metadata survives revisions --------------


def _add_task_metadata(aperio_dir, task_id, metadata):
    plan_file = aperio_dir / "plan.json"
    document = json.loads(plan_file.read_text())
    for task in document["tasks"]:
        if task["id"] == task_id:
            task.update(metadata)
    plan_file.write_text(json.dumps(document, ensure_ascii=False))


METADATA = {
    "orchestrator_note": {"nested": [1, 2, {"deep": True}], "none": None},
    "attempt_count": 3,
    "ratio": 0.5,
    "flag": False,
    "tags": ["a", "b"],
}


def test_extension_metadata_survives_revise_for_pending_task(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    _add_task_metadata(aperio_dir, "TASK-001", METADATA)
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", title="renamed (pending task)"
    )
    revise_plan(aperio_dir, revised, revises=1)
    task = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-001"
    )
    for key, expected in METADATA.items():
        assert task[key] == expected, f"metadata {key} lost or changed"
        assert type(task[key]) is type(expected), f"{key} changed JSON type"


def test_extension_metadata_survives_revise_for_in_progress_task(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    _add_task_metadata(aperio_dir, "TASK-001", METADATA)
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", title="renamed (in-progress task)"
    )
    revise_plan(aperio_dir, revised, revises=2)
    task = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-001"
    )
    for key, expected in METADATA.items():
        assert task[key] == expected
    assert task["status"] == "in-progress"  # lifecycle still carried


def test_extension_metadata_survives_revise_for_completed_task(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    update_task(aperio_dir, "TASK-001", new_status="in-progress", owner="a")
    update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="e", limitations="l", result="passed",
    )
    _add_task_metadata(aperio_dir, "TASK-001", METADATA)
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", title="renamed (completed task)"
    )
    revise_plan(aperio_dir, revised, revises=3)
    task = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-001"
    )
    for key, expected in METADATA.items():
        assert task[key] == expected
    assert task["verification"]["result"] == "passed"  # evidence intact


def test_removed_task_metadata_never_reassigned(run_cli, project_dir):
    """Metadata belongs to its task: a removed pending task's extension
    keys must not appear on surviving tasks."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    _add_task_metadata(aperio_dir, "TASK-001", METADATA)
    revised = json.loads(json.dumps(PLAN_INPUT))
    revised["tasks"] = [revised["tasks"][1]]  # drop TASK-001 (pending: ok)
    revised["tasks"][0]["depends_on"] = []
    revise_plan(aperio_dir, revised, revises=1)
    survivor = next(
        t for t in read_plan(aperio_dir)["tasks"] if t["id"] == "TASK-002"
    )
    for key in METADATA:
        assert key not in survivor


def test_strict_input_rejection_unchanged(run_cli, project_dir, tmp_path):
    """Extension preservation is on-disk only: author-supplied input still
    rejects unknown keys (no weakening of strict validation)."""
    run_cli("init", str(project_dir))
    bad = json.loads(json.dumps(PLAN_INPUT))
    bad["tasks"][0]["orchestrator_note"] = {"x": 1}
    input_file = tmp_path / "bad-plan.json"
    input_file.write_text(json.dumps(bad))
    with pytest.raises(ValidationError, match="orchestrator_note"):
        create_plan(project_dir / ".aperio", bad)


# --- Fix 3: accurate revision guidance ------------------------------------------


def test_material_change_guidance_names_working_workflow(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", acceptance_criteria=["NEW criteria"]
    )
    with pytest.raises(StateError, match="Return the task to 'pending'"):
        revise_plan(aperio_dir, revised, revises=3)


def test_guided_reopen_chain_actually_allows_revision(run_cli, project_dir):
    """The workflow the guidance names must actually succeed: reopen the
    completed task, block, unblock (now pending), then the material
    revision is accepted."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _create(aperio_dir)
    approve_plan(aperio_dir)
    _start_and_complete(aperio_dir, "TASK-001")
    revised = _revised_content(
        PLAN_INPUT, "TASK-001", acceptance_criteria=["NEW criteria"]
    )
    with pytest.raises(StateError, match="Return the task to 'pending'"):
        revise_plan(aperio_dir, revised, revises=3)
    # The guided chain: reopen -> block -> unblock -> revise.
    update_task(aperio_dir, "TASK-001", new_status="in-progress",
                owner="agent-x", reopen=True)
    update_task(aperio_dir, "TASK-001", new_status="blocked", reason="revising")
    update_task(aperio_dir, "TASK-001", new_status="pending")
    # Each lifecycle transition bumps plan_version; the agent re-reads
    # (plan-show --json) and declares the post-chain version.
    document = revise_plan(aperio_dir, revised, revises=6)
    task = next(t for t in document["tasks"] if t["id"] == "TASK-001")
    assert task["status"] == "pending"  # revise leaves the task pending
    assert task["verification"]["result"] is None  # cleared by reopen chain
