"""Feature 06 final safety gate: attribution, attempt binding, recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aperio.errors import AperioError, StateError
from aperio.integrity import check_integrity
from aperio.planstore import (
    adopt_plan_author,
    approve_plan,
    create_plan,
    release_failed_claim,
    revise_plan,
    sync_plan_authors,
    update_task,
)
from aperio.policy import append_event, audit_path, read_policy
from aperio.portable import export_project, import_archive
from aperio.reviews import (
    decide_review,
    list_reviews,
    read_reviews,
    submit_review,
    validate_reviews_document,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plan(title="safety gate") -> dict:
    return {
        "objective": "prove the safety gate",
        "expected_outcome": "durable attribution",
        "scope": ["governance"],
        "exclusions": [],
        "assumptions": [],
        "risks": [],
        "strategy": "bind attempts",
        "acceptance_criteria": ["independent review"],
        "verification_strategy": "pytest",
        "tasks": [
            {
                "id": "TASK-001",
                "title": title,
                "objective": "prove it",
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


def _governed(run_cli, project_dir, tmp_path) -> Path:
    assert run_cli("init", str(project_dir))[0] == 0
    code, _, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "i2:implementer", "--assign", "r:reviewer",
        "--assign", "r2:reviewer",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "plan-create", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan())), "--actor", "p",
    )
    assert code == 0, err
    assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0
    return project_dir / ".aperio"


def _claim(run_cli, project_dir, actor="i"):
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", actor, "--actor", actor,
    )
    assert code == 0, err


def _submit(run_cli, project_dir, actor="i", evidence="pytest"):
    code, out, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", actor, "--scope", "criteria", "--evidence", evidence,
    )
    assert code == 0, err
    return out


def _decide(run_cli, project_dir, review_id="REV-0001", actor="r",
            outcome="approved", findings="ok"):
    return run_cli(
        "review-decide", str(project_dir), review_id, "--actor", actor,
        "--outcome", outcome, "--findings", findings,
    )


def _complete(run_cli, project_dir, evidence="pytest"):
    return run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "completed",
        "--evidence", evidence, "--limitations", "none", "--result", "passed",
        "--actor", "i",
    )


def _approved_task(run_cli, project_dir, tmp_path) -> Path:
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    assert _decide(run_cli, project_dir)[0] == 0
    return aperio_dir


# --- 1. fail-closed plan attribution -----------------------------------------


def test_audit_failure_after_publication_blocks_then_syncs(
    run_cli, project_dir, tmp_path, monkeypatch
):
    import aperio.planstore as planstore_module

    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner",
        "--assign", "r:reviewer",
    )[0] == 0
    original = planstore_module._policy_event

    def failing(*args, **kwargs):
        if kwargs.get("action") == "plan.create":
            raise StateError("simulated audit write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(planstore_module, "_policy_event", failing)
    with pytest.raises(StateError, match="durable author attribution"):
        create_plan(
            project_dir / ".aperio", _plan(),
            actor="p",
        )
    plan = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    assert plan["authors"]["1"]["actor"] == "p"
    monkeypatch.setattr(planstore_module, "_policy_event", original)
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 1 and "no matching audit record" in err
    assert run_cli("plan-sync-authors", str(project_dir), "--actor", "p")[0] == 0
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 0, err


def test_audit_failure_after_revision_is_recoverable(
    run_cli, project_dir, tmp_path, monkeypatch
):
    import aperio.planstore as planstore_module

    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    original = planstore_module._policy_event

    def failing(*args, **kwargs):
        if kwargs.get("action") == "plan.revise":
            raise StateError("simulated audit failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(planstore_module, "_policy_event", failing)
    with pytest.raises(StateError, match="run 'aperio plan-sync-authors"):
        revise_plan(
            aperio_dir, _plan("revised"), revises=1, actor="p"
        )
    monkeypatch.setattr(planstore_module, "_policy_event", original)
    assert read_policy(aperio_dir)["enabled"] is True
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 1 and "no matching audit record" in err
    assert run_cli("plan-sync-authors", str(project_dir), "--actor", "p")[0] == 0
    assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0


def test_missing_attribution_requires_explicit_adoption(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "plan-create", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan())),
    )[0] == 0
    assert run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner",
        "--assign", "r:reviewer",
    )[0] == 0
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 1 and "no durable author attribution" in err
    # Adoption is explicit and requires the planner role.
    code, _, err = run_cli(
        "plan-adopt", str(project_dir), "--author", "p",
        "--reason", "created before governance", "--actor", "r",
    )
    assert code == 1 and "not permitted" in err
    assert run_cli(
        "plan-adopt", str(project_dir), "--author", "p",
        "--reason", "created before governance", "--actor", "p",
    )[0] == 0
    # The adopted author still cannot approve their own revision.
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "p")
    assert code == 1 and "separation of duties" in err
    assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0
    # Adoption never overwrites durable history.
    code, _, err = run_cli(
        "plan-adopt", str(project_dir), "--author", "i",
        "--reason", "rewrite", "--actor", "p",
    )
    assert code == 1 and "already has durable author attribution" in err


def test_corrupt_audit_blocks_approval_and_sync_repairs(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    audit_path(aperio_dir).write_text("{not json")
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r2")
    assert code == 1 and "corrupt" in err
    audit_path(aperio_dir).write_text(
        json.dumps({"schema_version": 1, "events": []})
    )
    assert run_cli("plan-sync-authors", str(project_dir), "--actor", "p")[0] == 0
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r2")
    assert code == 0, err


def test_conflicting_audit_attribution_blocks_approval(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    append_event(
        aperio_dir, actor="i2", role="implementer", action="plan.revise",
        target="plan", revision=1, outcome="conflict",
    )
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r2")
    assert code == 1 and "conflicting author attribution" in err


def test_adopted_author_self_approval_blocked_after_recovery(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "plan-create", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan())),
    )[0] == 0
    assert run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner",
        "--assign", "r:reviewer",
    )[0] == 0
    adopt_plan_author(
        project_dir / ".aperio", 1, "p", reason="legacy", actor="p"
    )
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "p")
    assert code == 1 and "separation of duties" in err
    assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0


def test_concurrent_revision_and_approval_keep_attribution_valid(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    revise_file = _write_plan(tmp_path, _plan("concurrent"), "revise.json")
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "aperio", "plan-revise",
             str(project_dir), "--file", str(revise_file),
             "--revises", "2", "--actor", "p"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT),
        ),
        subprocess.Popen(
            [sys.executable, "-m", "aperio", "plan-approve",
             str(project_dir), "--actor", "r"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT),
        ),
    ]
    for proc in procs:
        _out, err = proc.communicate(timeout=60)
        assert "Traceback" not in err
        if proc.returncode != 0:
            assert "aperio: error:" in err
    plan = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    assert plan["authors"][str(plan["plan_version"])]["actor"] == "p"
    if plan["approval"]["status"] != "approved":
        assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0
    assert check_integrity(str(project_dir))["ok"] is True


# --- 2. attempt-bound review approvals ---------------------------------------


def test_reopen_invalidates_prior_approval_but_keeps_history(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _approved_task(run_cli, project_dir, tmp_path)
    assert _complete(run_cli, project_dir)[0] == 0
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--reopen", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, project_dir, evidence="reverified")
    assert code == 1 and "current attempt is 2" in err
    _submit(run_cli, project_dir, evidence="reverified")
    assert _decide(run_cli, project_dir, "REV-0002")[0] == 0
    assert _complete(run_cli, project_dir, evidence="reverified")[0] == 0
    # The old decision remains in immutable history.
    assert [r["outcome"] for r in list_reviews(aperio_dir)] == [
        "approved", "approved"
    ]


def test_newer_rejection_blocks_older_approval(
    run_cli, project_dir, tmp_path
):
    _approved_task(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir)
    assert _decide(
        run_cli, project_dir, "REV-0002", actor="r2",
        outcome="rejected", findings="unsafe",
    )[0] == 0
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "older approval cannot override" in err
    _submit(run_cli, project_dir)
    assert _decide(run_cli, project_dir, "REV-0003")[0] == 0
    assert _complete(run_cli, project_dir)[0] == 0


def test_revised_evidence_invalidates_approved_review(
    run_cli, project_dir, tmp_path
):
    _approved_task(run_cli, project_dir, tmp_path)
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--evidence", "new evidence", "--limitations",
        "none", "--result", "passed", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, project_dir, evidence="new evidence")
    assert code == 1 and "work or evidence changed" in err
    _submit(run_cli, project_dir, evidence="new evidence")
    assert _decide(run_cli, project_dir, "REV-0002")[0] == 0
    assert _complete(run_cli, project_dir, evidence="new evidence")[0] == 0


def test_legacy_review_without_attempt_cannot_satisfy_completion(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _approved_task(run_cli, project_dir, tmp_path)
    path = aperio_dir / "reviews.json"
    document = json.loads(path.read_text())
    record = document["reviews"][0]
    del record["target"]["attempt"]
    del record["target"]["task_revision"]
    del record["implemented_by"]
    path.write_text(json.dumps(document))
    code, _, err = _complete(run_cli, project_dir)
    assert code == 1 and "predates attempt-bound" in err


def test_export_import_preserves_attempt_binding(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _approved_task(run_cli, project_dir, tmp_path)
    assert _complete(run_cli, project_dir)[0] == 0
    before = list_reviews(aperio_dir)[0]
    archive = tmp_path / "bound.zip"
    export_project(str(project_dir), output=str(archive))
    destination = tmp_path / "imported"
    import_archive(str(destination), str(archive))
    after = list_reviews(destination / ".aperio")[0]
    assert after["target"]["attempt"] == before["target"]["attempt"]
    assert after["target"]["task_revision"] == before["target"]["task_revision"]
    assert after["implemented_by"] == "i"
    # Reopening in the imported project still invalidates the old approval.
    code, _, err = run_cli(
        "task-status", str(destination), "TASK-001", "--status",
        "in-progress", "--reopen", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, destination)
    assert code == 1 and "current attempt is 2" in err
    assert check_integrity(str(destination))["ok"] is True


# --- 3. reviewer independence from the implementer ---------------------------


def test_only_recorded_implementer_may_submit(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir, actor="i")
    code, _, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i2", "--scope", "s", "--evidence", "e",
    )
    assert code == 1 and "implemented by 'i'" in err


def test_role_change_cannot_review_own_implementation(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    assert run_cli(
        "policy-assign", str(project_dir), "i", "--role", "reviewer"
    )[0] == 0
    code, _, err = _decide(run_cli, project_dir, actor="i")
    assert code == 1 and "own submission" in err
    assert _decide(run_cli, project_dir, actor="r")[0] == 0
    stored = list_reviews(aperio_dir)[0]
    assert stored["implemented_by"] == "i"
    assert stored["reviewer"] == "r"


def test_self_review_is_structurally_rejected_even_if_forged(
    tmp_path
):
    document = {
        "schema_version": 1,
        "reviews": [{
            "review_id": "REV-0001",
            "target": {
                "kind": "task", "task_id": "TASK-001",
                "plan_revision": 1, "run_id": None,
                "attempt": 1, "task_revision": 1,
            },
            "submitted_by": "b",
            "implemented_by": "a",
            "submitted_role": "implementer",
            "scope": ["s"], "evidence": ["e"], "limitations": None,
            "outcome": "approved", "reviewer": "a",
            "findings": "self", "created_at": "2026-01-01T00:00:00Z",
            "decided_at": "2026-01-01T00:00:00Z",
        }],
    }
    with pytest.raises(StateError, match="was submitted by 'b'"):
        validate_reviews_document(document, tmp_path / "reviews.json")
    document["reviews"][0]["submitted_by"] = "a"
    document["reviews"][0]["implemented_by"] = "a"
    with pytest.raises(StateError, match="own submitter"):
        validate_reviews_document(document, tmp_path / "reviews.json")


def test_missing_owner_blocks_submission(run_cli, project_dir, tmp_path):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    plan_path = aperio_dir / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["tasks"][0]["owner"] = None
    plan["tasks"][0].pop("implementer", None)
    plan_path.write_text(json.dumps(plan))
    code, _, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "s", "--evidence", "e",
    )
    assert code == 1 and "no recorded implementer" in err


# --- 4. no general completion bypass ----------------------------------------


def test_public_update_task_has_no_system_bypass(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    with pytest.raises(TypeError):
        update_task(
            aperio_dir, "TASK-001", new_status="completed",
            evidence="x", limitations="none", result="passed",
            system=True,
        )
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"


def test_recovery_refuses_nonprepared_and_unowned_claims(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    # No execution record exists: recovery cannot invent one.
    with pytest.raises(StateError, match="no such execution record"):
        release_failed_claim(
            aperio_dir, "TASK-001", run_id="RUN-9999",
            claim_owner="i",
        )
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"
    assert plan["tasks"][0]["owner"] == "i"


def test_delayed_recovery_never_reverses_competing_ownership(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    from aperio.runtime import dispatch_task

    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual", config=None,
        authorize_exec=False, actor="i",
    )
    # Synthetic competing ownership change plus a still-prepared record:
    # exactly what a delayed recovery must not overwrite.
    plan_path = aperio_dir / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["tasks"][0]["owner"] = "someone-else"
    plan_path.write_text(json.dumps(plan))
    executions = json.loads((aperio_dir / "executions.json").read_text())
    executions["runs"][0]["status"] = "prepared"
    (aperio_dir / "executions.json").write_text(json.dumps(executions))
    with pytest.raises(StateError, match="competing change"):
        release_failed_claim(
            aperio_dir, "TASK-001", run_id=record["run_id"],
            claim_owner="manual:RUN-0001",
        )
    plan = json.loads(plan_path.read_text())
    assert plan["tasks"][0]["owner"] == "someone-else"
    assert plan["tasks"][0]["status"] == "in-progress"


def test_memory_blocking_reflects_latest_attempt_binding(
    run_cli, project_dir, tmp_path
):
    from aperio.memory import load_context

    _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    assert _decide(run_cli, project_dir)[0] == 0
    assert load_context(project_dir).governance["blocking_requirements"] == []
    _submit(run_cli, project_dir)
    blocking = load_context(project_dir).governance["blocking_requirements"]
    assert [item["task_id"] for item in blocking] == ["TASK-001"]


def test_old_policy_without_recovery_actions_can_still_adopt(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "plan-create", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan())),
    )[0] == 0
    assert run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner",
        "--assign", "r:reviewer",
    )[0] == 0
    policy_file = project_dir / ".aperio" / "policies.json"
    policy = json.loads(policy_file.read_text())
    policy["roles"]["planner"]["actions"] = [
        "plan.create", "plan.revise", "plan.request_approval",
        "plan.approve",
    ]
    policy_file.write_text(json.dumps(policy))
    code, _, err = run_cli(
        "plan-adopt", str(project_dir), "--author", "p",
        "--reason", "legacy", "--actor", "p",
    )
    assert code == 0, err


def _start(*args):
    return subprocess.Popen(
        [sys.executable, "-m", "aperio", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT),
    )


def _finish(proc):
    _out, err = proc.communicate(timeout=60)
    assert "Traceback" not in err
    if proc.returncode != 0:
        assert "aperio: error:" in err
    return proc.returncode


def test_concurrent_completion_and_new_review_never_lose_a_gate(
    run_cli, project_dir, tmp_path
):
    _approved_task(run_cli, project_dir, tmp_path)
    complete = _start(
        "task-status", str(project_dir), "TASK-001", "--status",
        "completed", "--evidence", "pytest", "--limitations", "none",
        "--result", "passed", "--actor", "i",
    )
    submit = _start(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "s", "--evidence", "e",
    )
    codes = sorted((_finish(complete), _finish(submit)))
    plan = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    reviews = read_reviews(project_dir / ".aperio")["reviews"]
    # The completion guard holds reviews.lock through publication, so either
    # completion wins and a later submit is refused, or the new pending
    # review wins and completion is refused. Both never succeed together.
    assert codes == [0, 1]
    if plan["tasks"][0]["status"] == "completed":
        assert [review["outcome"] for review in reviews] == ["approved"]
    else:
        assert plan["tasks"][0]["status"] == "in-progress"
        assert reviews[-1]["outcome"] == "pending"
    assert check_integrity(str(project_dir))["ok"] is True


def test_concurrent_revision_policy_change_and_decision_stay_consistent(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _claim(run_cli, project_dir)
    _submit(run_cli, project_dir)
    revise_file = _write_plan(tmp_path, _plan("concurrent"), "revise.json")
    procs = [
        _start("plan-revise", str(project_dir), "--file", str(revise_file),
               "--revises", "2", "--actor", "p"),
        _start("policy-assign", str(project_dir), "r2", "--role", "planner"),
        _start("review-decide", str(project_dir), "REV-0001", "--actor", "r",
               "--outcome", "approved", "--findings", "ok"),
    ]
    for proc in procs:
        _finish(proc)
    plan = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    assert read_policy(project_dir / ".aperio")["actors"]["r2"]["role"] == "planner"
    if plan["approval"]["status"] != "approved":
        assert run_cli(
            "plan-approve", str(project_dir), "--actor", "r2"
        )[0] == 0
    assert check_integrity(str(project_dir))["ok"] is True


def test_run_owned_claim_keeps_declared_implementer_attribution(
    run_cli, project_dir, tmp_path
):
    from aperio.runtime import (
        dispatch_task, read_executions, record_external_result,
    )

    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual", config=None,
        authorize_exec=False, actor="i",
    )
    plan = json.loads((aperio_dir / "plan.json").read_text())
    task = plan["tasks"][0]
    stored_run = next(
        run for run in read_executions(aperio_dir)["runs"]
        if run["run_id"] == record["run_id"]
    )
    assert task["owner"] == stored_run["owner"]  # operational claim holder
    assert task["implementer"] == "i"               # declared implementer
    record_external_result(
        aperio_dir, record["run_id"],
        {"outcome_reported": "done", "success_reported": True},
    )
    _submit(run_cli, project_dir, actor="i")
    assert _decide(run_cli, project_dir, actor="r")[0] == 0
    code, _, err = _complete(run_cli, project_dir)
    assert code == 0, err


def test_integrity_reports_missing_audit_for_attributed_plan(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    (aperio_dir / "policy-audit.json").unlink()
    report = check_integrity(str(project_dir))
    assert any(
        item["code"] == "attribution-audit-missing"
        for item in report["errors"]
    )
    # The explicit recovery is idempotent and restores a clean report.
    assert run_cli("plan-sync-authors", str(project_dir), "--actor", "p")[0] == 0
    assert check_integrity(str(project_dir))["ok"] is True


def test_concurrent_adoption_and_approval_never_guess_authorship(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "plan-create", str(project_dir),
        "--file", str(_write_plan(tmp_path, _plan())),
    )[0] == 0
    assert run_cli(
        "policy-init", str(project_dir), "--assign", "p:planner",
        "--assign", "r:reviewer",
    )[0] == 0
    adopt = _start(
        "plan-adopt", str(project_dir), "--author", "p",
        "--reason", "legacy", "--actor", "p",
    )
    approve = _start("plan-approve", str(project_dir), "--actor", "r")
    _finish(adopt)
    _finish(approve)
    plan = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    assert plan["authors"]["1"]["actor"] == "p"
    if plan["approval"]["status"] != "approved":
        assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0
    # Once adopted, the declared author still cannot self-approve.
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "p")
    assert code == 1 and "separation of duties" in err


def test_governed_startup_failure_releases_only_its_run_claim(
    run_cli, project_dir, tmp_path
):
    import os

    from aperio.runtime import dispatch_task, read_executions

    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    os.symlink(outside, aperio_dir / "runs")
    with pytest.raises(AperioError):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id="manual", config=None,
            authorize_exec=False, actor="i",
        )
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "pending"
    assert plan["tasks"][0]["owner"] is None
    run = read_executions(aperio_dir)["runs"][0]
    assert run["status"] == "failed"
    assert run["claim_released"] is True
    # Recovery never recorded verification or completion.
    assert plan["tasks"][0]["verification"]["result"] is None
