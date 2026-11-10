"""Master roadmap (two-level planning): model, store, CLI, governance,
plan linkage, portability, and the v3 optional milestone field."""

from __future__ import annotations

import json

import pytest

from aperio.errors import PathError, StateError, ValidationError
from aperio.planmodel import validate_plan_input
from aperio.planstore import plan_summary, read_plan
from aperio.roadmapmodel import (
    derive_milestone_view,
    validate_roadmap_document,
    validate_roadmap_input,
)
from aperio.roadmapstore import (
    approve_roadmap,
    create_roadmap,
    read_roadmap,
    roadmap_exists,
    roadmap_summary,
    revise_roadmap,
)

ROADMAP_INPUT = {
    "objective": "Deliver a trustworthy local-first site builder",
    "lifecycle": "discovery -> planning -> build -> verify -> release",
    "milestones": [
        {
            "id": "MILESTONE-1",
            "title": "Project skeleton",
            "capabilities": ["working scaffold"],
            "depends_on": [],
            "risks": ["scope creep"],
            "acceptance_conditions": ["scaffold boots", "tests green"],
        },
        {
            "id": "MILESTONE-2",
            "title": "Content model",
            "capabilities": ["editable content"],
            "depends_on": ["MILESTONE-1"],
            "risks": [],
            "acceptance_conditions": ["content round-trips"],
        },
    ],
}

PLAN_INPUT = {
    "objective": "Build the project skeleton",
    "expected_outcome": "A tested scaffold",
    "scope": ["scaffold"],
    "exclusions": [],
    "assumptions": [],
    "risks": [],
    "strategy": "small steps",
    "acceptance_criteria": ["tests pass"],
    "verification_strategy": "pytest",
    "tasks": [
        {"id": "TASK-001", "title": "Scaffold", "objective": "Create it",
         "deliverables": ["scaffold/"], "acceptance_criteria": ["boots"],
         "depends_on": []},
    ],
}


def _init(run_cli, project_dir):
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    return project_dir / ".aperio"


def _write_json(tmp_path, name, data) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


# --- model: input validation -------------------------------------------------------


def test_validate_roadmap_input_normalizes_content():
    cleaned = validate_roadmap_input(ROADMAP_INPUT)
    assert cleaned["objective"] == ROADMAP_INPUT["objective"]
    assert cleaned["milestones"][0]["id"] == "MILESTONE-1"
    assert cleaned["milestones"][1]["depends_on"] == ["MILESTONE-1"]


def test_validate_roadmap_input_rejects_unknown_and_system_keys():
    with pytest.raises(ValidationError, match="unknown or system-managed"):
        validate_roadmap_input({**ROADMAP_INPUT, "surprise": 1})
    with pytest.raises(ValidationError, match="unknown or system-managed"):
        validate_roadmap_input({**ROADMAP_INPUT, "approval": {"status": "approved"}})


def test_validate_roadmap_input_rejects_missing_fields():
    data = {k: v for k, v in ROADMAP_INPUT.items() if k != "lifecycle"}
    with pytest.raises(ValidationError, match="missing required field"):
        validate_roadmap_input(data)


def test_validate_roadmap_input_rejects_empty_milestones():
    with pytest.raises(ValidationError, match="at least 1 milestone"):
        validate_roadmap_input({**ROADMAP_INPUT, "milestones": []})


def test_validate_milestone_rejects_bad_id_grammar():
    bad = json.loads(json.dumps(ROADMAP_INPUT))
    bad["milestones"][0]["id"] = "milestone-1"
    with pytest.raises(ValidationError, match="MILESTONE-<digits>"):
        validate_roadmap_input(bad)


def test_validate_milestone_rejects_empty_capabilities():
    bad = json.loads(json.dumps(ROADMAP_INPUT))
    bad["milestones"][0]["capabilities"] = []
    with pytest.raises(ValidationError, match="capabilities"):
        validate_roadmap_input(bad)


