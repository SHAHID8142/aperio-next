"""Project-boundary enforcement for all memory operations.

Every memory operation (memory, decision-add, handoff-write, phase-set —
library and CLI alike) must establish that it is operating on a valid
initialized project with a supported state schema, and must refuse an
``.aperio/`` directory that is itself a symlink — a leaf-file check alone
would pass while every access silently traverses the link.
"""

from __future__ import annotations

import json
import shutil

import pytest

from aperio.decisions import append_decision, read_decisions
from aperio.errors import NotInitializedError, PathError, StateError
from aperio.handoff import HandoffContent, read_handoff, write_handoff
from aperio.memory import load_context
from aperio.progress import update_phase
from aperio.schema import state_path

CONTENT = dict(
    current_objective="o",
    completed_work="c",
    work_in_progress="w",
    next_recommended_action="n",
    blockers="b",
    verification_evidence="v",
    verification_limitations="l",
)

DECISION_KWARGS = dict(
    title="t", context="c", decision="d", consequences="x"
)


@pytest.fixture
def redirected_project(tmp_path):
    """A real project whose .aperio/ has been replaced by a symlink to an
    outside directory."""
    real_project = tmp_path / "real-project"
    outside = tmp_path / "outside-aperio"
    outside.mkdir()
    run_init = real_project / ".aperio"
    # Build the real project first, then move .aperio outside and link to it.
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-m", "aperio", "init", str(real_project)],
        capture_output=True, text=True, check=True,
    )
    shutil.move(str(run_init), str(outside))
    run_init.symlink_to(outside, target_is_directory=True)
    return real_project, outside


def _assert_all_operations_refuse(project):
    with pytest.raises(PathError, match="symlink"):
        load_context(project)
    with pytest.raises(PathError, match="symlink"):
        append_decision(project / ".aperio", **DECISION_KWARGS)
    with pytest.raises(PathError, match="symlink"):
        write_handoff(project / ".aperio", HandoffContent(**CONTENT))
    with pytest.raises(PathError, match="symlink"):
        update_phase(project, "implementation")
    with pytest.raises(PathError, match="symlink"):
        read_decisions(project / ".aperio")
    with pytest.raises(PathError, match="symlink"):
        read_handoff(project / ".aperio")


def test_symlinked_aperio_refused_for_all_operations(redirected_project):
    project, outside = redirected_project
    _assert_all_operations_refuse(project)
    # Nothing was written through the link: the outside directory holds
    # exactly what the moved init created, nothing more.
    assert not (outside / "decisions.lock").exists()
    assert not (outside / "handoff.lock").exists()
    assert not (outside / "handoff.md.tmp").exists()


def test_symlinked_aperio_via_cli(redirected_project):
    import subprocess
    import sys

    project, outside = redirected_project
    before = sorted(p.name for p in outside.iterdir())
    for argv in (
        ["memory", str(project)],
        ["phase-set", str(project), "impl"],
    ):
        result = subprocess.run(
            [sys.executable, "-m", "aperio", *argv],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 1, argv
        assert "symlink" in result.stderr
    assert sorted(p.name for p in outside.iterdir()) == before


def test_uninitialized_dir_with_preexisting_context_files(
    run_cli, tmp_path
):
    """A directory with an .aperio/brief.md but no state.json is NOT a
    project: every memory operation refuses, and the user's pre-existing
    files are preserved byte-for-byte with nothing else created."""
    project = tmp_path / "half-project"
    aperio_dir = project / ".aperio"
    aperio_dir.mkdir(parents=True)
    user_brief = aperio_dir / "brief.md"
    user_brief.write_text("MY UNRELATED USER BRIEF")

    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        load_context(project)
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        append_decision(aperio_dir, **DECISION_KWARGS)
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        write_handoff(aperio_dir, HandoffContent(**CONTENT))
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        update_phase(project, "impl")
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        read_decisions(aperio_dir)
    with pytest.raises(NotInitializedError, match="not an Aperio project"):
        read_handoff(aperio_dir)

    assert user_brief.read_text() == "MY UNRELATED USER BRIEF"
    assert sorted(p.name for p in aperio_dir.iterdir()) == ["brief.md"]


def test_cli_uninitialized_with_preexisting_files(run_cli, tmp_path):
    project = tmp_path / "half-project"
    aperio_dir = project / ".aperio"
    aperio_dir.mkdir(parents=True)
    (aprio := aperio_dir / "brief.md").write_text("USER BRIEF")
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(DECISION_KWARGS))
    handoff_file = tmp_path / "handoff.json"
    handoff_file.write_text(json.dumps(CONTENT))

    for argv in (
        ["memory", str(project)],
        ["phase-set", str(project), "impl"],
        ["decision-add", str(project), "--file", str(decision_file)],
        ["handoff-write", str(project), "--file", str(handoff_file)],
    ):
        code, _, err = run_cli(*argv)
        assert code == 1, argv
        assert "not an Aperio project" in err
    assert (aprio := aperio_dir / "brief.md").read_text() == "USER BRIEF"
    assert sorted(p.name for p in aperio_dir.iterdir()) == ["brief.md"]


