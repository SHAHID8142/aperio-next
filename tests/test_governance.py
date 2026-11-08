"""Feature 06: workflow roles, policy governance, and review gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aperio.errors import StateError
from aperio.integrity import check_integrity
from aperio.memory import load_context
from aperio.planstore import approve_plan, create_plan, read_plan, revise_plan
from aperio.policy import assign_actor, read_policy
from aperio.portable import export_project, import_archive
from aperio.reviews import decide_review, list_reviews, submit_review

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plan(task_id="TASK-001", title="governed task") -> dict:
    return {
        "objective": "govern the workflow",
        "expected_outcome": "attributed approvals",
        "scope": ["governance"],
        "exclusions": [],
        "assumptions": [],
        "risks": [],
        "strategy": "roles + reviews",
        "acceptance_criteria": ["independent review"],
        "verification_strategy": "pytest",
        "tasks": [
            {
                "id": task_id,
                "title": title,
                "objective": "prove the gate",
                "deliverables": ["evidence"],
                "acceptance_criteria": ["reviewed"],
                "depends_on": [],
            }
        ],
    }


def _write_plan(tmp_path: Path, plan: dict, name="plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(plan))
    return path


def _governed_project(run_cli, project_dir, tmp_path) -> Path:
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    code, _, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "r:reviewer", "--assign", "r2:reviewer",
    )
    assert code == 0, err
    plan_file = _write_plan(tmp_path, _plan())
    code, _, err = run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file),
        "--actor", "p",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "plan-approve", str(project_dir), "--actor", "r"
    )
    assert code == 0, err
    return project_dir / ".aperio"


def _claim(run_cli, project_dir, actor="i"):
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", actor, "--actor", actor,
    )
    assert code == 0, err
    return out


def _submit(run_cli, project_dir, actor="i", task="TASK-001"):
    code, out, err = run_cli(
        "review-submit", str(project_dir),
        "--task", task, "--actor", actor,
        "--scope", "acceptance criteria", "--evidence", "pytest green",
    )
    assert code == 0, err
    return out


def _complete(run_cli, project_dir, actor="i"):
    return run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "completed", "--evidence", "pytest green",
        "--limitations", "none", "--result", "passed", "--actor", actor,
    )


# --- legacy compatibility -----------------------------------------------------


def test_project_without_policy_keeps_legacy_behavior(run_cli, project_dir):
    run_cli("init", str(project_dir))
    plan_file = _write_plan(project_dir, _plan())
    code, _, err = run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file)
    )
    assert code == 0, err
    code, _, err = run_cli("plan-approve", str(project_dir))
    assert code == 0, err
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "any-agent",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "completed", "--evidence", "done",
        "--limitations", "none", "--result", "passed",
    )
    assert code == 0, err
    assert load_context(project_dir).governance["enabled"] is False


def test_policy_init_is_explicit_and_never_replaces(
    run_cli, project_dir, tmp_path
):
    run_cli("init", str(project_dir))
    code, _, err = run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner"
    )
    assert code == 0, err
    code, _, err = run_cli(
        "policy-init", str(project_dir), "--assign", "q:planner"
    )
    assert code == 1 and "already exists" in err
    policy = read_policy(project_dir / ".aperio")
    assert policy["enabled"] is True and policy["actors"]["p"]["role"] == "planner"


def test_roles_matrix_cli(run_cli):
    code, out, err = run_cli("roles", "--json")
    assert code == 0, err
    matrix = json.loads(out)
    assert set(matrix) == {"planner", "implementer", "reviewer"}
    assert "review.decide" in matrix["reviewer"]["actions"]
    assert "review.decide" not in matrix["implementer"]["actions"]


# --- role permissions and attribution ----------------------------------------


def test_unknown_and_missing_actors_are_refused(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    code, _, err = run_cli("plan-approve", str(project_dir))
    assert code == 1 and "--actor" in err
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "ghost", "--actor", "ghost",
    )
    assert code == 1 and "not assigned" in err
    code, _, err = run_cli(
        "policy-permissions", str(project_dir), "--actor", "ghost"
    )
    assert code == 1 and "unknown actors" in err


def test_role_permissions_are_enforced_not_declared(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    # Implementer cannot approve or author plans.
    code, _, err = run_cli(
        "plan-approve", str(project_dir), "--actor", "i"
    )
    assert code == 1 and "not permitted" in err
    code, _, err = run_cli(
        "plan-revise", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan(), "r.json")),
        "--revises", "1", "--actor", "i",
    )
    assert code == 1 and "not permitted" in err
    # Reviewer cannot claim implementation work.
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "r", "--actor", "r",
    )
    assert code == 1 and "not permitted" in err


def test_planner_cannot_approve_own_revision(
    run_cli, project_dir, tmp_path
):
    run_cli("init", str(project_dir))
    run_cli("policy-init", str(project_dir), "--assign", "p:planner")
    plan_file = _write_plan(tmp_path, _plan())
    assert run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file),
        "--actor", "p",
    )[0] == 0
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "p")
    assert code == 1 and "separation of duties" in err
    assert read_plan(project_dir / ".aperio")["approval"]["status"] == "draft"


def test_material_revision_invalidates_prior_approval_and_review(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    revised = _plan(title="changed contract")
    code, _, err = run_cli(
        "plan-revise", str(project_dir), "--file",
        str(_write_plan(tmp_path, revised, "r2.json")),
        "--revises", "2", "--actor", "p",
    )
    assert code == 0, err
    assert read_plan(aperio_dir)["approval"]["status"] == "draft"
    # A material revision requires renewed approval before any decision.
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "approved", "--findings", "ok",
    )
    assert code == 1 and "approved plan revision" in err
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 0, err
    # The prior review is now stale against the new material revision.
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "approved", "--findings", "ok",
    )
    assert code == 1 and "stale" in err
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "current material revision 3" in err


def test_self_review_after_role_change_is_refused(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir, actor="i")
    _submit(run_cli, project_dir, actor="i")
    code, _, err = run_cli(
        "policy-assign", str(project_dir), "i", "--role", "reviewer"
    )
    assert code == 0, err
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "i", "--outcome", "approved", "--findings", "self",
    )
    assert code == 1 and "may not decide their own submission" in err
    # Role change preserved historical attribution.
    policy = read_policy(project_dir / ".aperio")
    assert policy["actor_history"][0]["from_role"] == "implementer"
    assert policy["actor_history"][0]["to_role"] == "reviewer"


# --- review lifecycle ---------------------------------------------------------


def test_independent_review_then_completion(run_cli, project_dir, tmp_path):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    # Execution success/evidence alone is not independent verification.
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "approved independent review" in err
    _submit(run_cli, project_dir)
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "approved", "--findings", "criteria met",
    )
    assert code == 0, err
    code, out, err = _complete(run_cli, project_dir)
    assert code == 0, err
    assert "completed" in out
    reviews = list_reviews(aperio_dir, task_id="TASK-001")
    assert [r["outcome"] for r in reviews] == ["approved"]
    assert reviews[0]["reviewer"] == "r"
    assert reviews[0]["submitted_by"] == "i"


def test_changes_requested_requires_new_review_attempt(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "changes-requested",
        "--findings", "missing edge case",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "latest review REV-0001 is 'changes-requested'" in err
    _submit(run_cli, project_dir)
    reviews = list_reviews(aperio_dir, task_id="TASK-001")
    assert [r["review_id"] for r in reviews] == ["REV-0001", "REV-0002"]
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0002",
        "--actor", "r2", "--outcome", "approved", "--findings", "fixed",
    )
    assert code == 0, err
    assert _complete(run_cli, project_dir)[0] == 0


def test_rejected_review_keeps_reason_and_is_immutable(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "rejected", "--findings", "unsafe design",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r2", "--outcome", "approved", "--findings", "override",
    )
    assert code == 1 and "immutable" in err
    stored = list_reviews(aperio_dir, task_id="TASK-001")[0]
    assert stored["outcome"] == "rejected"
    assert stored["findings"] == "unsafe design"
    assert _complete(run_cli, project_dir)[0] == 1


def test_review_submission_requires_in_progress_task(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    code, _, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "s", "--evidence", "e",
    )
    assert code == 1 and "only an in-progress" in err
    code, _, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-999",
        "--actor", "i", "--scope", "s", "--evidence", "e",
    )
    assert code == 1 and "not part of the current plan" in err


# --- memory and portability --------------------------------------------------


def test_memory_governance_block_is_additive_and_complete(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    data = load_context(project_dir).to_dict()
    governance = data["governance"]
    assert governance["enabled"] is True
    assert governance["policy_version"] == 1
    assert governance["actors"]["r"] == "reviewer"
    assert [item["review_id"] for item in governance["pending_reviews"]] == [
        "REV-0001"
    ]
    assert governance["blocking_requirements"] == [
        {
            "task_id": "TASK-001",
            "requirement": "approved_independent_review",
            "plan_revision": 1,
        }
    ]


def test_export_import_preserves_policy_reviews_and_audit(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    code, _, err = run_cli(
        "review-decide", str(project_dir), "REV-0001",
        "--actor", "r", "--outcome", "approved", "--findings", "ok",
    )
    assert code == 0, err
    archive = tmp_path / "gov.zip"
    export_project(str(project_dir), output=str(archive))
    destination = tmp_path / "other"
    report = import_archive(str(destination), str(archive))
    assert "policies.json" in report["created"]
    assert "reviews.json" in report["created"]
    assert "policy-audit.json" in report["created"]
    copied = destination / ".aperio"
    assert read_policy(copied)["actors"]["i"]["role"] == "implementer"
    assert list_reviews(copied)[0]["outcome"] == "approved"
    assert check_integrity(str(destination))["ok"] is True
    assert read_policy(copied)["policy_version"] == read_policy(aperio_dir)[
        "policy_version"
    ]


def test_future_policy_schema_is_preserved_on_export_but_refused_on_import(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    policy_file = aperio_dir / "policies.json"
    policy = json.loads(policy_file.read_text())
    policy["schema_version"] = 99
    policy_file.write_text(json.dumps(policy))
    archive = tmp_path / "future.zip"
    export_project(str(project_dir), output=str(archive))
    with pytest.raises(StateError, match="newer than supported"):
        import_archive(str(tmp_path / "dest"), str(archive))
    assert not (tmp_path / "dest" / ".aperio" / "state.json").exists()


def test_malformed_and_symlinked_policy_are_reported(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _guarded = _governed_project(
        run_cli, project_dir, tmp_path
    )
    (aperio_dir / "policies.json").write_text("{not json")
    report = check_integrity(str(project_dir))
    assert report["ok"] is False
    assert any(item["code"] == "policy-invalid" for item in report["errors"])

    (aperio_dir / "policies.json").unlink()
    outside = tmp_path / "outside-policy.json"
    outside.write_text(json.dumps({"schema_version": 1}))
    os.symlink(outside, aperio_dir / "policies.json")
    report = check_integrity(str(project_dir))
    assert any(item["code"] == "symlink-file" for item in report["errors"])


def test_review_for_missing_task_rejected_by_integrity(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    reviews_file = aperio_dir / "reviews.json"
    document = json.loads(reviews_file.read_text())
    document["reviews"][0]["target"]["task_id"] = "TASK-404"
    reviews_file.write_text(json.dumps(document))
    report = check_integrity(str(project_dir))
    assert any(item["code"] == "review-orphan-task" for item in report["errors"])


# --- concurrency --------------------------------------------------------------


def _start_cli(*args: str):
    return subprocess.Popen(
        [sys.executable, "-m", "aperio", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT),
    )


def _finish(proc, label):
    out, err = proc.communicate(timeout=60)
    assert "Traceback" not in err, f"{label} crashed:\n{err}"
    if proc.returncode != 0:
        assert "aperio: error:" in err, f"{label} failed uncleanly:\n{err}"
    return proc.returncode, out, err


def test_concurrent_submissions_receive_unique_review_ids(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    procs = [
        _start_cli(
            "review-submit", str(project_dir), "--task", "TASK-001",
            "--actor", "i", "--scope", "s", "--evidence", "e",
        )
        for _ in range(2)
    ]
    results = [_finish(proc, "review-submit") for proc in procs]
    assert all(code == 0 for code, _, _ in results), results
    ids = [review["review_id"] for review in list_reviews(aperio_dir)]
    assert len(ids) == len(set(ids)) == 2
    assert check_integrity(str(project_dir))["ok"] is True


def test_concurrent_decisions_allow_only_one_immutable_outcome(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    procs = [
        _start_cli(
            "review-decide", str(project_dir), "REV-0001",
            "--actor", actor, "--outcome", "approved", "--findings", "ok",
        )
        for actor in ("r", "r2")
    ]
    results = [_finish(proc, "review-decide") for proc in procs]
    codes = sorted(code for code, _, _ in results)
    assert codes == [0, 1], results
    stored = list_reviews(aperio_dir)[0]
    assert stored["outcome"] == "approved"
    assert stored["reviewer"] in ("r", "r2")
    assert check_integrity(str(project_dir))["ok"] is True


def test_concurrent_policy_change_and_review_submit_stay_consistent(
    run_cli, project_dir, tmp_path
):
    _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    assign = _start_cli(
        "policy-assign", str(project_dir), "r2", "--role", "planner"
    )
    submit = _start_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "s", "--evidence", "e",
    )
    _finish(assign, "policy-assign")
    _finish(submit, "review-submit")
    policy = read_policy(project_dir / ".aperio")
    assert policy["actors"]["r2"]["role"] == "planner"
    assert check_integrity(str(project_dir))["ok"] is True


# --- API-level attribution ----------------------------------------------------


def test_api_stale_review_cannot_approve_newer_plan(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    record = submit_review(
        aperio_dir,
        actor="i",
        task_id="TASK-001",
        scope=["criteria"],
        evidence=["pytest"],
    )
    revise_plan(
        aperio_dir, _plan(title="new contract"), revises=2, actor="p"
    )
    approve_plan(aperio_dir, actor="r")
    with pytest.raises(StateError, match="stale"):
        decide_review(
            aperio_dir, record["review_id"], actor="r2",
            outcome="approved", findings="ok",
        )
    # A fresh review keyed to revision 2 can be approved and completed.
    second = submit_review(
        aperio_dir,
        actor="i",
        task_id="TASK-001",
        scope=["criteria v2"],
        evidence=["pytest"],
    )
    decide_review(
        aperio_dir, second["review_id"], actor="r2",
        outcome="approved", findings="ok",
    )
    from aperio.planstore import update_task

    task = update_task(
        aperio_dir, "TASK-001", new_status="completed",
        evidence="pytest", limitations="none", result="passed", actor="i",
    )
    assert task["status"] == "completed"


def test_plan_author_and_actor_are_recorded_in_audit(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    from aperio.policy import audit_path

    events = json.loads(audit_path(aperio_dir).read_text())["events"]
    actions = [event["action"] for event in events]
    assert "policy.init" in actions
    assert "plan.create" in actions
    assert "plan.approve" in actions
    plan_event = next(
        event for event in events if event["action"] == "plan.create"
    )
    assert plan_event["actor"] == "p"
    assert plan_event["role"] == "planner"
    assert plan_event["revision"] == 1
    # No raw paths or environment content in the audit log.
    serialized = json.dumps(events)
    assert str(project_dir) not in serialized
    assert "pytest" not in serialized


def test_dispatch_is_governed_and_success_is_not_verification(
    run_cli, project_dir, tmp_path
):
    from aperio.runtime import dispatch_task, record_external_result

    aperio_dir = _governed_project(run_cli, project_dir, tmp_path)
    with pytest.raises(StateError, match="--actor"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id="manual", config=None,
            authorize_exec=False,
        )
    with pytest.raises(StateError, match="not permitted"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id="manual", config=None,
            authorize_exec=False, actor="r",
        )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual", config=None,
        authorize_exec=False, actor="i",
    )
    assert record["status"] == "awaiting-external"
    completed = record_external_result(
        aperio_dir, record["run_id"],
        {"outcome_reported": "all tests passed", "success_reported": True},
    )
    assert completed["status"] == "completed"
    # A successful external report is evidence, not independent review.
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "approved independent review" in err
    from aperio.policy import audit_path

    actions = [
        event["action"]
        for event in json.loads(audit_path(aperio_dir).read_text())["events"]
    ]
    assert "execution.dispatch" in actions
    assert "task.claim" not in actions  # dispatch claims under task.status
