"""Feature 04: agent execution runtime — eligibility, adapters, records,
concurrency, recovery, and CLI."""

from __future__ import annotations

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.planstore import approve_plan, create_plan, update_task
from aperio.planstore import plan_summary  # noqa: F401  (integration probe)
from aperio.runtime import (
    cancel_run,
    dispatch_task,
    execution_summary,
    read_executions,
    reconcile_runs,
    record_external_result,
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
    config = _write_config(tmp_path, "cfg.json", _local_config(
        tmp_path, project_dir=project_dir, script="print('hello')"))
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=False)
    assert record["status"] == "prepared"  # preparation only
    assert record["outcome"] and "no --authorize-exec" in record["outcome"]
    # No local log: nothing executed.
    assert (aprio := project_dir / ".aperio/local/runs").exists() is False


def test_local_process_execution_success(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    config = _local_config(
        tmp_path, project_dir=project_dir, script="print('hello from child')")
    record = dispatch_task(aperio_dir, "TASK-001", adapter_id="local-process",
                           config=config, authorize_exec=True)
    assert record["status"] == "completed"
    assert record["exit_code"] == 0
    assert "hello from child" in record["diagnostics"]
    # Raw output mirrored under local/runs/ (machine-local).
    log = project_dir / ".aperio/local/runs/RUN-0001.log"
    assert log.is_file() and b"hello from child" in log.read_bytes()
    # ...but the shareable record keeps only the sanitized tail.
    assert record["diagnostics"].count("hello from child") >= 1
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
    assert "boom" in record["diagnostics"]


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
    child_env = json.loads(record["diagnostics"].split("[stdout tail]\n")[-1]
                           .split("[stderr tail]")[0].strip())
    assert child_env["APERIO_SECRET_TEST"] is None  # minimal env: not passed
    assert child_env["PATH"]  # lookup vars are present


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
                          previous_run_id="RUN-0001")
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
    assert summary["recovery_required"] is False


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


def test_cli_run_start_without_authorization_is_preparation(
    run_cli, project_dir, tmp_path
):
    _prepared_project(run_cli, project_dir)
    config = _write_json(tmp_path, "cfg.json", _local_config(
        tmp_path, project_dir=project_dir, script="print('x')"))
    code, out, err = run_cli("run-start", str(project_dir), "TASK-001",
                             "--agent", "local-process", "--config", config)
    assert code == 0, err
    record = read_executions(project_dir / ".aperio")["runs"][0]
    assert record["status"] == "prepared"
    assert not (project_dir / ".aperio/local/runs").exists()
