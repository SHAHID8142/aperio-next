"""Controlled standardization: proposal, preview, approval, apply, verify."""

from __future__ import annotations

import json
import os

import pytest

from aperio.errors import StateError, ValidationError
from aperio.onboarding_discovery import discover_workspace
from aperio.onboarding_reconstruct import reconstruct_workspace
from aperio.onboarding_session import (
    create_session,
    has_approval,
    read_session,
    record_approval,
)
from aperio.onboarding_standardize import (
    APPLY_ACTION,
    apply_standardization,
    load_proposal,
    propose_standardization,
    render_proposal_markdown,
    save_proposal,
    verify_standardization,
)
from aperio.project import init_project


@pytest.fixture
def project(project_dir):
    """An existing, non-Aperio project with docs."""
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# app\nlanguage: Python\n")
    (project_dir / "PRD.md").write_text("language: Go\n- [ ] beta launch\n")
    (project_dir / "AGENTS.md").write_text("# legacy rules\n")
    return project_dir


@pytest.fixture
def apdir(project):
    init_project(str(project))
    return project / ".aperio"


@pytest.fixture
def proposal(project):
    discovery = discover_workspace(str(project))
    reconstruction = reconstruct_workspace(str(project))
    return propose_standardization(discovery, reconstruction)


def test_proposal_covers_the_required_checklist(proposal, project):
    d = proposal.to_dict()
    assert d["format"] == "aperio-onboarding-standardization-proposal"
    assert proposal.existing_documents  # inventoried documents
    assert proposal.source_of_truth
    assert proposal.proposed_additions  # canonical artifacts
    # No existing file is ever modified — the modifications list is empty by design.
    assert proposal.proposed_modifications == ()
    assert proposal.untouched  # README/PRD/AGENTS explicitly preserved
    assert proposal.backup_plan and proposal.rollback_plan
    assert proposal.compatibility_risks


def test_proposal_reports_conflicts(proposal):
    topics = [c.topic for c in proposal.conflicts]
    assert "language" in topics  # README vs PRD disagree
    assert "orchestration-ownership" in topics  # AGENTS.md present


def test_preview_contains_changes_and_preservation(proposal):
    text = render_proposal_markdown(proposal)
    assert proposal.proposal_id in text
    assert ".aperio/state.json" in text
    assert "None." in text  # modifications section says none
    assert "README.md" in text  # untouched list
    assert "Backup" in text


def test_proposal_is_read_only(project, proposal):
    before = sorted((p.name, p.stat().st_mtime_ns) for p in project.rglob("*"))
    render_proposal_markdown(proposal)
    after = sorted((p.name, p.stat().st_mtime_ns) for p in project.rglob("*"))
    assert before == after


def test_save_and_load_roundtrip(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    path = save_proposal(apdir, proposal)
    assert path.is_file()
    loaded = load_proposal(apdir, proposal.proposal_id)
    assert loaded.proposal_id == proposal.proposal_id
    assert loaded.existing_documents == proposal.existing_documents


def test_load_rejects_bad_ids(apdir):
    with pytest.raises(ValidationError):
        load_proposal(apdir, "../escape")
    with pytest.raises(Exception):
        load_proposal(apdir, "STD-does-not-exist")


def test_apply_without_approval_refused(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    before = sorted(p.name for p in project.rglob("*"))
    with pytest.raises(StateError, match="not approved"):
        apply_standardization(apdir, proposal)
    after = sorted(p.name for p in project.rglob("*"))
    assert before == after
    assert not has_approval(apdir, APPLY_ACTION, proposal.proposal_id)


def test_declined_migration_preserves_original_files(apdir, proposal, project):
    """The operator declines: nothing happens at all."""
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    before = {p.name: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    # No approval recorded -> no apply call at all.
    assert not has_approval(apdir, APPLY_ACTION, proposal.proposal_id)
    after = {p.name: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert before == after
    assert (project / "AGENTS.md").read_text() == "# legacy rules\n"


def test_approved_apply_creates_new_files_only(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope=proposal.proposal_id)
    originals = {
        name: (project / name).read_bytes() for name in ("README.md", "PRD.md", "AGENTS.md")
    }
    result = apply_standardization(apdir, proposal)
    assert (project / ".aperio/state.json").is_file()
    assert (project / ".aperio/brief.md").is_file()
    assert (apdir / "onboarding/standardization-mapping.json").is_file()
    # Originals byte-identical.
    for name, content in originals.items():
        assert (project / name).read_bytes() == content
    # Session records the applied mapping.
    session = read_session(apdir)
    assert session["standardization"]["proposal_id"] == proposal.proposal_id


def test_apply_scope_is_exact(apdir, proposal, project):
    """An approval for a different proposal id does not authorize this one."""
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope="STD-some-other-proposal")
    with pytest.raises(StateError, match="not approved"):
        apply_standardization(apdir, proposal)


def test_apply_refuses_double_application(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope=proposal.proposal_id)
    apply_standardization(apdir, proposal)
    mapping = apdir / "onboarding/standardization-mapping.json"
    original_mapping = mapping.read_bytes()
    # A second proposal is a different id -> no approval -> refused.
    discovery = discover_workspace(str(project))
    reconstruction = reconstruct_workspace(str(project))
    second = propose_standardization(discovery, reconstruction)
    with pytest.raises(StateError, match="not approved"):
        apply_standardization(apdir, second)
    # Even with approval, the existing mapping blocks a destructive redo.
    record_approval(apdir, action=APPLY_ACTION, scope=second.proposal_id)
    with pytest.raises(StateError, match="already exists"):
        apply_standardization(apdir, second)
    assert mapping.read_bytes() == original_mapping


def test_failed_apply_leaves_no_debris(apdir, proposal, project):
    """A mid-apply failure removes what this attempt created and guides recovery."""
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope=proposal.proposal_id)
    # Pre-create the mapping so step 3 fails after init + checkpoint ran.
    onboarding_dir = apdir / "onboarding"
    onboarding_dir.mkdir(exist_ok=True)
    (onboarding_dir / "standardization-mapping.json").write_text("{}\n")
    created_before = sorted(p.name for p in project.rglob("*"))
    with pytest.raises(StateError, match="already exists"):
        apply_standardization(apdir, proposal)
    after = sorted(p.name for p in project.rglob("*"))
    # Nothing new was left behind by the failed attempt.
    assert created_before == after


def test_verify_after_apply(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope=proposal.proposal_id)
    apply_standardization(apdir, proposal)
    original_hashes = {
        d["path"]: d["sha256"] for d in proposal.existing_documents
    }
    report = verify_standardization(apdir, proposal, original_hashes)
    assert report["all_ok"] is True
    assert report["drift"] == []


def test_verify_reports_drift_honestly(apdir, proposal, project):
    create_session(apdir, workspace=str(project), classification="legacy_orchestrated")
    record_approval(apdir, action=APPLY_ACTION, scope=proposal.proposal_id)
    hashes = {d["path"]: d["sha256"] for d in proposal.existing_documents}
    apply_standardization(apdir, proposal)
    (project / "README.md").write_text("# changed after reconstruction\n")
    report = verify_standardization(apdir, proposal, hashes)
    assert report["all_ok"] is False
    assert report["drift"] == [{"path": "README.md", "issue": "content changed since reconstruction"}]


def test_apply_requires_valid_project(tmp_path, proposal, project):
    (tmp_path / ".aperio").mkdir()  # partial, no state
    with pytest.raises(Exception):
        apply_standardization(tmp_path / ".aperio", proposal)
