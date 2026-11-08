"""M2 gate — safe publication of ``aperio goal-pack --output``.

The output file is the only filesystem effect of the standalone goal
workflow, so it gets the same treatment as every other Aperio write:
exclusive creation, no symlink following, no truncation of an existing
occupant, atomic publication where the platform supports it, and refusal to
touch protected project state or the goal it was derived from.

These tests are written to fail against the pre-fix implementation
(predictable temp path opened with ``Path.open``).
"""

from __future__ import annotations

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aperio import artifacts
from aperio.errors import PathError, StateError

MINIMAL = {"objective": "Explain compost", "intended_outcome": "A short brief"}


def _write_goal(directory: Path, **overrides) -> Path:
    path = directory / "goal.json"
    path.write_text(json.dumps({**MINIMAL, **overrides}), encoding="utf-8")
    return path


def _temp_occupants(directory: Path) -> list[str]:
    return sorted(
        entry.name for entry in directory.iterdir()
        if "aperio-tmp" in entry.name or entry.name.endswith(".tmp")
    )


# --------------------------------------------------------------------------
# Temporary-file safety
# --------------------------------------------------------------------------


def test_preexisting_predictable_temp_symlink_is_not_followed(
    run_cli, tmp_path
):
    """A planted symlink at the old temp path must not be opened or truncated."""

    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    planted = tmp_path / f".{target.name}.aperio-tmp"
    planted.symlink_to(victim)

    code, _out, err = run_cli("goal-pack", str(goal), "--output", str(target))
    assert code == 0, err
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert planted.is_symlink()
    assert target.read_text(encoding="utf-8").startswith("# Aperio Context Pack")


def test_leftover_temp_file_is_preserved(run_cli, tmp_path):
    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"
    leftover = tmp_path / f".{target.name}.aperio-tmp"
    leftover.write_text("leftover", encoding="utf-8")

    code, _out, err = run_cli("goal-pack", str(goal), "--output", str(target))
    assert code == 0, err
    assert leftover.read_text(encoding="utf-8") == "leftover"


def test_successful_write_leaves_no_temp_files(run_cli, tmp_path):
    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"
    code, _out, err = run_cli("goal-pack", str(goal), "--output", str(target))
    assert code == 0, err
    assert _temp_occupants(tmp_path) == []


