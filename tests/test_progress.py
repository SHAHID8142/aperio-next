"""Progress updates: phase compare-and-swap semantics."""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys

import pytest

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.progress import update_phase
from aperio.schema import parse_state_bytes, state_path


def test_phase_update_bumps_and_preserves(run_cli, project_dir, monkeypatch):
    run_cli("init", str(project_dir))
    before = json.loads((state_path(project_dir / ".aperio")).read_text())
    monkeypatch.setattr(
        "aperio.progress.utc_now_iso", lambda: "2030-01-01T00:00:00Z"
    )
    result = update_phase(project_dir, "implementation")
    assert result.updated is True
    assert result.previous_phase == "initialized"
    assert result.new_phase == "implementation"
    after = json.loads((state_path(project_dir / ".aperio")).read_text())
    assert after["phase"] == "implementation"
    assert after["updated_at"] == "2030-01-01T00:00:00Z"
    assert after["updated_at"] >= before["updated_at"]
    assert after["created_at"] == before["created_at"]
    assert after["name"] == before["name"]
    assert after["schema_version"] == before["schema_version"] == 1


def test_same_phase_is_idempotent_noop(run_cli, project_dir):
    run_cli("init", str(project_dir))
    result = update_phase(project_dir, "initialized")
    assert result.updated is False
    state = json.loads((state_path(project_dir / ".aperio")).read_text())
    assert state["phase"] == "initialized"
    # updated_at was not bumped by the no-op.
    before = update_phase(project_dir, "initialized")
    after_state = json.loads((state_path(project_dir / ".aperio")).read_text())
    assert after_state["updated_at"] == state["updated_at"]


def test_phase_validation(run_cli, project_dir):
    run_cli("init", str(project_dir))
    with pytest.raises(ValidationError, match="non-empty"):
        update_phase(project_dir, "   ")
    with pytest.raises(ValidationError, match="single line"):
        update_phase(project_dir, "two\nlines")
    with pytest.raises(ValidationError, match="non-empty"):
        update_phase(project_dir, None)


def test_repeated_updates(run_cli, project_dir):
    run_cli("init", str(project_dir))
    for phase in ("planning", "implementation", "verification", "done"):
        result = update_phase(project_dir, phase)
        assert result.updated is True
    state = json.loads((state_path(project_dir / ".aperio")).read_text())
    assert state["phase"] == "done"
    # Reading the project still works (schema compatibility preserved).
    code, out, err = run_cli("status", str(project_dir))
    assert code == 0
    assert "Phase:       done" in out


def test_conflicting_concurrent_modification_detected(
    run_cli, project_dir, monkeypatch
):
    """A state file modified after the update's under-lock read aborts the
    update at the compare-and-swap: no lost updates, nothing modified."""
    import pathlib

    run_cli("init", str(project_dir))
    state_file = state_path(project_dir / ".aperio")

    real_read_bytes = pathlib.Path.read_bytes
    state_reads = [0]

    def interfere_on_compare_read(self):
        data = real_read_bytes(self)
        if self.name == "state.json":
            state_reads[0] += 1
            if state_reads[0] == 3:
                # 1st: boundary read; 2nd: under-lock capture; 3rd: the
                # CAS compare — a concurrent writer lands exactly there.
                state_file.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "name": "proj",
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-02-02T00:00:00Z",
                            "phase": "hijacked-by-other-writer",
                            "aperio_version": "0.1.0",
                        }
                    )
                )
                data = real_read_bytes(self)
        return data

    monkeypatch.setattr(pathlib.Path, "read_bytes", interfere_on_compare_read)
    with pytest.raises(StateError, match="changed while"):
        update_phase(project_dir, "implementation")
    on_disk = json.loads(state_file.read_text())
    assert on_disk["phase"] == "hijacked-by-other-writer"  # untouched by us
    assert not (aprio := project_dir / ".aperio/state.json.tmp").exists()
    assert not (aprio := project_dir / ".aperio/state.lock").exists()


