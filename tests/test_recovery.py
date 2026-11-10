"""Bounded retry with problem signatures: gate, ledger derivation,
signature stability, recovery report, and CLI surface."""

from __future__ import annotations

import json

import pytest

from aperio.errors import StateError, ValidationError
from aperio.recovery import (
    MAX_ATTEMPTS_PER_SIGNATURE,
    attach_failure_signature,
    failure_counts,
    failure_signature,
    normalize_signature_text,
    recovery_report,
    retry_gate,
    signature_of_run,
)
from aperio.runtime import dispatch_task, read_executions
from test_execution import (
    PLAN_INPUT,
    _local_config,
    _prepared_project,
)


def _fail(aperio_dir, tmp_path, project_dir, *, script='import sys; sys.exit(9)',
          remediation=None, task_id="TASK-001", adapter_id="local-process"):
    config = _local_config(
        tmp_path, project_dir=project_dir, script=script)
    return dispatch_task(
        aperio_dir, task_id, adapter_id=adapter_id, config=config,
        authorize_exec=True, remediation=remediation)


# --- signature primitives -------------------------------------------------------------


def test_signature_is_stable_and_keyed_on_recorded_facts():
    a = failure_signature("TASK-001", "local-process",
                          outcome="exit code 9", exit_code=9)
    b = failure_signature("TASK-001", "local-process",
                          outcome="exit  code 9\n", exit_code=9)
    assert a == b  # whitespace-normalized: same problem, same signature
    different_task = failure_signature("TASK-002", "local-process",
                                       outcome="exit code 9", exit_code=9)
    different_exit = failure_signature("TASK-001", "local-process",
                                       outcome="exit code 9", exit_code=1)
    different_text = failure_signature("TASK-001", "local-process",
                                       outcome="boom", exit_code=9)
    assert len({a, different_task, different_exit, different_text}) == 4


def test_normalize_signature_text_bounds_and_flattens():
    text = "a\n\n  b\tc   " + "x" * 500
    normalized = normalize_signature_text(text)
    assert "\n" not in normalized and "\t" not in normalized
    assert len(normalized) <= 280


def test_signature_of_run_rederives_for_legacy_records():
    legacy = {"task_id": "TASK-001", "adapter_id": "local-process",
              "status": "failed", "outcome": "exit code 9", "exit_code": 9}
    assert signature_of_run(legacy) == failure_signature(
        "TASK-001", "local-process", outcome="exit code 9", exit_code=9)
    stamped = dict(legacy, failure_signature="deadbeefdeadbeef")
    assert signature_of_run(stamped) == "deadbeefdeadbeef"


def test_failure_counts_groups_by_signature():
    runs = [
        {"task_id": "T", "status": "failed", "outcome": "a", "exit_code": 1},
        {"task_id": "T", "status": "failed", "outcome": "a", "exit_code": 1},
        {"task_id": "T", "status": "failed", "outcome": "b", "exit_code": 2},
        {"task_id": "T", "status": "completed", "outcome": "a", "exit_code": 0},
        {"task_id": "OTHER", "status": "failed", "outcome": "a", "exit_code": 1},
    ]
    counts = failure_counts(runs, "T")
    assert sorted(counts.values()) == [1, 2]
    assert sum(counts.values()) == 3


# --- the dispatch gate ----------------------------------------------------------------


