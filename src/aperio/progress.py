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

Concurrency (accurate guarantees): the update is validated from the exact
bytes it will compare against (single read), written to an exclusive temp
file, and published only if the file still matches those bytes
(compare-and-swap). A concurrent update that lands *before* the compare is
detected and aborts with nothing modified; a write landing between the
compare and the rename can still win — callers doing concurrent updates
must serialize at a higher level. Crash-consistency is best-effort (no
fsync). A leftover ``state.json.tmp`` is a conflict resolved by explicit
user action, never auto-deleted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aperio import __version__
from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.paths import resolve_target
from aperio.project import aperio_dir_for
from aperio.schema import (
    parse_state_dict,
    replace_state_if_unchanged,
    state_path,
    utc_now_iso,
)


@dataclass(frozen=True)
class UpdatePhaseResult:
    """Outcome of a phase update (``updated`` is False for a no-op)."""

    previous_phase: str
    new_phase: str
    updated: bool


def update_phase(project_path: str | Path, new_phase: object) -> UpdatePhaseResult:
    """Update the project phase with lost-update detection.

    :raises ValidationError: when the phase is not a non-empty single line.
    :raises NotInitializedError: when the project is not initialized.
    :raises StateError: when state is corrupt/unsupported or a concurrent
        modification is detected.
    :raises PathError: when the project path, the ``.aperio/`` directory,
        or the state file is unsafe.
    """
    if not isinstance(new_phase, str) or not new_phase.strip():
        raise ValidationError("phase must be a non-empty string")
    phase = new_phase.strip()
    if "\n" in phase:
        raise ValidationError("phase must be a single line")

    target = resolve_target(str(project_path), for_writing=True)
    aperio_dir = aperio_dir_for(target)
    path = state_path(aperio_dir)
    if os.path.islink(aperio_dir):
        raise PathError(
            f"refusing to follow symlink at {aperio_dir}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio init' to repair"
        )

    # Single read: the state is validated from exactly the bytes the
    # compare-and-swap will check against. The full document (including
    # unknown extra fields) is kept for the rewrite.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise NotInitializedError(
            f"not an Aperio project: no {path} (run 'aperio init <project-directory>')"
        ) from None
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
    state_data = parse_state_dict(raw, path)

    previous_phase = state_data["phase"]
    if phase == previous_phase:
        return UpdatePhaseResult(previous_phase=phase, new_phase=phase, updated=False)

    state_data["phase"] = phase
    state_data["updated_at"] = utc_now_iso()
    state_data["aperio_version"] = __version__
    replace_state_if_unchanged(aperio_dir, state_data, expected_raw=raw)
    return UpdatePhaseResult(
        previous_phase=previous_phase, new_phase=phase, updated=True
    )
