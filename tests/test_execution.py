"""Feature 04: agent execution runtime — eligibility, adapters, records,
concurrency, recovery, and CLI."""

from __future__ import annotations

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aperio.artifacts import create_exclusive_text, read_confined_artifact
from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.planstore import approve_plan, create_plan, update_task
from aperio.planstore import plan_summary  # noqa: F401  (integration probe)
from aperio.runtime import (
    cancel_run,
    dispatch_task,
    execution_summary,
    read_executions,
    read_run_handoff,
    reconcile_runs,
    record_external_result,
    runs_dispatch,
)

PLAN_INPUT = {
    "objective": "Deliver the export feature",
    "expected_outcome": "Working export",
    "scope": ["export"], "exclusions": [], "assumptions": [], "risks": [],
    "strategy": "incremental",
    "acceptance_criteria": ["export works"],
    "verification_strategy": "pytest",
    "tasks": [
        {"id": "TASK-001", "title": "Build", "objective": "Build it",
         "deliverables": ["x.py"], "acceptance_criteria": ["works"],
         "depends_on": []},
        {"id": "TASK-002", "title": "Test", "objective": "Test it",
         "deliverables": ["t.py"], "acceptance_criteria": ["passes"],
         "depends_on": ["TASK-001"]},
    ],
}


def _prepared_project(run_cli, project_dir, approve=True):
    run_cli("init", str(project_dir))
    create_plan(project_dir / ".aperio", PLAN_INPUT)
    if approve:
        approve_plan(project_dir / ".aperio")
    return project_dir / ".aperio"


def _local_config(tmp_path, script: str, *, project_dir=None, **overrides) -> dict:
    config = {
        "executable": sys.executable,
        "args": ["-c", script],
        "working_dir": None,  # filled by callers with the project dir
        "timeout_seconds": 30,
        "env_policy": "minimal",
    }
    config.update(overrides)
    if config["working_dir"] is None:
        if project_dir is None:
            raise ValueError("local-process tests must pass project_dir")
        config["working_dir"] = str(project_dir)
    return config


def _write_config(tmp_path, name, config) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(config))
    return str(path)


def _write_json(tmp_path, name, data) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


# --- eligibility ----------------------------------------------------------------


def test_dispatch_requires_approved_plan(run_cli, project_dir):
    _prepared_project(run_cli, project_dir, approve=False)
    with pytest.raises(StateError, match="approved plan"):
        dispatch_task(project_dir / ".aperio", "TASK-001",
                      adapter_id="manual", config=None,
                      authorize_exec=False)
    # Nothing was written.
    assert (project_dir / ".aperio/executions.json").exists() is False


