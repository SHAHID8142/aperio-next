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


def write_state(aperio_dir: Path, state: ProjectState) -> None:
    """Write state atomically (write to a temp file, then replace).

    ``state.json.tmp`` is a reserved Aperio temporary name. ``Path.replace``
    renames *over* a symlink at the target instead of following it, so the
    final state file cannot be redirected; the temp path itself is checked
    to keep even that intermediate write inside the project.
    """
    target = state_path(aperio_dir)
    tmp = target.with_name(STATE_FILE_NAME + ".tmp")
    if os.path.islink(tmp):
        raise StateError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run 'aperio init' to repair"
        )
    payload = json.dumps(asdict(state), indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)


def read_state(aperio_dir: Path) -> ProjectState:
    """Read and validate ``.aperio/state.json``.

    :raises NotInitializedError: when the state file does not exist.
    :raises StateError: when it is unreadable, invalid, or too new.
    """
    path = state_path(aperio_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotInitializedError(
            f"not an Aperio project: no {path} (run 'aperio init <project-directory>')"
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}"
        ) from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file manually."
        ) from None

    return _validate(data, path)


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
