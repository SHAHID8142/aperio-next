"""Fresh initialization, repeated initialization, and file protection."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

import aperio
from aperio.paths import resolve_target
from aperio.schema import SCHEMA_VERSION

EXPECTED_FILES = (
    ".aperio/state.json",
    ".aperio/brief.md",
    ".aperio/decisions.md",
    ".aperio/handoff.md",
    ".aperio/.gitignore",
    ".aperio/local/README.md",
)


def test_fresh_init_creates_everything(run_cli, project_dir):
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    for rel in EXPECTED_FILES:
        assert (project_dir / rel).is_file(), f"missing {rel}"
    assert (project_dir / ".aperio/local").is_dir()
    assert "initialized" in out


def test_fresh_init_state_content(run_cli, project_dir):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    state = json.loads((project_dir / ".aperio/state.json").read_text())
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["name"] == project_dir.name
    assert state["phase"] == "initialized"
    assert state["aperio_version"] == aperio.__version__
    for key in ("created_at", "updated_at"):
        assert state[key].endswith("Z")
        datetime.fromisoformat(state[key].replace("Z", "+00:00"))


def test_fresh_init_templates_have_content(run_cli, project_dir):
    run_cli("init", str(project_dir))
    brief = (project_dir / ".aperio/brief.md").read_text()
    assert brief.startswith("# Project Brief")
    decisions = (project_dir / ".aperio/decisions.md").read_text()
    assert "Append-only" in decisions
    handoff = (project_dir / ".aperio/handoff.md").read_text()
    assert "Handoff" in handoff


def test_init_creates_missing_parent_directories(run_cli, tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    code, _, err = run_cli("init", str(deep))
    assert code == 0, err
    assert (deep / ".aperio/state.json").is_file()


def test_repeated_init_is_idempotent(run_cli, project_dir):
    run_cli("init", str(project_dir))
    before = {
        rel: (project_dir / rel).read_bytes() for rel in EXPECTED_FILES
    }
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    assert "already initialized" in out
    after = {rel: (project_dir / rel).read_bytes() for rel in EXPECTED_FILES}
    assert before == after, "re-init modified existing files"


def test_reinit_does_not_overwrite_user_edited_content(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/brief.md").write_text("USER CONTENT - do not lose")
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    assert (
        project_dir / ".aperio/brief.md"
    ).read_text() == "USER CONTENT - do not lose"


def test_reinit_repairs_only_missing_files(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/handoff.md").unlink()
    (project_dir / ".aperio/brief.md").write_text("USER CONTENT")
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    assert "created  .aperio/handoff.md" in out
    assert (project_dir / ".aperio/handoff.md").read_text().startswith("# Handoff")
    assert (project_dir / ".aperio/brief.md").read_text() == "USER CONTENT"


def test_init_around_existing_aperio_without_state(run_cli, project_dir):
    """User content in .aperio/ but no state.json: preserve content, add state."""
    (project_dir / ".aperio").mkdir(parents=True)
    (project_dir / ".aperio/brief.md").write_text("USER BRIEF")
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    assert (project_dir / ".aperio/brief.md").read_text() == "USER BRIEF"
    state = json.loads((project_dir / ".aperio/state.json").read_text())
    assert state["schema_version"] == SCHEMA_VERSION


def test_init_aborts_on_corrupt_state_without_overwriting(run_cli, project_dir):
    run_cli("init", str(project_dir))
    corrupt = '{"schema_version": 1, oops'
    (project_dir / ".aperio/state.json").write_text(corrupt)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "corrupt" in err
    assert (project_dir / ".aperio/state.json").read_text() == corrupt


def test_init_aborts_on_newer_schema(run_cli, project_dir):
    run_cli("init", str(project_dir))
    future = json.dumps(
        {
            "schema_version": SCHEMA_VERSION + 1,
            "name": "proj",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "phase": "initialized",
            "aperio_version": "999.0.0",
        }
    )
    (project_dir / ".aperio/state.json").write_text(future)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "newer" in err
    assert (project_dir / ".aperio/state.json").read_text() == future


def test_init_target_is_existing_file(run_cli, tmp_path):
    target = tmp_path / "afile"
    target.write_text("precious")
    code, _, err = run_cli("init", str(target))
    assert code == 1
    assert "not a directory" in err
    assert target.read_text() == "precious"


def test_init_rejects_symlink_target(run_cli, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    code, _, err = run_cli("init", str(link))
    assert code == 1
    assert "symlink" in err
    assert not (real / ".aperio").exists()


def test_init_rejects_symlink_to_file(run_cli, tmp_path):
    target_file = tmp_path / "f.txt"
    target_file.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target_file)
    code, _, err = run_cli("init", str(link))
    assert code == 1
    # The real file must be untouched.
    assert target_file.read_text() == "x"


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="chmod permissions do not restrict the root user",
)
def test_init_into_unwritable_parent(run_cli, tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        code, _, err = run_cli("init", str(parent / "child"))
        assert code == 1
        assert "cannot create" in err
        assert not (parent / "child").exists()
    finally:
        parent.chmod(0o755)


def test_resolve_target_rejects_root():
    with pytest.raises(Exception, match="root"):
        resolve_target("/", for_writing=True)


def test_init_rejects_dangling_symlink_at_context_file(run_cli, project_dir, tmp_path):
    """A dangling symlink at a per-file target must not be written through."""
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/handoff.md").unlink()
    outside = tmp_path / "outside.txt"
    outside_target = tmp_path / "dangle.txt"
    (project_dir / ".aperio/handoff.md").symlink_to(outside_target)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "symlink" in err
    # Nothing was written through the symlink, inside or outside the project.
    assert not outside_target.exists()
    assert not outside.exists()
    assert (project_dir / ".aperio/brief.md").is_file()


def test_init_rejects_symlinked_local_dir(run_cli, project_dir, tmp_path):
    """A symlinked .aperio/local/ must never become a write root."""
    run_cli("init", str(project_dir))
    import shutil

    shutil.rmtree(project_dir / ".aperio/local")
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (project_dir / ".aperio/local").symlink_to(outside_dir, target_is_directory=True)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "symlink" in err
    # The outside directory must not have received any Aperio files.
    assert list(outside_dir.iterdir()) == []


def test_init_rejects_symlinked_aperio_dir(run_cli, project_dir, tmp_path):
    project_dir.mkdir()
    outside = tmp_path / "not-aperio"
    outside.mkdir()
    (project_dir / ".aperio").symlink_to(outside, target_is_directory=True)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "symlink" in err
    assert list(outside.iterdir()) == []


def test_init_rejects_symlinked_state_tmp(run_cli, project_dir, tmp_path):
    """Even the intermediate state temp write must not follow a symlink."""
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/state.json").unlink()
    outside_tmp = tmp_path / "outside.json"
    (project_dir / ".aperio/state.json.tmp").symlink_to(outside_tmp)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 1
    assert "symlink" in err
    assert not outside_tmp.exists()


def test_init_removes_stale_state_tmp(run_cli, project_dir):
    run_cli("init", str(project_dir))
    stale = project_dir / ".aperio/state.json.tmp"
    stale.write_text("garbage from a crashed write")
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    assert "removed  .aperio/state.json.tmp" in out
    assert not stale.exists()
    assert (project_dir / ".aperio/state.json").is_file()


def test_init_reports_resolved_real_path(run_cli, tmp_path):
    """The echoed location must be the kernel-resolved one (e.g. /tmp on macOS)."""
    target = tmp_path / "proj"
    code, out, err = run_cli("init", str(target))
    assert code == 0, err
    resolved = os.path.realpath(target)
    assert str(resolved) in out


def test_resolve_target_expands_home():
    from pathlib import Path
    import os

    resolved = resolve_target("~", for_writing=False)
    assert resolved == Path(os.path.abspath(os.path.expanduser("~")))


def test_resolve_target_makes_absolute():
    resolved = resolve_target("some/relative/path", for_writing=False)
    assert resolved.is_absolute()


@pytest.mark.parametrize("bad", ["", "   "])
def test_resolve_target_rejects_empty(run_cli, bad):
    code, _, err = run_cli("status", bad)
    assert code == 1
    assert "must not be empty" in err