def test_dispatch_requires_dependency_ready(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    approve_plan(project_dir / ".aperio")
    with pytest.raises(StateError, match="dependencies not completed"):
        dispatch_task(project_dir / ".aperio", "TASK-002",
                      adapter_id="manual", config=None, authorize_exec=False)


def test_dispatch_rejects_unknown_task(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    with pytest.raises((StateError, ValidationError)):
        dispatch_task(project_dir / ".aperio", "TASK-999",
                      adapter_id="manual", config=None, authorize_exec=False)


def test_dispatch_rejects_unknown_adapter(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    with pytest.raises(ValidationError, match="unknown agent adapter"):
        dispatch_task(project_dir / ".aperio", "TASK-001",
                      adapter_id="claude-code", config=None,
                      authorize_exec=False)


# --- manual handoff adapter ---------------------------------------------------------


def test_manual_run_prepares_handoff_and_awaits(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False)
    assert record["status"] == "awaiting-external"
    assert record["run_id"] == "RUN-0001"
    handoff = aperio_dir / "runs/RUN-0001.handoff.md"
    assert handoff.is_file()
    text = handoff.read_text()
    assert "aperio:run-handoff:v1" in text
    assert "TASK-001" in text
    assert "acceptance criteria" in text.lower()
    # The task was claimed through the F03 lifecycle (in-progress, owned).
    plan = json.loads((aprio := aperio_dir / "plan.json").read_text())
    task = plan["tasks"][0]
    assert task["status"] == "in-progress"
    assert task["owner"] == "manual:RUN-0001"


def test_manual_handoff_injection_is_neutralized(run_cli, project_dir):
    """Forged column-0 headings in plan content cannot break the handoff
    document: content renders continuation-indented."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    forged_plan = json.loads(json.dumps(PLAN_INPUT))
    forged_plan["tasks"][0]["title"] = "real"
    forged_plan["tasks"][0]["objective"] = "o\n## FORGED SECTION"
    create_plan(aperio_dir, forged_plan)
    approve_plan(aperio_dir)
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False)
    text = (aprio := aperio_dir / "runs" / f"{record['run_id']}.handoff.md").read_text()
    forged_lines = [
        line for line in text.split("\n")
        if line.startswith("## FORGED")
    ]
    assert forged_lines == []  # content is continuation-indented


def test_manual_run_report_records_outcome(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "outcome_reported": "external agent finished the work",
        "success_reported": True,
        "diagnostics": "notes from the external agent",
    }))
    # run-report via the store API:
    run = record_external_result(aperio_dir, "RUN-0001", json.loads(report.read_text()))
    assert run["status"] == "completed"
    assert "external agent reported success" in run["outcome"]
    # ...but the TASK is NOT automatically completed.
    plan = json.loads((aprio := aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"


def test_run_report_requires_awaiting_external(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    with pytest.raises(StateError, match="no execution records"):
        record_external_result(project_dir / ".aperio", "RUN-0001",
                               {"outcome_reported": "x", "success_reported": True})


def test_run_report_strict_validation(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    with pytest.raises(ValidationError, match="outcome_reported"):
        record_external_result(aperio_dir, "RUN-0001", {"success_reported": True})
    with pytest.raises(ValidationError, match="success_reported"):
        record_external_result(aperio_dir, "RUN-0001",
                               {"outcome_reported": "x", "success_reported": "yes"})
    with pytest.raises(ValidationError, match="unknown"):
        record_external_result(aperio_dir, "RUN-0001",
                               {"outcome_reported": "x", "success_reported": True,
                                "typo": 1})


# --- local-process adapter (harmless fixtures) --------------------------------------


def test_local_process_requires_authorization(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    # Unauthorized local-process dispatch is an entirely read-only
    # preview: no record, no claim, no process (Fix 2).
    config = _local_config(tmp_path, project_dir=project_dir,
                           script="print('hello')")
    result = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=False)
    assert result["status"] == "preview"
    assert read_executions(aperio_dir) is None  # no record created
    assert not (aprio := project_dir / ".aperio/local/runs").exists()
    # The task remains eligible: a later authorized run works.
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["run_id"] == "RUN-0001"  # first real run
    assert record["status"] == "completed"


def test_local_process_execution_success(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('hello from child')")
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["status"] == "completed"
    assert record["exit_code"] == 0
    # Privacy boundary: raw child output stays machine-locally; the
    # shareable record carries only neutral metadata.
    assert "hello from child" not in json.dumps(record)
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    assert log.is_file() and b"hello from child" in log.read_bytes()
    assert record["local_log_ref"] == "local/runs/RUN-0001.log"
    # ...and the TASK is still in-progress: execution != verification.
    plan = json.loads((aprio := aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"


def test_local_process_nonzero_exit_fails_run(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir,
        script="import sys; print('boom'); sys.exit(3)")
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["status"] == "failed"
    assert record["exit_code"] == 3
    assert "boom" not in json.dumps(record)  # output stays local
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    assert b"boom" in log.read_bytes()
    assert record.get("local_log_ref") == "local/runs/RUN-0001.log"


def test_local_process_timeout_kills_child(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir,
        script="import time; print('started', flush=True); time.sleep(60)",
        timeout_seconds=1)
    started = time.monotonic()
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    elapsed = time.monotonic() - started
    assert record["status"] == "failed"
    assert "timed out" in record["outcome"]
    assert elapsed < 20  # killed promptly, not 60 s


def test_local_process_missing_executable(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="pass",
        executable="/nonexistent/aperio-test-exe")
    with pytest.raises(ValidationError, match="not found"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)


def test_local_process_bounded_capture_no_deadlock(run_cli, project_dir, tmp_path):
    """A chatty child producing far more than the capture bound must not
    deadlock; the tail is retained, the full stream mirrored locally."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir,
        script="for i in range(400): print('x' * 200, flush=True)")
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["status"] == "completed"
    assert record["output_truncated"]["stdout"] is True
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    assert log.stat().st_size > 64 * 1024  # full stream mirrored locally


def test_local_process_env_minimal(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    script = (
        "import json, os, sys;"
        "print(json.dumps({k: os.environ.get(k) for k in"
        " ('PATH', 'HOME', 'APERIO_SECRET_TEST')}))"
    )
    os.environ["APERIO_SECRET_TEST"] = "secret-value"
    config = _local_config(
        tmp_path, project_dir=project_dir, script=script)
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    # Child env output is machine-local only; read it from the local log.
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    child_env = json.loads(log.read_text().strip())
    assert child_env["APERIO_SECRET_TEST"] is None  # minimal env: not passed
    assert child_env["PATH"]  # lookup vars are present
    # And the shareable record carries no child output.
    assert "APERIO_SECRET_TEST" not in json.dumps(record)


def test_working_dir_outside_project_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    outside = tmp_path / "outside-workdir"
    outside.mkdir()
    config = _local_config(
        tmp_path, project_dir=project_dir, script="pass",
        working_dir=str(outside))
    with pytest.raises(ValidationError, match="outside the project boundary"):
        dispatch_task(project_dir / ".aperio", "TASK-001",
                      adapter_id="local-process", config=config,
                      authorize_exec=True)


def test_working_dir_symlink_escape_refused(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    create_plan(project_dir / ".aperio", PLAN_INPUT)
    approve_plan(project_dir / ".aperio")
    outside = tmp_path / "outside-workdir"
    outside.mkdir()
    link = project_dir / "workdir-link"
    link.symlink_to(outside, target_is_directory=True)
    config = _local_config(
        tmp_path, project_dir=project_dir, script="pass",
        working_dir=str(link))
    with pytest.raises(ValidationError, match="outside the project boundary"):
        dispatch_task(project_dir / ".aperio", "TASK-001",
                      adapter_id="local-process", config=config,
                      authorize_exec=True)
    assert list(outside.iterdir()) == []


# --- duplicate dispatch / concurrency ------------------------------------------------


def test_duplicate_dispatch_prevented(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    with pytest.raises(StateError, match="active run"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)


def test_concurrent_dispatch_single_winner(run_cli, project_dir):
    """Genuinely concurrent processes dispatching the same task: exactly
    one wins, the rest are refused (active run / lifecycle gate)."""
    _prepared_project(run_cli, project_dir)
    workers = 6

    def worker(i):
        result = subprocess.run(
            [sys.executable, "-m", "aperio", "run-start",
             str(project_dir), "TASK-001", "--agent", "manual"],
            capture_output=True, text=True, check=False,
        )
        return result.returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(workers)]
        codes = [f.result(timeout=120) for f in futures]
    assert codes.count(0) == 1
    document = read_executions(project_dir / ".aperio")
    assert len(document["runs"]) == 1


def test_concurrent_distinct_tasks_all_dispatch(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    big = json.loads(json.dumps(PLAN_INPUT))
    big["tasks"] = [
        {"id": f"TASK-{i:03d}", "title": f"t{i}", "objective": "o",
         "deliverables": ["d"], "acceptance_criteria": ["a"], "depends_on": []}
        for i in range(1, 5)
    ]
    from aperio.planstore import create_plan as cp

    # Recreate the plan with 4 independent tasks.
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "plan.json").unlink()
    cp(aperio_dir, big)
    approve_plan(aperio_dir)

    def worker(i):
        result = subprocess.run(
            [sys.executable, "-m", "aperio", "run-start",
             str(project_dir), f"TASK-{i:03d}", "--agent", "manual"],
            capture_output=True, text=True, check=False,
        )
        return result.returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, i) for i in range(1, 5)]
        codes = [f.result(timeout=120) for f in futures]
    assert codes == [0] * 4
    document = read_executions(aperio_dir)
    assert len(document["runs"]) == 4
    assert len({r["run_id"] for r in document["runs"]}) == 4


def test_dispatch_vs_manual_claim_race_is_clean(run_cli, project_dir):
    """A manual claim landing between eligibility and the runtime claim
    finalizes the run as failed (claim lost) — never dangling."""
    run_cli("init", str(project_dir))
    from aperio import runtime as runtime_module
    from aperio.planstore import create_plan as cp

    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    real_update = runtime_module.update_task

    def claim_interference(*a, **kw):
        # A competing agent claims the task right before the runtime claim.
        real_update(*a, **kw)
        raise StateError("task claimed by a competing writer")

    monkey_target = runtime_module
    import unittest.mock as mock

    with mock.patch.object(runtime_module, "update_task",
                           side_effect=claim_interference):
        with pytest.raises(StateError, match="could not be claimed"):
            dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                          config=None, authorize_exec=False)
    document = read_executions(aperio_dir)
    assert document["runs"][0]["status"] == "failed"
    assert "claim lost" in document["runs"][0]["outcome"]


# --- retry / history ------------------------------------------------------------------


def test_retry_creates_new_run_and_preserves_history(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="import sys; sys.exit(9)")
    failed = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert failed["status"] == "failed"
    # Retry (same adapter, task already claimed by the first run).
    config2 = _local_config(
        tmp_path, project_dir=project_dir, script="print('recovered')")
    retry = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                          config=config2, authorize_exec=True,
                          previous_run_id="RUN-0001",
                          remediation="script changed to print('recovered')")
    assert retry["run_id"] == "RUN-0002"
    assert retry["previous_run_id"] == "RUN-0001"
    assert retry["status"] == "completed"
    document = read_executions(aperio_dir)
    assert len(document["runs"]) == 2
    assert document["runs"][0]["status"] == "failed"  # history preserved


