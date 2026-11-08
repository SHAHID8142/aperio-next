"""Feature 06 evidence-binding gate.

Completion must use exactly the evidence and limitations of the latest
approved independent review for the current work attempt.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aperio.integrity import check_integrity
from aperio.portable import export_project, import_archive
from aperio.reviews import list_reviews

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plan() -> dict:
    return {
        "objective": "bind evidence",
        "expected_outcome": "reviewed evidence only",
        "scope": ["governance"],
        "exclusions": [],
        "assumptions": [],
        "risks": [],
        "strategy": "bind completion",
        "acceptance_criteria": ["reviewed evidence"],
        "verification_strategy": "pytest",
        "tasks": [{
            "id": "TASK-001",
            "title": "evidence binding",
            "objective": "prove it",
            "deliverables": ["evidence"],
            "acceptance_criteria": ["reviewed"],
            "depends_on": [],
        }],
    }


def _write_plan(tmp_path: Path, name="plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(_plan()))
    return path


def _governed(run_cli, project_dir, tmp_path) -> Path:
    assert run_cli("init", str(project_dir))[0] == 0
    code, _, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "r:reviewer", "--assign", "r2:reviewer",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "plan-create", str(project_dir), "--file", str(_write_plan(tmp_path)),
        "--actor", "p",
    )
    assert code == 0, err
    assert run_cli("plan-approve", str(project_dir), "--actor", "r")[0] == 0
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err
    return project_dir / ".aperio"


def _submit(run_cli, project_dir, evidence, limitations=None):
    args = [
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "criteria", "--evidence", evidence,
    ]
    if limitations is not None:
        args += ["--limitations", limitations]
    code, out, err = run_cli(*args)
    assert code == 0, err
    return out


def _approve(run_cli, project_dir, review_id="REV-0001", actor="r",
             outcome="approved", findings="ok"):
    return run_cli(
        "review-decide", str(project_dir), review_id, "--actor", actor,
        "--outcome", outcome, "--findings", findings,
    )


def _complete(run_cli, project_dir, evidence, limitations="none"):
    return run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "completed",
        "--evidence", evidence, "--limitations", limitations,
        "--result", "passed", "--actor", "i",
    )


def test_approved_then_substituted_evidence_is_refused(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    code, _, err = _complete(run_cli, project_dir, "pytest: evidence B")
    assert code == 1 and "not part of the approved review" in err
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["status"] == "in-progress"
    assert plan["tasks"][0]["verification"]["evidence"] is None
    assert list_reviews(aperio_dir)[0]["evidence"] == ["pytest: evidence A"]


def test_matching_approved_evidence_completes(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    code, out, err = _complete(run_cli, project_dir, "pytest: evidence A")
    assert code == 0, err
    assert "completed" in out
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["tasks"][0]["verification"]["evidence"] == "pytest: evidence A"


def test_changing_verification_after_approval_requires_new_review(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--evidence", "pytest: evidence B", "--limitations",
        "none", "--result", "passed", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, project_dir, "pytest: evidence B")
    assert code == 1 and "work or evidence changed" in err
    _submit(run_cli, project_dir, "pytest: evidence B")
    assert _approve(run_cli, project_dir, "REV-0002")[0] == 0
    assert _complete(run_cli, project_dir, "pytest: evidence B")[0] == 0


def test_newer_rejection_blocks_older_approval_with_matching_evidence(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(
        run_cli, project_dir, "REV-0002", actor="r2",
        outcome="rejected", findings="regression",
    )[0] == 0
    code, _, err = _complete(run_cli, project_dir, "pytest: evidence A")
    assert code == 1 and "older approval cannot override" in err


def test_reopen_invalidates_evidence_approval(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    assert _complete(run_cli, project_dir, "pytest: evidence A")[0] == 0
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--reopen", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = _complete(run_cli, project_dir, "pytest: evidence A")
    assert code == 1 and "current attempt is 2" in err
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir, "REV-0002")[0] == 0
    assert _complete(run_cli, project_dir, "pytest: evidence A")[0] == 0


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


def test_concurrent_completion_and_new_submission_stay_consistent(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    complete = _start(
        "task-status", str(project_dir), "TASK-001", "--status",
        "completed", "--evidence", "pytest: evidence A", "--limitations",
        "none", "--result", "passed", "--actor", "i",
    )
    submit = _start(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "s", "--evidence", "pytest: evidence B",
    )
    codes = sorted((_finish(complete), _finish(submit)))
    assert codes == [0, 1]
    plan = json.loads((aperio_dir / "plan.json").read_text())
    reviews = list_reviews(aperio_dir)
    if plan["tasks"][0]["status"] == "completed":
        assert reviews[-1]["outcome"] == "approved"
    else:
        assert plan["tasks"][0]["status"] == "in-progress"
        assert reviews[-1]["outcome"] == "pending"
    assert check_integrity(str(project_dir))["ok"] is True


def test_export_import_preserves_evidence_relationship(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    _submit(run_cli, project_dir, "pytest: evidence A")
    assert _approve(run_cli, project_dir)[0] == 0
    archive = tmp_path / "evidence.zip"
    export_project(str(project_dir), output=str(archive))
    destination = tmp_path / "imported"
    import_archive(str(destination), str(archive))
    stored = list_reviews(destination / ".aperio")[0]
    assert stored["evidence"] == ["pytest: evidence A"]
    code, _, err = _complete(
        run_cli, destination, "pytest: evidence B"
    )
    assert code == 1 and "not part of the approved review" in err
    assert _complete(
        run_cli, destination, "pytest: evidence A"
    )[0] == 0
    assert check_integrity(str(destination))["ok"] is True


def test_legacy_project_without_policy_keeps_completion_behavior(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "plan-create", str(project_dir), "--file", str(_write_plan(tmp_path)),
    )[0] == 0
    assert run_cli("plan-approve", str(project_dir))[0] == 0
    assert run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--owner", "any-agent",
    )[0] == 0
    code, out, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "completed",
        "--evidence", "legacy evidence", "--limitations", "none",
        "--result", "passed",
    )
    assert code == 0, err
    assert "completed" in out


def test_limitations_must_match_the_approved_review(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    _submit(
        run_cli, project_dir, "pytest: evidence A",
        limitations="known timeout on CI",
    )
    assert _approve(run_cli, project_dir)[0] == 0
    code, _, err = _complete(
        run_cli, project_dir, "pytest: evidence A", limitations="none"
    )
    assert code == 1 and "limitations" in err
    assert _complete(
        run_cli, project_dir, "pytest: evidence A",
        limitations="known timeout on CI",
    )[0] == 0


def test_failed_verification_cannot_be_submitted_for_approval(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status",
        "in-progress", "--evidence", "pytest: failed", "--limitations",
        "none", "--result", "failed", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "criteria", "--evidence",
        "pytest: failed",
    )
    assert code == 1 and "failed verification" in err