def test_fresh_task_dispatches_without_remediation(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    record = _fail(aperio_dir, tmp_path, project_dir,
                   script="print('first try')")
    assert record["status"] == "completed"


def test_retry_without_remediation_refused(run_cli, project_dir, tmp_path):
    aperio_dir = _prepared_project(run_cli, project_dir)
    failed = _fail(aperio_dir, tmp_path, project_dir)
    assert failed["status"] == "failed"
    with pytest.raises(ValidationError, match="must declare what changed"):
        _fail(aperio_dir, tmp_path, project_dir)


def test_retry_records_remediation_and_attempt(run_cli, project_dir, tmp_path):
    aperio_dir = _prepared_project(run_cli, project_dir)
    failed = _fail(aperio_dir, tmp_path, project_dir)
    assert failed["failure_signature"]
    assert failed["failure_attempt"] == 1
    retry = _fail(aperio_dir, tmp_path, project_dir,
                  remediation="changed script to exit 3 instead")
    assert retry["status"] == "failed"
    assert retry["remediation_note"] == "changed script to exit 3 instead"
    assert retry["retry_attempt_no"] == 2
    assert retry["retry_signature"] == failed["failure_signature"]
    document = read_executions(aperio_dir)
    counts = failure_counts(document["runs"], "TASK-001")
    assert counts[retry["failure_signature"]] == 2


def test_repeated_remediation_note_refused_as_not_distinct(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    _fail(aperio_dir, tmp_path, project_dir,
          remediation="tried the same thing again")
    with pytest.raises(ValidationError, match="materially distinct"):
        _fail(aperio_dir, tmp_path, project_dir,
              remediation="tried the same thing again")
    # whitespace-equivalent repetition is refused too
    with pytest.raises(ValidationError, match="materially distinct"):
        _fail(aperio_dir, tmp_path, project_dir,
              remediation="  tried the same thing again  ")


def test_budget_exhaustion_refuses_fourth_attempt_with_escalation(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _prepared_project(run_cli, project_dir)
    _fail(aperio_dir, tmp_path, project_dir)
    _fail(aperio_dir, tmp_path, project_dir,
          remediation="attempt 2: new hypothesis")
    _fail(aperio_dir, tmp_path, project_dir,
          remediation="attempt 3: different evidence examined")
    with pytest.raises(StateError, match="budget is exhausted") as caught:
        _fail(aperio_dir, tmp_path, project_dir,
              remediation="attempt 4: another idea")
    message = str(caught.value)
    assert "task-status" in message and "blocked" in message
    assert "recovery-report" in message
    assert "agent-doctor" in message


def test_budget_is_per_signature_not_per_task(run_cli, project_dir, tmp_path):
    """A materially different failure is a NEW problem with its own
    budget: the gate keys on the latest failure's signature."""
    aperio_dir = _prepared_project(run_cli, project_dir)
    _fail(aperio_dir, tmp_path, project_dir)  # exit 9
    _fail(aperio_dir, tmp_path, project_dir,
          remediation="hypothesis: wrong exit path",
          script="import sys; raise SystemExit(3)")
    third = _fail(aperio_dir, tmp_path, project_dir,
                  remediation="hypothesis: env problem",
                  script="import sys; print('boom'); raise SystemExit(7)")
    # a different outcome text = a different signature
    assert third["failure_attempt"] == 1
    assert MAX_ATTEMPTS_PER_SIGNATURE == 3


def test_fresh_task_gate_contract(run_cli, project_dir):
    """A fresh task (no failure history) passes the gate with attempt 1."""
    gate = retry_gate([], "TASK-001", remediation=None)
    assert gate == {"signature": None, "attempt_no": 1, "prior_failures": 0}


def test_runs_dispatch_stops_on_gated_task(run_cli, project_dir, tmp_path):
    """Auto-scheduling surfaces an exhausted/gated task instead of
    silently looping or skipping it."""
    from aperio.runtime import runs_dispatch

    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    _fail(aperio_dir, tmp_path, project_dir)  # one prior failure
    with pytest.raises(ValidationError, match="must declare what changed"):
        runs_dispatch(aperio_dir, adapter_id="local-process",
                      config=_local_config(
                          tmp_path, project_dir=project_dir,
                          script="print('ok')"),
                      authorize_exec=True)


# --- attach + report -------------------------------------------------------------------


def test_attach_failure_signature_counts_prior():
    prior = [
        {"task_id": "T", "status": "failed", "outcome": "boom",
         "exit_code": 2, "adapter_id": "local-process"},
    ]
    fields = attach_failure_signature(
        prior, task_id="T", adapter_id="local-process",
        outcome="boom", exit_code=2)
    assert fields == {"failure_signature": failure_signature(
        "T", "local-process", outcome="boom", exit_code=2),
        "failure_attempt": 2}
    fresh = attach_failure_signature(
        prior, task_id="T", adapter_id="local-process",
        outcome="different", exit_code=2)
    assert fresh["failure_attempt"] == 1


def test_recovery_report_groups_and_verdicts():
    runs = [
        {"run_id": "RUN-0001", "task_id": "T", "status": "failed",
         "adapter_id": "local-process", "outcome": "exit code 9",
         "exit_code": 9, "finished_at": "2026-01-01T00:00:00Z"},
        {"run_id": "RUN-0002", "task_id": "T", "status": "failed",
         "adapter_id": "local-process", "outcome": "exit code 9",
         "exit_code": 9, "finished_at": "2026-01-01T00:01:00Z",
         "remediation_note": "hypothesis two"},
        {"run_id": "RUN-0003", "task_id": "OTHER", "status": "failed",
         "adapter_id": "local-process", "outcome": "x", "exit_code": 1},
    ]
    report = recovery_report(runs, "T")
    assert report["failed_run_count"] == 2
    assert report["verdict"] == "failed_runs_present"
    entry = report["signatures"][0]
    assert entry["attempts"] == 2 and entry["exhausted"] is False
    assert entry["runs"][1]["remediation_note"] == "hypothesis two"
    # three raw failed runs sharing one recorded outcome = one exhausted
    # signature (raw records keep task_id so signatures re-derive)
    exhausted_runs = runs[:2] + [dict(runs[0], run_id="RUN-0009")]
    exhausted_report = recovery_report(exhausted_runs, "T")
    assert exhausted_report["verdict"] == "exhausted"
    assert any("blocked" in line for line in exhausted_report["guidance"])
    assert recovery_report(runs, "NOPE")["verdict"] == "no_failed_runs"


# --- CLI surface ------------------------------------------------------------------------


def test_cli_recovery_report_json_and_text(run_cli, project_dir, tmp_path):
    _prepared_project(run_cli, project_dir)
    aperio_dir = project_dir / ".aperio"
    _fail(aperio_dir, tmp_path, project_dir)
    code, out, err = run_cli(
        "recovery-report", str(project_dir), "TASK-001", "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["failed_run_count"] == 1
    assert report["signatures"][0]["runs"][0]["exit_code"] == 9

    code, out, _ = run_cli("recovery-report", str(project_dir), "TASK-001")
    assert code == 0
    assert "problem signature" in out and "exit=9" in out

    code, out, err = run_cli(
        "recovery-report", str(project_dir), "TASK-001", "--json")
    assert code == 0 and json.loads(out)["verdict"] == "failed_runs_present"


def test_cli_run_start_refuses_retry_without_remediation(
    run_cli, project_dir, tmp_path
):
    _prepared_project(run_cli, project_dir)
    config = _local_config(
        tmp_path, project_dir=project_dir, script="import sys; sys.exit(9)")
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config))
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", str(config_path),
        "--authorize-exec",
    )
    assert code == 0, err
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", "local-process", "--config", str(config_path),
        "--authorize-exec",
    )
    assert code == 1 and "must declare what changed" in err


def test_gate_contract_constants_documented():
    assert MAX_ATTEMPTS_PER_SIGNATURE == 3
    assert PLAN_INPUT is not None  # helper import sanity