def test_retry_different_adapter_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="import sys; sys.exit(9)")
    dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                  config=config, authorize_exec=True)
    with pytest.raises(StateError, match="owned by"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)


# --- cancel / reconcile -----------------------------------------------------------------


def test_cancel_awaiting_external_run(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    run = cancel_run(aperio_dir, "RUN-0001", reason="operator decision")
    assert run["status"] == "cancelled"
    with pytest.raises(StateError, match="already finalized"):
        cancel_run(aperio_dir, "RUN-0001")


def test_cancel_finalized_run_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('done')")
    dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                  config=config, authorize_exec=True)
    with pytest.raises(StateError, match="already finalized"):
        cancel_run(aperio_dir, "RUN-0001")


def test_reconcile_marks_dead_pid_interrupted(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False)
    # Simulate a crash: rewrite the record to running with a dead pid.
    from aperio.planstore import plan_path  # noqa: F401
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "running"
    document["runs"][0]["pid"] = 999999999
    document["runs"][0]["machine"] = os.uname().nodename
    exec_file.write_text(json.dumps(document))
    report = reconcile_runs(aperio_dir)
    assert report["interrupted"] == ["RUN-0001"]
    assert report["recovery_required"] is True
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "interrupted"
    assert "no success was invented" in run["outcome"]


def test_reconcile_cancelled_prepared_with_claimed_task(run_cli, project_dir):
    """Crash between claim and running-flip: record cancelled, task
    reported orphaned."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    # Simulate the crash window: force the record back to prepared while
    # the task stays claimed.
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "prepared"
    exec_file.write_text(json.dumps(document))
    report = reconcile_runs(aperio_dir)
    assert report["cancelled_stale"] == ["RUN-0001"]
    assert report["orphaned_tasks"] == ["TASK-001"]
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "cancelled"


def test_reconcile_left_running_for_live_pid(run_cli, project_dir):
    """A live recorded process is left running (reported, not decided)."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "running"
    document["runs"][0]["pid"] = os.getpid()  # this test process: alive
    document["runs"][0]["machine"] = os.uname().nodename
    exec_file.write_text(json.dumps(document))
    report = reconcile_runs(aperio_dir)
    assert report["left_running"] and not report["interrupted"]


