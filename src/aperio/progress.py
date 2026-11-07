"""Project progress updates (Feature 02).

``update_phase`` is the *deliberate* state-update operation — separate from
Feature 01's create-only ``write_state``, whose protections are unchanged.

Semantics:

- The new phase must be a non-empty single-line string (schema v1 keeps
  phases free-form).
- Setting the same phase is an idempotent no-op success that does not bump
  ``updated_at``.
- A real change bumps ``phase``, ``updated_at`` (UTC now) and stamps
  ``aperio_version`` with the writing version — the file was rewritten by
  that version; ``schema_version`` governs compatibility and stays 1.
  ``name`` and ``created_at`` are preserved exactly, and so is every other
  field in the state object: the update mutates only its own three keys in
  the parsed document and re-serializes it, so unknown extra fields from
  other writers survive byte-for-byte in meaning and JSON type.

Concurrency (accurate guarantees): the entire read → validate → modify →
publish sequence runs under the advisory ``.aperio/state.lock`` (bounded
wait; a lock is never stolen — an occupied lock past the timeout produces
a clear conflict and nothing is modified), so concurrent *aperio* updates
serialize and cannot silently overwrite each other. Inside the lock, the
publish is still guarded by a byte compare-and-swap against the exact
bytes read, which detects a write from a tool that ignores aperio's
coordination. A write landing between that final compare and the rename
can still win — the filesystem cannot give absolute protection against
non-cooperating writers. Crash-consistency is best-effort (no fsync). A
leftover ``state.json.tmp`` is a conflict resolved by explicit user
action, never auto-deleted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aperio import __version__
from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.locks import LOCK_TIMEOUT_SECONDS, acquire_lock, release_lock
from aperio.paths import resolve_target
from aperio.project import aperio_dir_for, require_supported_state
from aperio.schema import (
    parse_state_dict,
    replace_state_if_unchanged,
    state_path,
    utc_now_iso,
)

STATE_LOCK_NAME = "state.lock"


@dataclass(frozen=True)
class UpdatePhaseResult:
    """Outcome of a phase update (``updated`` is False for a no-op)."""

    previous_phase: str
    new_phase: str
    updated: bool


def update_phase(
    project_path: str | Path,
    new_phase: object,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> UpdatePhaseResult:
    """Update the project phase under coordination, with lost-update
    detection.

    :raises ValidationError: when the phase is not a non-empty single line.
    :raises NotInitializedError: when the project is not initialized.
    :raises StateError: when state is corrupt/unsupported, a concurrent
        modification is detected, or the state lock cannot be acquired
        within the bounded wait.
    :raises PathError: when the project path, the ``.aperio/`` directory,
        the lock, or the state file is unsafe.
    """
    if not isinstance(new_phase, str) or not new_phase.strip():
        raise ValidationError("phase must be a non-empty string")
    phase = new_phase.strip()
    if "\n" in phase:
        raise ValidationError("phase must be a single line")

    target = resolve_target(str(project_path), for_writing=True)
    aperio_dir = aperio_dir_for(target)
    path = state_path(aperio_dir)

    # Boundary first: a genuine initialized project with a supported
    # schema, a non-symlinked .aperio/ directory, and a non-symlinked
    # state file — before any lock or write activity.
    require_supported_state(aperio_dir)

    lock_path = aperio_dir / STATE_LOCK_NAME
    token = acquire_lock(lock_path, timeout=lock_timeout, artifact="state update")
    try:
        # Defense-in-depth under the lock (mirrors the sibling writers):
        # re-check for a symlink swapped in between the boundary check and
        # this read. Publication is separately guarded by
        # replace_state_if_unchanged's own symlink refusal.
        if os.path.islink(path):
            raise PathError(
                f"refusing to follow symlink at {path}; "
                "remove it and re-run 'aperio init' to repair"
            )
        # Single read under the lock: the state is validated from exactly
        # the bytes the compare-and-swap will check against. The full
        # document (including unknown extra fields) is kept for the
        # rewrite.
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise NotInitializedError(
                f"not an Aperio project: no {path} "
                "(run 'aperio init <project-directory>')"
            ) from None
        except OSError as exc:
            raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
        state_data = parse_state_dict(raw, path)

        previous_phase = state_data["phase"]
        if phase == previous_phase:
            return UpdatePhaseResult(
                previous_phase=phase, new_phase=phase, updated=False
            )

        state_data["phase"] = phase
        state_data["updated_at"] = utc_now_iso()
        state_data["aperio_version"] = __version__
        replace_state_if_unchanged(aperio_dir, state_data, expected_raw=raw)
        return UpdatePhaseResult(
            previous_phase=previous_phase, new_phase=phase, updated=True
        )
    finally:
        release_lock(lock_path, token)
