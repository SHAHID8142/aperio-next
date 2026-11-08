"""Portable context loading: structure, missing files, exclusions."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from aperio.decisions import append_decision
from aperio.errors import NotInitializedError, PathError, StateError
from aperio.handoff import HandoffContent, write_handoff
from aperio.memory import load_context


def test_full_context_load(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    append_decision(aperio_dir, title="T", context="c", decision="d", consequences="x")
    write_handoff(
        aperio_dir,
        HandoffContent(
            current_objective="obj",
            completed_work="done",
            work_in_progress="wip",
            next_recommended_action="next",
            blockers="none",
            verification_evidence="tests",
            verification_limitations="none",
        ),
    )
    context = load_context(project_dir)
    assert context.state.phase == "initialized"
    assert context.brief is not None and "# Project Brief" in context.brief
    assert context.decisions is not None and len(context.decisions) == 1
    assert context.handoff is not None and context.handoff.structured is True
    assert context.missing == []
    assert context.skipped_headings == 0


def test_missing_optional_files_reported(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "brief.md").unlink()
    (aprio := aperio_dir / "decisions.md").unlink()
    (aprio := aperio_dir / "handoff.md").unlink()
    context = load_context(project_dir)
    assert context.brief is None
    assert context.decisions is None  # missing, not merely empty
    assert context.handoff is None
    assert context.missing == ["brief.md", "decisions.md", "handoff.md"]


def test_present_but_empty_decisions_distinguished_from_missing(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    context = load_context(project_dir)
    assert context.decisions == []  # file exists with zero entries
    assert "decisions.md" not in context.missing


def test_uninitialized_and_corrupt_state(run_cli, tmp_path, project_dir):
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        load_context(tmp_path / "nope")
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/state.json").write_text("{bad json")
    with pytest.raises(StateError, match="corrupt"):
        load_context(project_dir)


def test_newer_schema_state_reported(run_cli, project_dir):
    run_cli("init", str(project_dir))
    future = {
        "schema_version": 99,
        "name": "proj",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "phase": "initialized",
        "aperio_version": "99.0.0",
    }
    (project_dir / ".aperio/state.json").write_text(json.dumps(future))
    with pytest.raises(StateError, match="newer"):
        load_context(project_dir)


def test_non_utf8_brief_clean_error(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/brief.md").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(StateError, match="UTF-8"):
        load_context(project_dir)


def test_local_directory_never_enters_context(run_cli, project_dir):
    run_cli("init", str(project_dir))
    secret = project_dir / ".aperio/local/credentials.txt"
    secret.write_text("SECRET-LOCAL-CONTENT-123")
    context = load_context(project_dir)
    dumped = json.dumps(context.to_dict())
    assert "SECRET-LOCAL-CONTENT-123" not in dumped
    assert "credentials.txt" not in dumped
    assert "local" not in {k.lower() for k in context.to_dict()}


def test_symlinked_shareable_file_refused(run_cli, project_dir):
    """Export integrity: a symlinked shareable file is refused rather than
    read, so local/ or private content can never be smuggled into the
    exported context (unlike status, which follows symlinks read-only)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    secret = aperio_dir / "local/credentials.txt"
    secret.write_text("SECRET-LOCAL-CONTENT-123")
    (aprio := aperio_dir / "brief.md").unlink()
    aprio.symlink_to(secret)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        load_context(project_dir)
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 1
    assert "SECRET-LOCAL-CONTENT-123" not in out
    assert "SECRET-LOCAL-CONTENT-123" not in err


def test_symlinked_shareable_file_to_outside_refused(
    run_cli, project_dir, tmp_path
):
    run_cli("init", str(project_dir))
    outside = tmp_path / "private-notes.md"
    outside.write_text("PRIVATE-OUTSIDE-CONTENT-456")
    (aprio := project_dir / ".aperio/handoff.md").unlink()
    aprio.symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        load_context(project_dir)
    assert outside.read_text() == "PRIVATE-OUTSIDE-CONTENT-456"