def test_reconcile_foreign_machine_reported_not_decided(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "running"
    document["runs"][0]["pid"] = 4242424
    document["runs"][0]["machine"] = "some-other-machine"
    exec_file.write_text(json.dumps(document))
    report = reconcile_runs(aperio_dir)
    assert report["foreign_machine"]
    assert not report["interrupted"]
    # And cancel likewise refuses to signal a foreign pid.
    with pytest.raises(StateError, match="another machine"):
        cancel_run(aperio_dir, "RUN-0001")


def test_plan_revision_during_execution_flagged(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    from aperio.planstore import revise_plan

    revise_plan(aperio_dir, PLAN_INPUT, revises=2)  # title-only revision
    report = reconcile_runs(aperio_dir)
    drift = [
        d for d in report["plan_version_drift"]
        if d["run_id"] == "RUN-0001"
    ]
    assert drift and drift[0]["current_plan_version"] == 3


# --- records: corrupt / future-version / symlink ----------------------------------------


def test_corrupt_executions_fatal(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    (aprio := aperio_dir / "executions.json").write_text("{broken")
    with pytest.raises(StateError, match="corrupt"):
        read_executions(aperio_dir)
    assert (aprio := aperio_dir / "executions.json").read_text() == "{broken"


def test_future_exec_schema_refused_on_write(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["schema_version"] = 2
    exec_file.write_text(json.dumps(document))
    with pytest.raises(StateError, match="schema version 2"):
        cancel_run(aperio_dir, "RUN-0001")
    # Memory degrades gracefully.
    summary = execution_summary(aperio_dir)
    assert summary["unsupported_schema"] is True


def test_symlinked_executions_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (aprio := aperio_dir / "executions.json").symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_executions(aperio_dir)
    assert outside.read_text() == "{}"


def test_no_secrets_in_shareable_records(run_cli, project_dir, tmp_path, monkeypatch):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    monkeypatch.setenv("APERIO_TEST_TOKEN", "ghp_" + "s" * 36)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    record_text = (aprio := aperio_dir / "executions.json").read_text()
    assert "ghp_" not in record_text
    handoff = (aprio := aperio_dir / "runs/RUN-0001.handoff.md").read_text()
    assert "ghp_" not in handoff


# --- memory integration -----------------------------------------------------------------


def test_memory_execution_summary_additive(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    summary = execution_summary(aperio_dir)
    assert summary["present"] is True
    assert summary["active_runs"][0]["run_id"] == "RUN-0001"
    assert summary["recovery_required"] is False  # awaiting-external is normal


def test_memory_without_executions_is_null(run_cli, project_dir):
    _prepared_project(run_cli, project_dir)
    assert execution_summary(project_dir / ".aperio") is None
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 0
    assert json.loads(out)["execution"] is None


# --- CLI --------------------------------------------------------------------------------


def test_cli_agent_list_and_run_flow(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    code, out, err = run_cli("agent-list", str(project_dir))
    assert code == 0
    assert "manual" in out and "local-process" in out
    code, out, err = run_cli("run-preview", str(project_dir), "TASK-001",
                             "--agent", "manual")
    assert code == 0, err
    data = json.loads(out)
    assert data["eligible"] is True
    config = _write_json(tmp_path, "cfg.json", _local_config(
        tmp_path, project_dir=project_dir, script="print('x')"))
    code, out, err = run_cli("run-start", str(project_dir), "TASK-001",
                             "--agent", "local-process",
                             "--config", config, "--authorize-exec", "--json")
    assert code == 0, err
    record = json.loads(out)
    assert record["status"] == "completed"
    code, out, err = run_cli("run-status", str(project_dir), record["run_id"],
                             "--json")
    assert code == 0
    assert json.loads(out)["status"] == "completed"
    code, out, err = run_cli("runs-list", str(project_dir), "--json")
    assert code == 0
    assert len(json.loads(out)) == 1


def test_cli_run_start_without_authorization_is_read_only(
    run_cli, project_dir, tmp_path
):
    """run-start without --authorize-exec is an entirely read-only
    preview: no record, no claim, no process; a later authorized run
    works normally (nothing was blocked)."""
    _prepared_project(run_cli, project_dir)
    config = _write_json(tmp_path, "cfg.json", _local_config(
        tmp_path, project_dir=project_dir, script="print('x')"))
    code, out, err = run_cli("run-start", str(project_dir), "TASK-001",
                             "--agent", "local-process", "--config", config)
    assert code == 0, err
    assert read_executions(project_dir / ".aperio") is None
    assert not (project_dir / ".aperio/local/runs").exists()
    code, out, err = run_cli("run-start", str(project_dir), "TASK-001",
                             "--agent", "local-process", "--config", config,
                             "--authorize-exec")
    assert code == 0, err
    assert "completed" in out



def test_secrets_from_child_output_stay_local(run_cli, project_dir, tmp_path):
    """Synthetic credentials printed by a child process must never enter
    the shareable execution record or the portable memory JSON — the
    privacy boundary stores raw output machine-locally only."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    script = (
        "import json, sys;"
        "print(json.dumps({"
        "'api_key': 'ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG1234',"
        "'password': 'hunter2-super-secret',"
        "'token': 'AKIAIOSFODNN7EXAMPLE',"
        "'private_path': '/Users/someone/.ssh/id_rsa',"
        "'env_secret': 'very-secret-env-value',"
        "}))"
    )
    config = _local_config(
        tmp_path, project_dir=project_dir, script=script)
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["status"] == "completed"
    record_text = json.dumps(record)
    for secret in ("ghp_AAAABBBB", "hunter2-super-secret",
                   "AKIAIOSFODNN7EXAMPLE", "/Users/someone/.ssh",
                   "very-secret-env-value"):
        assert secret not in record_text, f"secret {secret!r} leaked into record"
    # The raw output IS in the machine-local log (by design).
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    assert b"ghp_AAAABBBB" in log.read_bytes()
    # The shareable record has the neutral metadata instead.
    assert record["output_truncated"] is not None
    assert record["local_log_ref"] == "local/runs/RUN-0001.log"
    # Memory JSON also stays clean.
    summary = execution_summary(aperio_dir)
    assert "ghp_AAAABBBB" not in json.dumps(summary)


# --- stabilization: debug output / config secrecy ---------------------------------------

SYNTHETIC_CONFIG_SECRET = "ghp_" + "CONFIGSECRET" * 3


def test_no_debug_config_dump_in_output_or_records(
    run_cli, project_dir, tmp_path
):
    """A config value (argv item) must never be echoed by aperio itself:
    no DEBUG dump to stderr, and nothing in the shareable record or
    portable memory."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('ok')",
        args=["-c", "print('ok')", SYNTHETIC_CONFIG_SECRET],
    )
    config_path = _write_config(tmp_path, "cfg.json", config)
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", config_path,
        "--authorize-exec",
    )
    assert code == 0, err
    assert SYNTHETIC_CONFIG_SECRET not in out
    assert SYNTHETIC_CONFIG_SECRET not in err
    assert "DEBUG" not in err
    assert "DEBUG" not in out
    records = (aperio_dir / "executions.json").read_text()
    assert SYNTHETIC_CONFIG_SECRET not in records
    memory = json.dumps(execution_summary(aperio_dir))
    assert SYNTHETIC_CONFIG_SECRET not in memory


def test_invalid_config_error_does_not_echo_config(
    run_cli, project_dir, tmp_path
):
    """An invalid config produces one actionable line, never a traceback
    and never a dump of the (possibly secret-bearing) config object."""
    _prepared_project(run_cli, project_dir)
    config = {"executable": "", "secret_extra": SYNTHETIC_CONFIG_SECRET}
    config_path = _write_config(tmp_path, "cfg.json", config)
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", config_path,
        "--authorize-exec",
    )
    assert code == 1
    assert "Traceback" not in err
    assert "aperio: error:" in err
    assert SYNTHETIC_CONFIG_SECRET not in err
    assert SYNTHETIC_CONFIG_SECRET not in out
    assert read_executions(project_dir / ".aperio") is None


# --- stabilization: run-handoff schema + confinement --------------------------------------


def _tamper_handoff_ref(aperio_dir: Path, ref) -> None:
    path = aperio_dir / "executions.json"
    document = json.loads(path.read_text())
    document["runs"][0]["handoff_ref"] = ref
    path.write_text(json.dumps(document))


def test_run_handoff_cli_reads_prepared_manual_handoff(run_cli, project_dir):
    """End-to-end: a freshly prepared manual handoff can be read back by
    the CLI through the current handoff_ref schema."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual",
        config=None, authorize_exec=False,
    )
    assert record["handoff_ref"] == f"runs/{record['run_id']}.handoff.md"
    code, out, err = run_cli(
        "run-handoff", str(project_dir), record["run_id"]
    )
    assert code == 0, err
    assert "<!-- aperio:run-handoff:v1 -->" in out
    assert "Build" in out
    code, out, err = run_cli(
        "run-status", str(project_dir), record["run_id"]
    )
    assert code == 0, err
    assert f".aperio/runs/{record['run_id']}.handoff.md" in out
    # Store-level reader returns the same document.
    text = read_run_handoff(aperio_dir, record["run_id"])
    assert "<!-- aperio:run-handoff:v1 -->" in text


@pytest.mark.parametrize("bad_ref", [
    "../outside.md",
    "runs/../../outside.md",
    "/etc/passwd",
    "runs/other-run.handoff.md",
    "local/runs/RUN-0001.log",
    "runs/RUN-0001.md",
    7,
    None,
])
def test_run_handoff_rejects_malformed_or_outside_references(
    run_cli, project_dir, tmp_path, bad_ref
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET")
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual",
        config=None, authorize_exec=False,
    )
    _tamper_handoff_ref(aperio_dir, bad_ref)
    code, out, err = run_cli(
        "run-handoff", str(project_dir), record["run_id"]
    )
    assert code == 1
    assert "Traceback" not in err
    assert "aperio: error:" in err
    assert "OUTSIDE-SECRET" not in out
    assert outside.read_text() == "OUTSIDE-SECRET"


def test_run_handoff_refuses_symlinked_runs_directory(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual",
        config=None, authorize_exec=False,
    )
    real_runs = aperio_dir / "runs"
    outside = tmp_path / "outside-runs"
    shutil.move(str(real_runs), str(outside))
    os.symlink(outside, real_runs)
    code, out, err = run_cli(
        "run-handoff", str(project_dir), record["run_id"]
    )
    assert code == 1
    assert "symlink" in err
    assert "Traceback" not in err
    assert (outside / f"{record['run_id']}.handoff.md").is_file()


def test_run_handoff_refuses_symlinked_leaf(run_cli, project_dir, tmp_path):
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual",
        config=None, authorize_exec=False,
    )
    handoff = aperio_dir / "runs" / f"{record['run_id']}.handoff.md"
    outside = tmp_path / "outside-handoff.md"
    handoff.unlink()
    os.symlink(outside, handoff)  # dangling: target never created
    code, out, err = run_cli(
        "run-handoff", str(project_dir), record["run_id"]
    )
    assert code == 1
    assert "symlink" in err
    assert not outside.exists()


def test_run_handoff_without_reference_is_actionable(run_cli, project_dir):
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual",
        config=None, authorize_exec=False,
    )
    _tamper_handoff_ref(aperio_dir, None)
    code, out, err = run_cli(
        "run-handoff", str(project_dir), record["run_id"]
    )
    assert code == 1
    assert "no prepared run-handoff document" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("ref", [
    "../outside.md",
    "runs/../../outside.md",
    "runs/./RUN-0001.handoff.md",
    "runs//RUN-0001.handoff.md",
    "/abs/RUN-0001.handoff.md",
    "local/runs/RUN-0001.handoff.md",
    "runs/.handoff.md",
    "runs/sub/RUN-0001.handoff.md",
    "runs\\RUN-0001.handoff.md",
    "runs/RUN-0001.handoff.md\x00.txt",
])
def test_read_confined_artifact_rejects_unsafe_references(
    run_cli, project_dir, tmp_path, ref
):
    """Defense-in-depth: the shared reader itself enforces confinement,
    independent of the caller's exact-reference check."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET")
    runs = aperio_dir / "runs"
    runs.mkdir(exist_ok=True)
    (runs / "RUN-0001.handoff.md").write_text("ok")
    with pytest.raises((PathError, StateError)):
        read_confined_artifact(
            runs, ref, dir_name="runs", suffix=".handoff.md"
        )
    assert outside.read_text() == "OUTSIDE-SECRET"


def test_read_confined_artifact_refuses_linked_leaf(run_cli, project_dir, tmp_path):
    aperio_dir = _prepared_project(run_cli, project_dir)
    runs = aperio_dir / "runs"
    runs.mkdir(exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET")
    os.symlink(outside, runs / "RUN-0001.handoff.md")
    with pytest.raises(PathError, match="symlink"):
        read_confined_artifact(
            runs, "runs/RUN-0001.handoff.md",
            dir_name="runs", suffix=".handoff.md",
        )
    assert outside.read_text() == "OUTSIDE-SECRET"


def test_read_confined_artifact_refuses_linked_directory(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    (outside / "RUN-0001.handoff.md").write_text("OUTSIDE-SECRET")
    os.symlink(outside, aperio_dir / "runs")
    with pytest.raises(PathError, match="symlink"):
        read_confined_artifact(
            aperio_dir / "runs", "runs/RUN-0001.handoff.md",
            dir_name="runs", suffix=".handoff.md",
        )
    assert (outside / "RUN-0001.handoff.md").read_text() == "OUTSIDE-SECRET"


# --- stabilization: artifact path safety --------------------------------------------------


def test_local_runs_parent_symlink_refused(run_cli, project_dir, tmp_path):
    """A symlinked .aperio/local/runs must never redirect child output
    outside the project."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside-local-runs"
    outside.mkdir()
    os.symlink(outside, aperio_dir / "local" / "runs")
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')")
    with pytest.raises(PathError, match="symlink"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)
    assert list(outside.iterdir()) == []
    # The run record exists (created before execution) but failed cleanly.
    document = read_executions(aperio_dir)
    assert document["runs"][0]["status"] == "failed"
    assert not (aperio_dir / "executions.lock").exists()


def test_local_runs_parent_non_directory_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    (aperio_dir / "local" / "runs").write_text("occupied")
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')")
    with pytest.raises(PathError, match="non-directory occupant"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)


def test_manual_runs_symlink_refused(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    os.symlink(outside, aperio_dir / "runs")
    with pytest.raises(PathError, match="symlink"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    assert list(outside.iterdir()) == []


def test_dangling_handoff_symlink_never_followed(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    runs = aperio_dir / "runs"
    runs.mkdir()
    outside = tmp_path / "not-created.md"
    os.symlink(outside, runs / "RUN-0001.handoff.md")
    with pytest.raises(PathError, match="symlink"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    assert not outside.exists()


def test_occupied_local_log_is_preserved_not_truncated(
    run_cli, project_dir, tmp_path
):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    log_dir = aperio_dir / "local" / "runs"
    log_dir.mkdir(parents=True)
    occupied = log_dir / "RUN-0001.log"
    occupied.write_text("PRESERVE-ME")
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')")
    with pytest.raises(StateError, match="already occupied"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)
    assert occupied.read_text() == "PRESERVE-ME"
    assert read_executions(aperio_dir)["runs"][0]["status"] == "failed"


def test_missing_executable_cleans_up_log(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')",
        executable=str(tmp_path / "definitely-not-here"),
    )
    with pytest.raises(ValidationError, match="executable not found"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)
    assert not (aperio_dir / "local/runs/RUN-0001.log").exists()
    assert read_executions(aperio_dir)["runs"][0]["status"] == "failed"
    # The shareable record carries no absolute machine path from the error.
    records = (aperio_dir / "executions.json").read_text()
    assert str(tmp_path) not in records
    assert not (aperio_dir / "executions.lock").exists()


def test_permission_failure_is_an_aperio_error(
    run_cli, project_dir, tmp_path
):
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("permission checks are meaningless as root")
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    local_dir = aperio_dir / "local"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')")
    local_dir.chmod(0o500)
    try:
        with pytest.raises(StateError, match="cannot create artifact directory"):
            dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                          config=config, authorize_exec=True)
    finally:
        local_dir.chmod(0o700)


def test_exclusive_artifact_creation_race(tmp_path):
    """Only one racing writer may create the leaf; the winner's content
    is never truncated and the loser reports a conflict."""
    target = tmp_path / "raced.handoff.md"

    def attempt(index: int) -> str:
        try:
            create_exclusive_text(target, f"payload-{index}")
            return f"won-{index}"
        except StateError:
            return f"lost-{index}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    winners = [o for o in outcomes if o.startswith("won-")]
    assert len(winners) == 1
    index = winners[0].split("-")[1]
    assert target.read_text() == f"payload-{index}"


def test_run_report_diagnostics_symlink_refused(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    outside = tmp_path / "outside-local-runs"
    outside.mkdir()
    os.symlink(outside, aperio_dir / "local" / "runs")
    with pytest.raises(PathError, match="symlink"):
        record_external_result(aperio_dir, "RUN-0001", {
            "outcome_reported": "done",
            "success_reported": True,
            "diagnostics": "notes",
        })
    assert list(outside.iterdir()) == []
    # No partial mutation: the run is still awaiting an external report.
    assert read_executions(aperio_dir)["runs"][0]["status"] == "awaiting-external"


def test_run_report_occupied_report_file_preserved(run_cli, project_dir):
    aperio_dir = _prepared_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    report_dir = aperio_dir / "local" / "runs"
    report_dir.mkdir(parents=True)
    occupied = report_dir / "RUN-0001.report.txt"
    occupied.write_text("PRESERVE-ME")
    with pytest.raises(StateError, match="already occupied"):
        record_external_result(aperio_dir, "RUN-0001", {
            "outcome_reported": "done", "success_reported": True,
        })
    assert occupied.read_text() == "PRESERVE-ME"
    assert read_executions(aperio_dir)["runs"][0]["status"] == "awaiting-external"


# --- stabilization: external-result privacy ----------------------------------------------

EXTERNAL_SECRETS = (
    "ghp_" + "EXTERNAL" * 4,
    "hunter2-external-password",
    "AKIA" + "Q" * 16,
    "/Users/private-operator/.ssh/id_rsa",
    "acme-internal-project-codename",
)


def _external_result_payload() -> dict:
    secret_blob = " ".join(EXTERNAL_SECRETS)
    return {
        "outcome_reported": f"finished the work; {secret_blob}",
        "success_reported": True,
        "diagnostics": f"password=... path=... {secret_blob}",
    }


def test_external_report_text_stays_machine_local(run_cli, project_dir):
    """Raw external-reported text must never enter executions.json or
    portable memory; only neutral bounded metadata may be shareable."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    run = record_external_result(aperio_dir, "RUN-0001", _external_result_payload())
    assert run["outcome"] == "external agent reported success"
    assert run["external_reported"]["raw_stored_locally"] is True
    records = (aperio_dir / "executions.json").read_text()
    memory = json.dumps(execution_summary(aperio_dir))
    for secret in EXTERNAL_SECRETS:
        assert secret not in json.dumps(run)
        assert secret not in records
        assert secret not in memory
    # The raw report is kept machine-locally by design.
    local = (aperio_dir / "local/runs/RUN-0001.report.txt").read_text()
    for secret in EXTERNAL_SECRETS:
        assert secret in local


def test_external_report_cli_output_is_clean(run_cli, project_dir, tmp_path):
    aperio_dir = _prepared_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_external_result_payload()))
    code, out, err = run_cli("run-report", str(project_dir), "RUN-0001",
                             "--file", str(report))
    assert code == 0, err
    for secret in EXTERNAL_SECRETS:
        assert secret not in out
        assert secret not in err
    records = (aperio_dir / "executions.json").read_text()
    for secret in EXTERNAL_SECRETS:
        assert secret not in records


# --- stabilization: scheduler preview / subprocess dispatch -------------------------------

PLAN_INPUT_INDEPENDENT = {
    "objective": "Deliver independent work",
    "expected_outcome": "Both tasks run",
    "scope": ["work"], "exclusions": [], "assumptions": [], "risks": [],
    "strategy": "incremental",
    "acceptance_criteria": ["both run"],
    "verification_strategy": "pytest",
    "tasks": [
        {"id": "TASK-002", "title": "Second", "objective": "B",
         "deliverables": ["b"], "acceptance_criteria": ["b"],
         "depends_on": []},
        {"id": "TASK-001", "title": "First", "objective": "A",
         "deliverables": ["a"], "acceptance_criteria": ["a"],
         "depends_on": []},
    ],
}


def _independent_project(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    create_plan(aperio_dir, PLAN_INPUT_INDEPENDENT)
    approve_plan(aperio_dir)
    return aperio_dir


def test_runs_dispatch_preview_is_read_only_then_authorized_works(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _independent_project(run_cli, project_dir)
    marker = tmp_path / "child-ran.txt"
    script = f"open({str(marker)!r}, 'a').write('ran\\n')"
    config = _local_config(tmp_path, project_dir=project_dir, script=script)

    previews = runs_dispatch(
        aperio_dir, adapter_id="local-process", config=config,
        authorize_exec=False,
    )
    assert [p["status"] for p in previews] == ["preview", "preview"]
    assert [p["task_id"] for p in previews] == ["TASK-001", "TASK-002"]
    assert all("run_id" not in p for p in previews)
    assert read_executions(aperio_dir) is None
    assert not marker.exists()
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert {t["status"] for t in plan["tasks"]} == {"pending"}
    assert not (aperio_dir / "local/runs").exists()
    assert not (aperio_dir / "dispatch.lock").exists()

    results = runs_dispatch(
        aperio_dir, adapter_id="local-process", config=config,
        authorize_exec=True,
    )
    assert [r["task_id"] for r in results] == ["TASK-001", "TASK-002"]
    assert [r["run_id"] for r in results] == ["RUN-0001", "RUN-0002"]
    assert [r["status"] for r in results] == ["completed", "completed"]
    assert marker.read_text().count("ran") == 2
    document = read_executions(aperio_dir)
    assert [r["task_id"] for r in document["runs"]] == ["TASK-001", "TASK-002"]
    # Exit 0 never completes a task: verification stays manual.
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert {t["status"] for t in plan["tasks"]} == {"in-progress"}
    assert not (aperio_dir / "dispatch.lock").exists()


def test_runs_dispatch_manual_still_creates_handoffs(run_cli, project_dir):
    aperio_dir = _independent_project(run_cli, project_dir)
    results = runs_dispatch(
        aperio_dir, adapter_id="manual", config=None, authorize_exec=False,
    )
    assert [r["status"] for r in results] == ["awaiting-external", "awaiting-external"]
    for record in results:
        assert record["handoff_ref"] == f"runs/{record['run_id']}.handoff.md"
        assert read_run_handoff(aperio_dir, record["run_id"])


def test_cancel_during_process_startup_is_clean(
    run_cli, project_dir, tmp_path, monkeypatch
):
    """Cancelling while the child is starting must terminate/reap it, keep
    the cancelled status, and leave no lock or dangling claim behind."""
    from aperio import runtime as runtime_module

    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    original = runtime_module._flip_to_running

    def flip_then_cancel(dir_path, run_id, pid):
        runtime_module.cancel_run(dir_path, run_id, reason="test startup cancel")
        original(dir_path, run_id, pid)  # raises: run was cancelled

    monkeypatch.setattr(runtime_module, "_flip_to_running", flip_then_cancel)
    config = _local_config(
        tmp_path, project_dir=project_dir,
        script="import time; time.sleep(30)",
    )
    with pytest.raises(StateError, match="cancelled"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                      config=config, authorize_exec=True)
    document = read_executions(aperio_dir)
    assert document["runs"][0]["status"] == "cancelled"
    for lock in ("executions.lock", "dispatch.lock"):
        assert not (aperio_dir / lock).exists()


# --- stabilization: CLI error surfaces ----------------------------------------------------


def test_cli_operational_errors_are_actionable(run_cli, project_dir, tmp_path):
    """Existing log, invalid config, missing executable, unknown run and
    unreadable report file all return exit 1 with one clean line."""
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"

    # invalid config
    bad_config = _write_json(tmp_path, "bad.json", {"args": ["x"]})
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", bad_config,
        "--authorize-exec",
    )
    assert code == 1 and "Traceback" not in err and "aperio: error:" in err

    # missing executable
    missing = _local_config(
        tmp_path, project_dir=project_dir, script="print('x')",
        executable=str(tmp_path / "no-such-binary"),
    )
    missing_path = _write_config(tmp_path, "missing.json", missing)
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", missing_path,
        "--authorize-exec",
    )
    assert code == 1 and "Traceback" not in err
    assert "executable not found" in err

    # existing log collision (retry run id will be RUN-0002)
    log_dir = aperio_dir / "local" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "RUN-0002.log").write_text("PRESERVE-ME")
    good = _write_json(tmp_path, "good.json", _local_config(
        tmp_path, project_dir=project_dir, script="print('ok')"))
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", good,
        "--authorize-exec",
        "--remediation", "executable restored; retesting log collision",
    )
    assert code == 1 and "Traceback" not in err
    assert "already occupied" in err
    assert (log_dir / "RUN-0002.log").read_text() == "PRESERVE-ME"

    # unknown run
    code, out, err = run_cli("run-status", str(project_dir), "RUN-9999")
    assert code == 1 and "Traceback" not in err
    # unreadable report file
    code, out, err = run_cli(
        "run-report", str(project_dir), "RUN-0001",
        "--file", str(tmp_path / "absent.json"),
    )
    assert code == 1 and "Traceback" not in err


def test_cli_run_report_lifecycle(run_cli, project_dir, tmp_path):
    """Manual flow: dispatch -> handoff -> run-report -> verification is
    still pending (reporting never completes the task)."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "outcome_reported": "done", "success_reported": True,
    }))
    code, out, err = run_cli("run-report", str(project_dir),
                             record["run_id"], "--file", str(report))
    assert code == 0, err
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"
    code, out, err = run_cli("runs-reconcile", str(project_dir))
    assert code == 0, err
    assert not (aperio_dir / "executions.lock").exists()


# --- release-blocker patch: startup failure boundary -----------------------------------


def _manual_start_collision(aperio_dir: Path, run_id: str) -> Path:
    runs = aperio_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    target = runs / f"{run_id}.handoff.md"
    target.write_text("PRESERVE-ME")
    return target


def test_manual_handoff_symlinked_parent_fails_run_and_releases_claim(
    run_cli, project_dir, tmp_path
):
    """A manual handoff blocked by a symlinked .aperio/runs must finalize
    the run as failed (never leave it prepared), release this run's
    verified claim, and leave outside files untouched."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    os.symlink(outside, aperio_dir / "runs")
    with pytest.raises(PathError, match="symlink") as caught:
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    assert "returned to pending" in str(caught.value)
    assert "repair. The task claim" in str(caught.value)  # readable join
    assert list(outside.iterdir()) == []
    run = read_executions(aperio_dir)["runs"][0]
    assert run["run_id"] == "RUN-0001"
    assert run["status"] == "failed"  # never left 'prepared'
    assert "did not start" in run["outcome"]
    assert run["claim_released"] is True
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "pending"
    assert plan["tasks"][0]["owner"] is None
    assert not (aperio_dir / "executions.lock").exists()
    report = reconcile_runs(aperio_dir)
    assert report["orphaned_tasks"] == []
    assert report["recovery_required"] is False


def test_manual_handoff_collision_fails_preserves_file_and_recovers(
    run_cli, project_dir
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    occupied = _manual_start_collision(aperio_dir, "RUN-0001")
    with pytest.raises(StateError, match="already occupied") as caught:
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    assert "returned to pending" in str(caught.value)
    assert occupied.read_text() == "PRESERVE-ME"
    first = read_executions(aperio_dir)["runs"][0]
    assert first["status"] == "failed"
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "pending"
    # Explicit recovery: remove the obstruction and dispatch again.
    occupied.unlink()
    second = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False,
                           remediation="handoff collision removed")
    assert second["run_id"] == "RUN-0002"
    assert second["status"] == "awaiting-external"
    document = read_executions(aperio_dir)
    assert [r["status"] for r in document["runs"]] == [
        "failed", "awaiting-external",
    ]  # history preserved
    assert read_run_handoff(aperio_dir, "RUN-0002")


def test_manual_handoff_permission_denied_fails_cleanly(
    run_cli, project_dir
):
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("permission checks are meaningless as root")
    aperio_dir = _prepared_project(run_cli, project_dir)
    runs = aperio_dir / "runs"
    runs.mkdir()
    runs.chmod(0o500)
    try:
        code, out, err = run_cli(
            "run-start", str(project_dir), "TASK-001", "--agent", "manual",
        )
    finally:
        runs.chmod(0o700)
    assert code == 1
    assert "aperio: error:" in err and "Traceback" not in err
    assert "returned to pending" in err
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "failed"
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "pending"


def test_unverifiable_claim_is_reported_and_not_reversed(
    run_cli, project_dir, monkeypatch
):
    """When ownership/state cannot be positively verified, the claim is
    never reversed automatically; it is recorded as orphaned, reported by
    reconciliation, and recoverable through an explicit operator path."""
    from aperio import runtime as runtime_module

    aperio_dir = _prepared_project(run_cli, project_dir)
    plan_file = aperio_dir / "plan.json"
    original_plan = plan_file.read_bytes()

    def broken_start(self, *, run_id, aperio_dir, task, config, on_running=None):
        plan_file.write_text("{ not valid json")  # claim now unverifiable
        raise PathError("handoff directory is blocked")

    from aperio.adapters import ManualHandoffAdapter

    monkeypatch.setattr(ManualHandoffAdapter, "start", broken_start)
    with pytest.raises(PathError, match="orphaned claim"):
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "failed"
    assert run["claim_orphaned"] is True
    assert run["claim_released"] is False
    # Restore a readable plan with the claim this run held (the claim
    # itself survived: the failure only made it unverifiable).
    plan = json.loads(original_plan)
    plan["tasks"][0]["status"] = "in-progress"
    plan["tasks"][0]["owner"] = "manual:RUN-0001"
    plan_file.write_text(json.dumps(plan))
    report = reconcile_runs(aperio_dir)
    assert report["orphaned_tasks"] == ["TASK-001"]
    assert report["recovery_required"] is True
    # Explicit recovery path then allows a valid dispatch.
    monkeypatch.undo()
    update_task(aperio_dir, "TASK-001", new_status="blocked",
                reason="dispatch failed")
    update_task(aperio_dir, "TASK-001", new_status="pending")
    result = dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                           config=None, authorize_exec=False,
                           remediation="claim obstruction resolved via "
                                       "explicit task-status recovery")
    assert result["status"] == "awaiting-external"
    assert read_run_handoff(aperio_dir, result["run_id"])