def test_failed_write_leaves_no_temp_files_and_no_destination(
    run_cli, tmp_path, monkeypatch
):
    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"

    def boom(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(artifacts.os, "link", boom)
    monkeypatch.setattr(artifacts.os, "replace", boom)
    code, _out, err = run_cli("goal-pack", str(goal), "--output", str(target))
    assert code == 1
    assert "cannot write" in err
    assert not target.exists()
    assert _temp_occupants(tmp_path) == []


def test_interrupted_forced_write_keeps_previous_content(
    run_cli, tmp_path, monkeypatch
):
    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"
    target.write_text("previous", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(artifacts.os, "replace", boom)
    code, _out, err = run_cli(
        "goal-pack", str(goal), "--output", str(target), "--force"
    )
    assert code == 1
    assert "cannot write" in err
    assert target.read_text(encoding="utf-8") == "previous"
    assert _temp_occupants(tmp_path) == []


def test_concurrent_writers_never_overwrite_each_other(run_cli, tmp_path):
    """Without --force exactly one concurrent writer may publish."""

    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"

    def attempt(_index: int) -> int:
        code, _out, _err = run_cli("goal-pack", str(goal), "--output", str(target))
        return code

    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(attempt, range(5)))
    assert codes.count(0) == 1, codes
    assert set(codes) <= {0, 1}

    from aperio.context_pack import render_context_pack_markdown

    published = target.read_text(encoding="utf-8")
    assert published.startswith("# Aperio Context Pack")
    assert published.rstrip().endswith("Aperio does not execute or verify.") or (
        "Content SHA-256" in published
    )
    assert "aperio-tmp" not in published
    assert _temp_occupants(tmp_path) == []
    assert render_context_pack_markdown  # keep import meaningful


def test_destination_appearing_after_validation_is_not_overwritten(
    tmp_path, monkeypatch
):
    """The no-clobber guarantee must not depend on the earlier existence check."""

    target = tmp_path / "pack.md"

    def sneaky_link(src, dst, **_kwargs):
        Path(dst).write_text("raced", encoding="utf-8")
        raise FileExistsError(17, "File exists")

    monkeypatch.setattr(artifacts.os, "link", sneaky_link)
    with pytest.raises(StateError, match="already exists"):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert target.read_text(encoding="utf-8") == "raced"
    assert _temp_occupants(tmp_path) == []


# --------------------------------------------------------------------------
# P1-01: no-clobber publication must fail closed, never check-then-replace
# --------------------------------------------------------------------------


def _unsupported_link(_src, _dst, **_kwargs):
    raise OSError(getattr(errno, "EOPNOTSUPP", errno.ENOSYS), os.strerror(
        getattr(errno, "EOPNOTSUPP", errno.ENOSYS)
    ))


def test_unsupported_link_with_absent_destination_fails_closed(
    tmp_path, monkeypatch
):
    """No hard links => refuse. Never a fallback replace that can clobber."""

    target = tmp_path / "pack.md"
    monkeypatch.setattr(artifacts.os, "link", _unsupported_link)
    replaced: list[tuple[str, str]] = []
    real_replace = artifacts.os.replace

    def spy_replace(src, dst, **_kwargs):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", spy_replace)
    with pytest.raises(StateError, match="no-overwrite publication"):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert replaced == [], "publication must not fall back to os.replace"
    assert not target.exists()
    assert _temp_occupants(tmp_path) == []


def test_unsupported_link_with_destination_created_during_handling(
    tmp_path, monkeypatch
):
    """A destination created inside the failing link call is never lost."""

    target = tmp_path / "pack.md"

    def racing_link(_src, dst, **_kwargs):
        Path(dst).write_text("raced", encoding="utf-8")
        raise OSError(getattr(errno, "EOPNOTSUPP", errno.ENOSYS), "unsupported")

    monkeypatch.setattr(artifacts.os, "link", racing_link)
    with pytest.raises(StateError, match="no-overwrite publication"):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert target.read_text(encoding="utf-8") == "raced"
    assert _temp_occupants(tmp_path) == []


@pytest.mark.parametrize(
    "code,expected",
    [
        (getattr(errno, "EOPNOTSUPP", errno.ENOSYS), "no-overwrite publication"),
        (errno.ENOSYS, "no-overwrite publication"),
        (errno.EXDEV, "no-overwrite publication"),
        (getattr(errno, "EMLINK", errno.EXDEV), "no-overwrite publication"),
        (errno.EPERM, "not permitted here"),
        (errno.EACCES, "not permitted here"),
        (errno.EIO, "cannot write"),
    ],
)
def test_link_failures_are_classified_and_never_overwrite(
    tmp_path, monkeypatch, code, expected
):
    target = tmp_path / "pack.md"

    def failing_link(_src, _dst, **_kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(artifacts.os, "link", failing_link)
    with pytest.raises(StateError, match=expected):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert not target.exists()
    assert _temp_occupants(tmp_path) == []


def test_link_unsupported_does_not_overwrite_existing_destination(
    tmp_path, monkeypatch
):
    target = tmp_path / "pack.md"
    target.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(artifacts.os, "link", _unsupported_link)
    with pytest.raises(StateError, match="already exists"):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert target.read_text(encoding="utf-8") == "previous"
    assert _temp_occupants(tmp_path) == []


def test_force_publishes_even_when_links_are_unsupported(tmp_path, monkeypatch):
    target = tmp_path / "pack.md"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(artifacts.os, "link", _unsupported_link)
    artifacts.publish_text_atomic(target, "new", force=True)
    assert target.read_text(encoding="utf-8") == "new"
    assert _temp_occupants(tmp_path) == []


def test_link_not_implemented_platform_is_also_fail_closed(tmp_path, monkeypatch):
    target = tmp_path / "pack.md"

    def not_implemented(_src, _dst, **_kwargs):
        raise NotImplementedError("hard links unsupported")

    monkeypatch.setattr(artifacts.os, "link", not_implemented)
    with pytest.raises(StateError, match="no-overwrite publication"):
        artifacts.publish_text_atomic(target, "new content", force=False)
    assert not target.exists()
    assert _temp_occupants(tmp_path) == []


def test_cli_reports_unsupported_no_overwrite_with_actionable_error(
    run_cli, tmp_path, monkeypatch
):
    goal = _write_goal(tmp_path)
    target = tmp_path / "pack.md"
    monkeypatch.setattr(artifacts.os, "link", _unsupported_link)
    code, _out, err = run_cli("goal-pack", str(goal), "--output", str(target))
    assert code == 1
    assert "aperio: error:" in err
    assert "--force" in err
    assert "Traceback" not in err
    assert not target.exists()
    assert _temp_occupants(tmp_path) == []

    code, _out, err = run_cli(
        "goal-pack", str(goal), "--output", str(target), "--force"
    )
    assert code == 0, err
    assert target.read_text(encoding="utf-8").startswith("# Aperio Context Pack")


def test_publish_refuses_symlink_destination_with_force(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    with pytest.raises(PathError, match="symlink"):
        artifacts.publish_text_atomic(link, "new", force=True)
    assert real.read_text(encoding="utf-8") == "keep"


def test_publish_refuses_directory_destination(tmp_path):
    directory = tmp_path / "pack.md"
    directory.mkdir()
    with pytest.raises(PathError, match="directory"):
        artifacts.publish_text_atomic(directory, "new", force=True)


def test_publish_force_replaces_atomically(tmp_path):
    target = tmp_path / "pack.md"
    target.write_text("old", encoding="utf-8")
    artifacts.publish_text_atomic(target, "new", force=True)
    assert target.read_text(encoding="utf-8") == "new"
    assert _temp_occupants(tmp_path) == []


def test_publish_creates_file_readable_by_owner_only(tmp_path):
    target = tmp_path / "pack.md"
    artifacts.publish_text_atomic(target, "content", force=False)
    assert os.stat(target).st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------
# Protected destinations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative", [".aperio/plan.json", ".aperio/state.json", ".aperio/reviews.json"]
)
def test_output_refuses_project_state_paths(run_cli, project_dir, relative):
    code, _out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    goal = _write_goal(project_dir, profile="software")
    target = project_dir / relative
    before = target.read_bytes() if target.exists() else None

    code, _out, err = run_cli(
        "goal-pack", str(goal), "--output", str(target), "--force"
    )
    assert code == 1
    assert ".aperio" in err
    assert (target.read_bytes() if target.exists() else None) == before


def test_output_refuses_project_state_through_symlinked_directory(
    run_cli, project_dir, tmp_path
):
    code, _out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    goal = _write_goal(tmp_path)
    link_dir = tmp_path / "state-link"
    link_dir.symlink_to(project_dir / ".aperio", target_is_directory=True)
    target = link_dir / "state.json"
    before = target.read_bytes()

    code, _out, err = run_cli(
        "goal-pack", str(goal), "--output", str(target), "--force"
    )
    assert code == 1
    assert ".aperio" in err
    assert target.read_bytes() == before


def test_output_refuses_to_overwrite_the_goal_file(run_cli, tmp_path):
    goal = _write_goal(tmp_path)
    before = goal.read_bytes()
    code, _out, err = run_cli(
        "goal-pack", str(goal), "--output", str(goal), "--force"
    )
    assert code == 1
    assert "goal file" in err
    assert goal.read_bytes() == before
