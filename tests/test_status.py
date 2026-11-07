"""Status reporting, persistence, and read-only guarantees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.schema import SCHEMA_VERSION, read_state, write_state, ProjectState
from aperio.errors import NotInitializedError, StateError


def snapshot_tree(root: Path) -> dict:
    """Full content+mtime snapshot of every path under root."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel] = ("dir",)
        else:
            stat = path.stat()
            out[rel] = ("file", path.read_bytes(), stat.st_mtime_ns, stat.st_size)
    return out


def test_status_reports_initialized_project(run_cli, project_dir):
    run_cli("init", str(project_dir))
    code, out, err = run_cli("status", str(project_dir))
    assert code == 0, err
    assert f"Project:     {project_dir.name}" in out
    assert f"Schema:      v{SCHEMA_VERSION}" in out
    assert "Phase:       initialized" in out
    assert "Created:" in out
    assert "Updated:" in out
    assert "present  .aperio/brief.md" in out
    assert "present  .aperio/decisions.md" in out
    assert "present  .aperio/handoff.md" in out
    assert "present  .aperio/local/" in out


def test_status_is_read_only(run_cli, project_dir):
    run_cli("init", str(project_dir))
    before = snapshot_tree(project_dir)
    for _ in range(2):
        code, _, err = run_cli("status", str(project_dir))
        assert code == 0, err
    assert snapshot_tree(project_dir) == before


def test_status_reports_missing_files(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/decisions.md").unlink()
    code, out, err = run_cli("status", str(project_dir))
    assert code == 0, err
    assert "MISSING  .aperio/decisions.md" in out
    assert "aperio init" in out  # repair hint
    # Status itself must not have repaired anything.
    assert not (project_dir / ".aperio/decisions.md").exists()


def test_status_on_uninitialized_directory(run_cli, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run_cli("status", str(empty))
    assert code == 1
    assert "not an Aperio project" in err


def test_status_on_nonexistent_directory(run_cli, tmp_path):
    code, _, err = run_cli("status", str(tmp_path / "nope"))
    assert code == 1
    assert "not an Aperio project" in err


def test_status_on_corrupt_state(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/state.json").write_text("{broken")
    code, _, err = run_cli("status", str(project_dir))
    assert code == 1
    assert "corrupt" in err


def test_status_works_through_symlink_target(run_cli, project_dir, tmp_path):
    """status is read-only, so symlinked targets are allowed for reading."""
    run_cli("init", str(project_dir))
    link = tmp_path / "proj-link"
    link.symlink_to(project_dir, target_is_directory=True)
    code, out, err = run_cli("status", str(link))
    assert code == 0, err
    assert f"Project:     {project_dir.name}" in out


def test_state_roundtrip(tmp_path):
    aperio_dir = tmp_path / ".aperio"
    aperio_dir.mkdir()
    state = ProjectState(
        name="demo",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        phase="initialized",
        aperio_version="0.1.0",
    )
    write_state(aperio_dir, state)
    loaded = read_state(aperio_dir)
    assert loaded == state


def test_read_state_missing(tmp_path):
    with pytest.raises(NotInitializedError):
        read_state(tmp_path)


def test_read_state_invalid_json(tmp_path):
    (tmp_path / "state.json").write_text("{nope")
    with pytest.raises(StateError, match="corrupt"):
        read_state(tmp_path)


def test_read_state_not_an_object(tmp_path):
    (tmp_path / "state.json").write_text("[]")
    with pytest.raises(StateError, match="JSON object"):
        read_state(tmp_path)


def test_read_state_rejects_non_integer_schema(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": "1"}))
    with pytest.raises(StateError, match="schema_version"):
        read_state(tmp_path)


def test_read_state_rejects_boolean_schema(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": True}))
    with pytest.raises(StateError, match="schema_version"):
        read_state(tmp_path)


def test_read_state_rejects_version_zero(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": 0}))
    with pytest.raises(StateError, match="unrecognized"):
        read_state(tmp_path)


def test_read_state_rejects_missing_fields(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(StateError, match="name"):
        read_state(tmp_path)


def test_read_state_rejects_bad_timestamp(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "x",
                "created_at": "not-a-date",
                "updated_at": "2026-01-01T00:00:00Z",
                "phase": "initialized",
                "aperio_version": "0.1.0",
            }
        )
    )
    with pytest.raises(StateError, match="created_at"):
        read_state(tmp_path)


def test_read_state_tolerates_unknown_fields(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "x",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "phase": "initialized",
                "aperio_version": "0.1.0",
                "future_extension": {"any": "thing"},
            }
        )
    )
    loaded = read_state(tmp_path)
    assert loaded.name == "x"


def test_state_file_written_with_trailing_newline_and_indent(run_cli, project_dir):
    run_cli("init", str(project_dir))
    raw = (project_dir / ".aperio/state.json").read_text()
    assert raw.endswith("\n")
    assert '\n  "name"' in raw