def test_manual_startup_failure_never_overwrites_cancelled_run(
    run_cli, project_dir, monkeypatch
):
    from aperio import runtime as runtime_module

    aperio_dir = _prepared_project(run_cli, project_dir)

    def cancel_then_fail(self, *, run_id, aperio_dir, task, config, on_running=None):
        runtime_module.cancel_run(aperio_dir, run_id, reason="startup cancel")
        raise PathError("handoff blocked after cancellation")

    from aperio.adapters import ManualHandoffAdapter

    monkeypatch.setattr(ManualHandoffAdapter, "start", cancel_then_fail)
    with pytest.raises(PathError) as caught:
        dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                      config=None, authorize_exec=False)
    assert "already cancelled" in str(caught.value)
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "cancelled"  # precedence preserved
    assert "finalize after cancellation ignored" in run["outcome_note"]
    assert "claim_released" not in run
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"  # cancel never edits claims


def test_reconcile_cancels_prepared_run_without_a_claim(run_cli, project_dir):
    """A prepared record whose task is no longer claimed (crash after a
    startup failure released it) is cancelled, never left blocking."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    update_task(aperio_dir, "TASK-001", new_status="blocked",
                reason="simulated release")
    update_task(aperio_dir, "TASK-001", new_status="pending")
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "prepared"
    exec_file.write_text(json.dumps(document))
    report = reconcile_runs(aperio_dir)
    assert report["cancelled_stale"] == ["RUN-0001"]
    assert report["orphaned_tasks"] == []
    assert read_executions(aperio_dir)["runs"][0]["status"] == "cancelled"


def test_subprocess_startup_oserror_is_actionable_and_secret_free(
    run_cli, project_dir, tmp_path, monkeypatch
):
    """A non-FileNotFound OSError during Popen is normalized into an
    Aperio error: no traceback, no raw config value in the message, the
    record, or portable memory; the run is failed and the claim released."""
    import errno

    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"

    def boom(*args, **kwargs):
        raise OSError(errno.ENOMEM, "Cannot allocate memory")

    monkeypatch.setattr("aperio.adapters.subprocess.Popen", boom)
    secret = SYNTHETIC_CONFIG_SECRET
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('never')",
        args=["-c", "print('never')", secret],
    )
    config_path = _write_config(tmp_path, "cfg.json", config)
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", config_path,
        "--authorize-exec",
    )
    assert code == 1
    assert "could not be started" in err
    assert "Cannot allocate memory" in err
    assert "Traceback" not in err
    assert secret not in err and secret not in out
    records = (aperio_dir / "executions.json").read_text()
    assert secret not in records
    assert str(tmp_path) not in records
    assert secret not in json.dumps(execution_summary(aperio_dir))
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "failed"
    assert run["claim_released"] is True
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "pending"