def test_validate_roadmap_rejects_duplicate_ids_and_cycles():
    bad = json.loads(json.dumps(ROADMAP_INPUT))
    bad["milestones"][1]["id"] = "MILESTONE-1"
    with pytest.raises(ValidationError, match="duplicate milestone id"):
        validate_roadmap_input(bad)
    cyc = json.loads(json.dumps(ROADMAP_INPUT))
    cyc["milestones"][0]["depends_on"] = ["MILESTONE-2"]
    with pytest.raises(ValidationError, match="cycle"):
        validate_roadmap_input(cyc)


def test_stored_document_tolerates_unknown_keys_but_validates_content(tmp_path):
    doc = {
        "schema_version": 1,
        "roadmap_version": 1,
        "approval": {"status": "draft", "approved_revision": None,
                     "approved_at": None, "approved_by": None},
        "objective": "x",
        "lifecycle": "y",
        "milestones": ROADMAP_INPUT["milestones"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "aperio_version": "test",
        "extension_field": {"kept": True},
    }
    validated = validate_roadmap_document(doc, tmp_path / "roadmap.json")
    assert validated["extension_field"] == {"kept": True}
    broken = json.loads(json.dumps(doc))
    broken["milestones"][0]["acceptance_conditions"] = []
    with pytest.raises(StateError, match="corrupt"):
        validate_roadmap_document(broken, tmp_path / "roadmap.json")


# --- store: create / read / revise / approve ----------------------------------------


def test_create_starts_draft_revision_1(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    document = create_roadmap(aperio_dir, ROADMAP_INPUT)
    assert document["roadmap_version"] == 1
    assert document["approval"]["status"] == "draft"
    assert (aprio := aperio_dir / "roadmap.json").is_file()
    assert roadmap_exists(aperio_dir)


def test_create_on_existing_roadmap_conflicts(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    with pytest.raises(StateError, match="already exists"):
        create_roadmap(aperio_dir, ROADMAP_INPUT)


def test_read_missing_roadmap_guides_create(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    assert roadmap_summary(aperio_dir) is None
    with pytest.raises(StateError, match="roadmap-create"):
        read_roadmap(aperio_dir)


def test_symlinked_roadmap_refused(run_cli, project_dir, tmp_path):
    aperio_dir = _init(run_cli, project_dir)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (aprio := aperio_dir / "roadmap.json").symlink_to(target)
    with pytest.raises(PathError, match="symlink"):
        roadmap_summary(aperio_dir)
    (aprio).unlink()


def test_corrupt_roadmap_refused_not_rewritten(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    (aperio_dir / "roadmap.json").write_text("{ not json")
    with pytest.raises(StateError, match="corrupt"):
        roadmap_summary(aperio_dir)
    assert (aperio_dir / "roadmap.json").read_text() == "{ not json"


def test_future_schema_refused(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    doc = json.loads((aperio_dir / "roadmap.json").read_text())
    doc["schema_version"] = 2
    (aperio_dir / "roadmap.json").write_text(json.dumps(doc))
    summary = roadmap_summary(aperio_dir)
    assert summary["unsupported_schema"] is True
    assert summary["schema_version"] == 2
    with pytest.raises(StateError, match="will not"):
        read_roadmap(aperio_dir)


def test_revise_resets_approval_bumps_version_refuses_stale(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    approve_roadmap(aperio_dir, by="owner")
    revised = revise_roadmap(aperio_dir, ROADMAP_INPUT, revises=1)
    assert revised["roadmap_version"] == 2
    assert revised["approval"]["status"] == "draft"
    with pytest.raises(StateError, match="moved on"):
        revise_roadmap(aperio_dir, ROADMAP_INPUT, revises=1)


def test_approve_records_metadata_and_reapproval_allowed(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    document = approve_roadmap(aperio_dir, by="owner")
    assert document["approval"]["status"] == "approved"
    assert document["approval"]["approved_revision"] == 1
    assert document["approval"]["approved_by"] == "owner"
    again = approve_roadmap(aperio_dir, by="owner")
    assert again["approval"]["approved_by"] == "owner"
    with pytest.raises(StateError, match="moved on"):
        approve_roadmap(aperio_dir, revision=2)


# --- governance ----------------------------------------------------------------------


def _governed(run_cli, project_dir):
    aperio_dir = _init(run_cli, project_dir)
    code, out, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "owner-planner:planner",
        "--assign", "owner-reviewer:reviewer",
    )
    assert code == 0, err
    return aperio_dir


def test_governed_roadmap_requires_actor(run_cli, project_dir):
    aperio_dir = _governed(run_cli, project_dir)
    with pytest.raises(StateError, match="--actor"):
        create_roadmap(aperio_dir, ROADMAP_INPUT)


def test_governed_roadmap_separation_of_duties(run_cli, project_dir):
    aperio_dir = _governed(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT, actor="owner-planner")
    with pytest.raises(StateError, match="separation of duties"):
        approve_roadmap(aperio_dir, actor="owner-planner")
    document = approve_roadmap(aperio_dir, actor="owner-reviewer")
    assert document["approval"]["approved_by"] == "owner-reviewer"


def test_policy_predating_roadmap_still_allows_roadmap_actions(
    run_cli, project_dir
):
    """A governed project whose stored policy predates the roadmap feature
    must remain usable: roadmap actions stay role-gated intrinsically."""
    aperio_dir = _governed(run_cli, project_dir)
    policy_path = aperio_dir / "policies.json"
    policy = json.loads(policy_path.read_text())
    # simulate a pre-roadmap policy: the planner list without roadmap actions
    policy["roles"]["planner"]["actions"] = [
        a for a in policy["roles"]["planner"]["actions"]
        if not a.startswith("roadmap.")
    ]
    policy_path.write_text(json.dumps(policy))
    document = create_roadmap(aperio_dir, ROADMAP_INPUT, actor="owner-planner")
    assert document["roadmap_version"] == 1
    # author approves own roadmap: separation of duties still enforced
    with pytest.raises(StateError, match="separation of duties"):
        approve_roadmap(aperio_dir, actor="owner-planner")


# --- plan linkage (plan-schema v3 optional milestone) --------------------------------


def test_plan_input_accepts_optional_milestone():
    cleaned = validate_plan_input({**PLAN_INPUT, "milestone": "MILESTONE-1"})
    assert cleaned["milestone"] == "MILESTONE-1"
    with pytest.raises(ValidationError, match="MILESTONE-<digits>"):
        validate_plan_input({**PLAN_INPUT, "milestone": "MILESTONE_ONE"})


def test_plan_create_milestone_flag_and_summary(run_cli, project_dir, tmp_path):
    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    plan_file = _write_json(tmp_path, "plan.json", PLAN_INPUT)
    code, out, err = run_cli(
        "plan-create", str(project_dir), "--file", plan_file,
        "--milestone", "MILESTONE-1",
    )
    assert code == 0, err
    assert "implements MILESTONE-1" in out
    summary = plan_summary(aperio_dir)
    assert summary["milestone"] == "MILESTONE-1"
    code, out, _ = run_cli("plan-show", str(project_dir))
    assert "Milestone:   MILESTONE-1" in out


def test_plan_create_rejects_unknown_milestone(run_cli, project_dir, tmp_path):
    _init(run_cli, project_dir)
    plan_file = _write_json(tmp_path, "plan.json", PLAN_INPUT)
    code, out, err = run_cli(
        "plan-create", str(project_dir), "--file", plan_file,
        "--milestone", "PHASE-1",
    )
    assert code == 1
    assert "MILESTONE-<digits>" in err


def test_legacy_plan_without_milestone_reads_as_none(run_cli, project_dir):
    from aperio.planstore import create_plan

    aperio_dir = _init(run_cli, project_dir)
    document = create_plan(aperio_dir, PLAN_INPUT)
    # force a v2-shaped document (no milestone field) and re-read it
    doc = json.loads((aperio_dir / "plan.json").read_text())
    doc["schema_version"] = 2
    (aperio_dir / "plan.json").write_text(json.dumps(doc))
    plan = read_plan(aperio_dir)
    assert plan["schema_version"] == 2
    summary = plan_summary(aperio_dir)
    assert summary["milestone"] is None
    assert summary["plan_version"] == document["plan_version"]


def test_derive_milestone_view_without_and_with_plan():
    roadmap = validate_roadmap_input(ROADMAP_INPUT)
    view = derive_milestone_view(roadmap, None)
    assert all(m["in_current_plan"] is False for m in view)
    assert all(m["dependencies_in_plan"] is None for m in view)
    plan = {"milestone": "MILESTONE-2"}
    view = derive_milestone_view(roadmap, plan)
    by_id = {m["id"]: m for m in view}
    assert by_id["MILESTONE-2"]["in_current_plan"] is True
    assert by_id["MILESTONE-2"]["dependencies_in_plan"] is False
    assert by_id["MILESTONE-1"]["in_current_plan"] is False


# --- CLI surface ----------------------------------------------------------------------


def test_roadmap_cli_roundtrip(run_cli, project_dir, tmp_path):
    _init(run_cli, project_dir)
    roadmap_file = _write_json(tmp_path, "roadmap.json", ROADMAP_INPUT)
    code, out, err = run_cli(
        "roadmap-create", str(project_dir), "--file", roadmap_file
    )
    assert code == 0, err
    assert "roadmap created" in out and "2 milestone(s)" in out

    code, out, _ = run_cli("roadmap-show", str(project_dir))
    assert code == 0
    assert "MILESTONE-1" in out and "MILESTONE-2" in out
    assert "required before it counts as the approved master plan" in out

    code, out, _ = run_cli("roadmap-approve", str(project_dir), "--by", "owner")
    assert code == 0 and "approval recorded" in out

    code, out, _ = run_cli("roadmap-show", str(project_dir), "--json")
    assert code == 0
    assert json.loads(out)["approval"]["status"] == "approved"

    code, out, err = run_cli(
        "roadmap-revise", str(project_dir), "--file", roadmap_file,
        "--revises", "1",
    )
    assert code == 0, err
    assert "approval reset to draft" in out


def test_roadmap_show_without_roadmap_mentions_create(run_cli, project_dir):
    _init(run_cli, project_dir)
    code, out, _ = run_cli("roadmap-show", str(project_dir))
    assert code == 0
    assert "roadmap-create" in out


def test_roadmap_create_requires_initialized_project(run_cli, project_dir, tmp_path):
    code, out, err = run_cli(
        "roadmap-create", str(project_dir),
        "--file", _write_json(tmp_path, "r.json", ROADMAP_INPUT),
    )
    assert code == 1


# --- portability ------------------------------------------------------------------------


def test_export_import_roundtrip_carries_roadmap(run_cli, project_dir, tmp_path):
    from aperio.portable import export_project, import_archive

    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    archive = tmp_path / "bundle.zip"
    export_project(str(project_dir), output=str(archive))
    destination = tmp_path / "dest"
    import_archive(str(destination), str(archive))
    imported = roadmap_summary(destination / ".aperio")
    assert imported["present"] is True
    assert imported["objective"] == ROADMAP_INPUT["objective"]
    assert imported["milestone_ids"] == ["MILESTONE-1", "MILESTONE-2"]


def test_integrity_reports_roadmap_schema(run_cli, project_dir):
    from aperio.integrity import check_integrity

    aperio_dir = _init(run_cli, project_dir)
    create_roadmap(aperio_dir, ROADMAP_INPUT)
    report = check_integrity(str(project_dir))
    assert report["supported"]["roadmap"] == 1
    assert report["schemas"]["roadmap"] == 1
    assert "roadmap.json" not in report["unexpected"]
    assert report["ok"]