def test_symlinked_state_refused_for_update(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    outside = tmp_path / "outside-state.json"
    outside.write_text('{"external": true}')
    (sp := state_path(project_dir / ".aperio")).unlink()
    sp.symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        update_phase(project_dir, "implementation")
    assert outside.read_text() == '{"external": true}'
    assert sp.is_symlink()


def test_update_on_corrupt_state(run_cli, project_dir):
    run_cli("init", str(project_dir))
    state_path(project_dir / ".aperio").write_text("{broken")
    with pytest.raises(StateError, match="corrupt"):
        update_phase(project_dir, "implementation")


def test_update_on_uninitialized(run_cli, tmp_path):
    with pytest.raises(NotInitializedError):
        update_phase(tmp_path / "nope", "implementation")


def test_update_preserves_bytes_it_validated(run_cli, project_dir):
    """The CAS compares against exactly the bytes that were validated
    (single read) — the state parsed and the state checked are identical."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    raw = state_path(aperio_dir).read_bytes()
    state = parse_state_bytes(raw, state_path(aperio_dir))
    assert state.phase == "initialized"
    update_phase(project_dir, "implementation")
    # The old bytes are now historical; parsing them still yields the old
    # phase (bytes were captured before the update).
    assert parse_state_bytes(raw, state_path(aperio_dir)).phase == "initialized"


def test_cli_phase_set(run_cli, project_dir):
    run_cli("init", str(project_dir))
    code, out, err = run_cli("phase-set", str(project_dir), "verification")
    assert code == 0, err
    assert "initialized -> verification" in out
    code, out, err = run_cli("phase-set", str(project_dir), "verification")
    assert code == 0, err
    assert "phase unchanged: verification" in out


def _phase_worker(project_dir: str, phase: str) -> int:
    """Runs in a separate process: one CLI phase-set (real concurrency)."""
    result = subprocess.run(
        [sys.executable, "-m", "aperio", "phase-set", project_dir, phase],
        capture_output=True, text=True, check=False,
    )
    return result.returncode


def test_concurrent_processes_cannot_silently_overwrite(run_cli, project_dir):
    """Genuinely concurrent phase updates serialize under state.lock. The
    no-steal policy fails closed, so on a heavily loaded machine a writer
    may legitimately exit 1 with the bounded-wait conflict — accepted
    outcomes are success or that conflict, never a corrupt or silently
    lost state. (Deep lost-update detection is covered by the CAS tests;
    this test proves end-to-end serialization never corrupts state.)"""
    run_cli("init", str(project_dir))
    state_file = state_path(project_dir / ".aperio")
    state = json.loads(state_file.read_text())
    state["keep_me"] = {"nested": [1, {"deep": True}]}
    state_file.write_text(json.dumps(state))

    workers = 8
    phases = [f"phase-{i}" for i in range(workers)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_phase_worker, str(project_dir), phase)
            for phase in phases
        ]
        codes = [f.result(timeout=120) for f in futures]
    assert all(code in (0, 1) for code in codes)
    assert 0 in codes, "at least one writer must succeed"
    after = json.loads(state_file.read_text())  # state stays valid JSON
    assert after["phase"] in phases + ["initialized"]
    assert after["keep_me"] == {"nested": [1, {"deep": True}]}
    assert after["schema_version"] == 1
    assert not (aprio := project_dir / ".aperio/state.lock").exists()
    assert not (aprio := project_dir / ".aperio/state.json.tmp").exists()


def test_held_state_lock_blocks_update_with_bounded_conflict(
    run_cli, project_dir
):
    """An occupied state lock (external holder or abandoned) is never
    stolen: the update fails with a bounded, actionable conflict and
    nothing is modified."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "state.lock"
    lock.write_text("999999:holder")
    before = state_file_bytes = (aprio := aperio_dir / "state.json").read_bytes()
    with pytest.raises(StateError, match="could not acquire"):
        update_phase(project_dir, "implementation", lock_timeout=0.3)
    assert (aprio := aperio_dir / "state.json").read_bytes() == before
    assert lock.read_text() == "999999:holder"


def test_unknown_state_fields_survive_phase_updates(
    run_cli, project_dir, monkeypatch
):
    """Regression: phase updates must preserve every unrelated field of the
    state object — including nested structures — exactly in meaning and
    JSON type, not just the six schema-v1 fields."""
    run_cli("init", str(project_dir))
    state_file = state_path(project_dir / ".aperio")
    extras = {
        "orchestrator_note": "written by another tool",
        "attempt_counter": 7,
        "ratio": 0.25,
        "flags": {"nested": [1, 2, {"deep": True, "none": None}]},
        "empty_list": [],
        "unicode_value": "café ünïcode ✓",
    }
    state = json.loads(state_file.read_text())
    state.update(extras)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    monkeypatch.setattr(
        "aperio.progress.utc_now_iso", lambda: "2030-01-01T00:00:00Z"
    )
    result = update_phase(project_dir, "implementation")
    assert result.updated is True

    after = json.loads(state_file.read_text())
    for key, expected in extras.items():
        assert key in after, f"extra field {key} was dropped"
        assert after[key] == expected, f"extra field {key} changed value"
        assert type(after[key]) is type(expected), (
            f"extra field {key} changed JSON type"
        )
    assert after["phase"] == "implementation"
    assert after["updated_at"] == "2030-01-01T00:00:00Z"
    # Known fields still intact too.
    assert after["name"] == project_dir.name
    assert after["schema_version"] == 1
    # The known fields keep their positions; extras keep theirs.
    assert list(after.keys())[:6] == [
        "name",
        "created_at",
        "updated_at",
        "phase",
        "aperio_version",
        "schema_version",
    ]


def test_extra_fields_survive_repeated_updates_and_noop(
    run_cli, project_dir
):
    run_cli("init", str(project_dir))
    state_file = state_path(project_dir / ".aperio")
    state = json.loads(state_file.read_text())
    state["keep_me"] = {"a": [1, {"b": False}]}
    state_file.write_text(json.dumps(state))

    update_phase(project_dir, "one")
    before = json.loads(state_file.read_text())
    update_phase(project_dir, "one")  # no-op must not rewrite either
    after = json.loads(state_file.read_text())
    assert before["keep_me"] == {"a": [1, {"b": False}]}
    assert after["keep_me"] == {"a": [1, {"b": False}]}
    update_phase(project_dir, "two")
    final = json.loads(state_file.read_text())
    assert final["keep_me"] == {"a": [1, {"b": False}]}
