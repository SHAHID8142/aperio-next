"""Versioned project-state schema for Aperio (schema version 1).

``.aperio/state.json`` is the single source of truth for Aperio project
state. It is deliberately tiny, JSON-only, and portable: any agent on any
machine can read it with nothing but a JSON parser.

Schema version 1 fields (all required):

- ``schema_version``  int, must be <= the supported version
- ``name``            str, project name (defaults to the directory name)
- ``created_at``      str, ISO-8601 UTC timestamp
- ``updated_at``      str, ISO-8601 UTC timestamp
- ``phase``           str, coarse project phase (starts as "initialized")
- ``aperio_version``  str, version of Aperio that wrote the file

Unknown extra keys are tolerated (forward compatibility within schema
version 1). A *higher* schema version is never modified: Aperio aborts with
a clear error instead, because it cannot know what a newer writer intended.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import NotInitializedError, StateError

SCHEMA_VERSION = 1
STATE_FILE_NAME = "state.json"

_PHASE_INITIALIZED = "initialized"

_REQUIRED_STR_FIELDS = ("name", "created_at", "updated_at", "phase", "aperio_version")


@dataclass(frozen=True)
class ProjectState:
    """Validated contents of ``.aperio/state.json``."""

    name: str
    created_at: str
    updated_at: str
    phase: str
    aperio_version: str
    schema_version: int = SCHEMA_VERSION


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def build_initial_state(project_dir: Path, version: str) -> ProjectState:
    """Build the state written by a fresh ``aperio init``."""
    now = utc_now_iso()
    return ProjectState(
        name=project_dir.name,
        created_at=now,
        updated_at=now,
        phase=_PHASE_INITIALIZED,
        aperio_version=version,
    )


def state_path(aperio_dir: Path) -> Path:
    """Path of the state file inside an ``.aperio/`` directory."""
    return aperio_dir / STATE_FILE_NAME


def state_tmp_path(aperio_dir: Path) -> Path:
    """Path of the reserved atomic-write temporary file.

    ``state.json.tmp`` is reserved by Aperio: init requires it to be
    unobstructed before any operation. Nothing is ever deleted from this
    name automatically — ownership of a plain file cannot be verified, so
    any occupant (including a leftover from a crashed write) is a conflict
    the user resolves explicitly. The name is git-ignored in new projects.
    """
    return aperio_dir / (STATE_FILE_NAME + ".tmp")


def write_state(aperio_dir: Path, state: ProjectState) -> None:
    """Write state atomically (exclusive temp file, then hard-link publish).

    Safety properties:

    - ``state.json.tmp`` is a reserved Aperio temporary name. The temp file
      is created exclusively (``O_CREAT | O_EXCL``), so a pre-existing file,
      directory, or symlink at that name is never overwritten.
    - The state target itself is protected: an existing regular file is
      never overwritten through this function (state updates must load and
      validate first), and a symlinked target is refused outright.
    - The final publish is ``os.link(tmp, target)``: an atomic
      create-if-not-exists. Even a state file planted between the
      pre-check and the publish is never destroyed — the link fails with
      ``EEXIST`` and the write aborts. Requires a filesystem that supports
      hard links (POSIX filesystems, NTFS); where that is unavailable the
      write fails with a clean, actionable StateError rather than
      degrading into an overwrite.
    - On any failure, only the temp file created by this very call is
      removed; anything found on disk is left untouched for the user.
    - All expected I/O failures surface as StateError with an actionable
      message.
    """
    target = state_path(aperio_dir)
    tmp = state_tmp_path(aperio_dir)
    if os.path.islink(target):
        raise StateError(
            f"refusing to follow symlink at {target}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if os.path.islink(tmp):
        raise StateError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if target.exists():
        raise StateError(
            f"refusing to overwrite existing state file {target}; "
            "load and update state through the schema API instead"
        )
    payload = json.dumps(asdict(state), indent=2) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot write project state at {target}: {exc.strerror or exc}; "
            f"check for a conflicting path at {target} or {tmp}, remove it "
            "manually if it is not Aperio content, and re-run 'aperio init'"
        ) from None

    try:
        os.link(tmp, target)
    except FileExistsError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"refusing to overwrite existing state file {target}: a state "
            "file appeared while the write was in progress; inspect it and "
            "re-run 'aperio init' if appropriate"
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish project state at {target}: {exc.strerror or exc}; "
            "the filesystem may not support hard links; no existing content "
            "was modified"
        ) from None

    # target now shares the temp file's inode; drop our temp name.
    # Best effort: if this fails, the leftover is reported as a conflict by
    # the next init rather than being destroyed here.
    try:
        os.unlink(tmp)
    except OSError:
        pass


def replace_state_if_unchanged(
    aperio_dir: Path, state_data: dict, expected_raw: bytes
) -> None:
    """Explicitly update an existing state file with lost-update detection.

    This is the deliberate state-update operation (Feature 02) — separate
    from :func:`write_state`, which only ever creates fresh state.

    ``state_data`` is the *full* parsed state object (as returned by
    :func:`parse_state_dict`), typically the caller's validated copy with
    only the intended fields modified. Every other key — including unknown
    extra fields from future or third-party writers — is preserved exactly
    in meaning and JSON type: the dict is validated and serialized as-is,
    never reconstructed into the known-field dataclass.

    Sequence: validate the outgoing dict, refuse symlinked targets/temp
    names, write the new state to an exclusive temp file, then compare the
    current target bytes against ``expected_raw`` (the bytes captured when
    the caller read and validated the state). If they differ — or the
    target has vanished — the update is aborted, the temp file is removed,
    and nothing on disk is modified.

    Concurrency guarantee (documented, not absolute): this prevents lost
    updates caused by writes that completed *before* the compare. A write
    landing between the compare and the final rename can still win; callers
    performing concurrent updates must serialize at a higher level.
    """
    target = state_path(aperio_dir)
    tmp = state_tmp_path(aperio_dir)
    # Refuse to publish anything that is not valid schema v1 (or an
    # accepted variant), even if the caller mutated the dict carelessly.
    _validate(state_data, target)
    if os.path.islink(target):
        raise StateError(
            f"refusing to follow symlink at {target}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if os.path.islink(tmp):
        raise StateError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run 'aperio init' to repair"
        )
    payload = json.dumps(state_data, indent=2) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot prepare state update at {tmp}: {exc.strerror or exc}; "
            "the temp name is occupied by a concurrent aperio write (retry "
            "shortly) or a leftover from a crashed write (removing it is "
            "your decision); nothing was modified"
        ) from None

    try:
        current = target.read_bytes()
    except FileNotFoundError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot update state: {target} disappeared during the update; "
            "nothing was modified. Re-run 'aperio init' to repair the project."
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot read {target} for the update check: {exc.strerror or exc}; "
            "nothing was modified"
        ) from None

    if current != expected_raw:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"state at {target} changed while the update was being prepared "
            "(concurrent modification); nothing was modified. Re-read the "
            "state and retry the update."
        )

    try:
        os.replace(tmp, target)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish state update at {target}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    """Remove a temp file this same call created; never touch anything else."""
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover is reported as a conflict by the next init


def read_state(aperio_dir: Path) -> ProjectState:
    """Read and validate ``.aperio/state.json``.

    :raises NotInitializedError: when the state file does not exist.
    :raises StateError: when it is unreadable, invalid, or too new.
    """
    path = state_path(aperio_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise NotInitializedError(
            f"not an Aperio project: no {path} (run 'aperio init <project-directory>')"
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}"
        ) from None

    return parse_state_bytes(raw, path)


def parse_state_dict(raw: bytes, path: Path) -> dict:
    """Validate raw ``state.json`` bytes and return the full parsed object.

    Unlike :func:`parse_state_bytes`, the returned dict keeps **all** keys
    in document order — including unknown extra fields — so deliberate
    update operations can modify only their own fields and preserve
    everything else exactly (meaning and JSON types).
    """
    data = _decode_state(raw, path)
    _validate(data, path)
    return data


def parse_state_bytes(raw: bytes, path: Path) -> ProjectState:
    """Validate raw ``state.json`` bytes into a :class:`ProjectState`.

    Split from :func:`read_state` so callers that need compare-and-swap
    semantics can validate exactly the bytes they captured, with no
    second read in between. Unknown extra keys are tolerated here but not
    represented — use :func:`parse_state_dict` when they must survive a
    rewrite.
    """
    return _validate(_decode_state(raw, path), path)


def _decode_state(raw: bytes, path: Path) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not overwrite "
            "it. Inspect or restore the file manually."
        ) from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file manually."
        ) from None
    return data


def _validate(data: object, path: Path) -> ProjectState:
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: state must be a JSON object")

    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise StateError(
            f"{path} is corrupt: 'schema_version' must be an integer"
        )
    if schema_version > SCHEMA_VERSION:
        raise StateError(
            f"{path} was written with schema version {schema_version}, but this "
            f"aperio supports at most version {SCHEMA_VERSION}. Upgrade aperio "
            "or inspect the project manually; aperio will not modify it."
        )
    if schema_version < 1:
        raise StateError(
            f"{path} uses unrecognized schema version {schema_version}; "
            "aperio will not modify it."
        )

    for field in _REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise StateError(
                f"{path} is corrupt: '{field}' must be a non-empty string"
            )

    for field in ("created_at", "updated_at"):
        try:
            datetime.fromisoformat(data[field])
        except ValueError:
            raise StateError(
                f"{path} is corrupt: '{field}' is not an ISO-8601 timestamp"
            ) from None

    return ProjectState(
        name=data["name"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        phase=data["phase"],
        aperio_version=data["aperio_version"],
        schema_version=schema_version,
    )