def test_corrupt_state_refused_for_all_operations(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "state.json").write_text("{broken")
    with pytest.raises(StateError, match="corrupt"):
        load_context(project_dir)
    with pytest.raises(StateError, match="corrupt"):
        append_decision(aperio_dir, **DECISION_KWARGS)
    with pytest.raises(StateError, match="corrupt"):
        write_handoff(aperio_dir, HandoffContent(**CONTENT))
    with pytest.raises(StateError, match="corrupt"):
        update_phase(project_dir, "impl")
    # The corrupt file was not modified by any refusal.
    assert (aprio := aperio_dir / "state.json").read_text() == "{broken"


def test_newer_schema_refused_for_all_operations(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    future = {
        "schema_version": 99,
        "name": "proj",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "phase": "initialized",
        "aperio_version": "99.0.0",
    }
    (aprio := aperio_dir / "state.json").write_text(json.dumps(future))
    for operation in (
        lambda: load_context(project_dir),
        lambda: append_decision(aperio_dir, **DECISION_KWARGS),
        lambda: write_handoff(aperio_dir, HandoffContent(**CONTENT)),
        lambda: update_phase(project_dir, "impl"),
        lambda: read_decisions(aperio_dir),
        lambda: read_handoff(aperio_dir),
    ):
        with pytest.raises(StateError, match="newer"):
            operation()
    assert (aprio := aperio_dir / "state.json").read_text() == json.dumps(future)


def test_outside_files_preserved_when_aperio_symlinked(
    run_cli, project_dir, tmp_path
):
    """A symlinked .aperio pointing at an arbitrary outside directory must
    never cause writes there — all outside files are preserved."""
    run_cli("init", str(project_dir))
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    (outside / "unrelated.txt").write_text("UNRELATED-OUTSIDE-CONTENT")
    real_aperio = project_dir / ".aperio-real-backup"
    shutil.move(str(project_dir / ".aperio"), str(real_aperio))
    (aprio := project_dir / ".aperio").symlink_to(outside, target_is_directory=True)

    _assert_all_operations_refuse(project_dir)
    assert (outside / "unrelated.txt").read_text() == "UNRELATED-OUTSIDE-CONTENT"
    # The real context was never touched through the link either.
    assert (real_aperio / "state.json").is_file()


def test_symlinked_state_json_refused_for_memory_reads(run_cli, project_dir, tmp_path):
    """Read-side boundary: a symlinked state.json is refused even when its
    target is a perfectly valid state file — portable memory reads never
    follow links at shareable paths (status still does, by design)."""
    run_cli("init", str(project_dir))
    valid_target = tmp_path / "valid-state.json"
    valid_target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "elsewhere",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "phase": "initialized",
                "aperio_version": "0.2.0",
            }
        )
    )
    (sp := state_path(project_dir / ".aperio")).unlink()
    sp.symlink_to(valid_target)
    for reader in (
        lambda: load_context(project_dir),
        lambda: update_phase(project_dir, "impl"),
        lambda: read_decisions(project_dir / ".aperio"),
        lambda: read_handoff(project_dir / ".aperio"),
    ):
        with pytest.raises(PathError, match="refusing to follow symlink"):
            reader()
    # The target file was never read into context nor modified.
    assert json.loads(valid_target.read_text())["name"] == "elsewhere"


def test_symlinked_context_files_into_local_refused_for_reads(
    run_cli, project_dir
):
    """Links from shareable paths into .aperio/local/ must never be read
    into portable context."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    secret = aperio_dir / "local/credentials.txt"
    secret.write_text("SECRET-LOCAL-CONTENT-789")

    (aprio := aperio_dir / "brief.md").unlink()
    aprio.symlink_to(secret)
    (aprio := aperio_dir / "decisions.md").unlink()
    aprio.symlink_to(secret)
    (aprio := aperio_dir / "handoff.md").unlink()
    aprio.symlink_to(secret)

    with pytest.raises(PathError, match="refusing to follow symlink"):
        load_context(project_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_decisions(aperio_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_handoff(aperio_dir)
    code, out, err = run_cli("memory", str(project_dir), "--json")
    assert code == 1
    assert "SECRET-LOCAL-CONTENT-789" not in out + err
    # The link targets were never modified.
    assert secret.read_text() == "SECRET-LOCAL-CONTENT-789"


def test_symlinked_context_files_outside_refused_for_reads(
    run_cli, project_dir, tmp_path
):
    """Links from shareable paths to outside private files must never be
    read into portable context, and the outside files stay untouched."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    private = tmp_path / "private-notes.md"
    private.write_text("PRIVATE-OUTSIDE-NOTES-456")

    (aprio := aperio_dir / "decisions.md").unlink()
    aprio.symlink_to(private)
    (aprio := aperio_dir / "handoff.md").unlink()
    aprio.symlink_to(private)

    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_decisions(aperio_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        read_handoff(aperio_dir)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        load_context(project_dir)
    assert private.read_text() == "PRIVATE-OUTSIDE-NOTES-456"