def test_dangling_shareable_symlink_reported_missing(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (aprio := project_dir / ".aperio/brief.md").unlink()
    aprio.symlink_to(project_dir / ".aperio" / "nowhere.md")
    context = load_context(project_dir)
    assert context.brief is None
    assert "brief.md" in context.missing


def test_environment_variables_not_captured(run_cli, project_dir, monkeypatch):
    run_cli("init", str(project_dir))
    token = "ghp_" + "q" * 36
    monkeypatch.setenv("APERIO_TEST_TOKEN", token)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    context = load_context(project_dir)
    assert token not in json.dumps(context.to_dict())


def test_to_dict_schema_is_stable(run_cli, project_dir):
    """The machine contract: top-level keys are pinned (Feature 03 added
    the documented additive 'plan' summary key); the exported context
    never contains the local filesystem path."""
    run_cli("init", str(project_dir))
    context = load_context(project_dir)
    dumped = context.to_dict()
    assert set(dumped.keys()) == {
        "reader_version",
        "state",
        "brief",
        "decisions",
        "handoff",
        "plan",
        "execution",
        "governance",
        "missing",
        "skipped_headings",
        "malformed_entries",
        "duplicate_decision_ids",
    }
    assert dumped["plan"] is None  # no plan created yet: normal state
    assert dumped["execution"] is None  # no runs recorded yet: normal state
    # Feature 06 additive governance block; disabled by default.
    assert dumped["governance"]["enabled"] is False
    assert set(dumped["state"].keys()) == {
        "schema_version",
        "name",
        "created_at",
        "updated_at",
        "phase",
        "aperio_version",
    }
    assert str(project_dir) not in json.dumps(dumped)


def test_backward_compat_feature01_project(run_cli, project_dir):
    """A freshly initialized project (no Feature-02 writes yet) loads
    cleanly: zero decisions, and the v1 skeleton handoff that init
    templates. Truly legacy free-form handoffs (created by aperio 0.1.0)
    are covered in test_handoff.py."""
    run_cli("init", str(project_dir))
    context = load_context(project_dir)
    assert context.decisions == []
    assert context.handoff is not None
    assert context.handoff.structured is True  # template ships v1 sections
    assert context.handoff.fields["current_objective"] == "None noted."


def test_cli_memory_json_machine_readable(run_cli, project_dir):
    run_cli("init", str(project_dir))
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 0, err
    data = json.loads(out)
    assert set(data.keys()) == {
        "reader_version",
        "state",
        "brief",
        "decisions",
        "handoff",
        "plan",
        "execution",
        "governance",
        "missing",
        "skipped_headings",
        "malformed_entries",
        "duplicate_decision_ids",
    }
    assert data["state"]["name"] == project_dir.name
    assert data["plan"] is None  # freshly initialized: no plan yet
    assert data["execution"] is None  # no execution records yet
    assert data["governance"]["enabled"] is False


def test_cli_memory_json_via_subprocess(run_cli, project_dir):
    """Cross-process check that --json output parses standalone."""
    code = run_cli("init", str(project_dir))[0]
    assert code == 0
    result = subprocess.run(
        [sys.executable, "-m", "aperio", "memory", str(project_dir), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["state"]["schema_version"] == 1


def test_cli_memory_human_summary(run_cli, project_dir):
    run_cli("init", str(project_dir))
    code, out, err = run_cli("memory", str(project_dir))
    assert code == 0, err
    assert f"Project:     {project_dir.name}" in out
    assert "Decisions:   0" in out
    assert "Handoff:     structured v1" in out


def test_cli_memory_on_uninitialized(run_cli, tmp_path):
    code, _, err = run_cli("memory", str(tmp_path / "nope"))
    assert code == 1
    assert "not an Aperio project" in err


def test_read_only_guarantee(run_cli, project_dir):
    """load_context must not modify anything (mirrors the Feature-01
    status guarantee)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"

    def snapshot(root):
        out = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            out[rel] = (
                ("dir",) if path.is_dir() else ("file", path.read_bytes())
            )
        return out

    before = snapshot(project_dir)
    load_context(project_dir)
    load_context(project_dir)
    assert snapshot(project_dir) == before


def test_load_context_readonly_path_policy(run_cli, project_dir, tmp_path):
    """Reads follow the read-only policy: symlinked project dirs are OK."""
    run_cli("init", str(project_dir))
    link = tmp_path / "proj-link"
    link.symlink_to(project_dir, target_is_directory=True)
    context = load_context(str(link))
    assert context.state.name == project_dir.name
