"""Core project operations: ``init`` and ``status``.

Layout created by ``init`` inside the project directory::

    <project-dir>/
    ├── .aperio/
    │   ├── state.json        versioned state (single source of truth)
    │   ├── brief.md          project brief (shareable)
    │   ├── decisions.md      append-only decision log (shareable)
    │   ├── handoff.md        handoff notes for the next agent (shareable)
    │   ├── .gitignore        keeps local/ out of any git history
    │   └── local/            machine-local + sensitive data (NOT shareable)
    │       └── README.md
    └── (user content, untouched)

Safety rules implemented here:

- Existing files are never overwritten. ``init`` only creates files that
  are missing, so re-running it repairs a damaged project without touching
  user content.
- If ``state.json`` exists it is validated first. A corrupt or unsupported
  state aborts ``init`` before anything else is written.
- All writes happen inside the resolved project directory only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from aperio import __version__
from aperio.errors import PathError
from aperio.paths import resolve_target
from aperio.schema import (
    SCHEMA_VERSION,
    ProjectState,
    build_initial_state,
    read_state,
    state_path,
    write_state,
)

APERIO_DIR_NAME = ".aperio"
LOCAL_DIR_NAME = "local"

# Shareable context files, in documentation/report order.
CONTEXT_FILES: tuple[str, ...] = ("brief.md", "decisions.md", "handoff.md")

_STATE = "state.json"
_GITIGNORE = ".gitignore"
_LOCAL_README = f"{LOCAL_DIR_NAME}/README.md"


@dataclass
class InitResult:
    """What ``init`` did, for reporting."""

    target: Path
    already_initialized: bool
    created: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    state: ProjectState | None = None


@dataclass
class StatusReport:
    """What ``status`` found, for reporting."""

    target: Path
    state: ProjectState
    files: dict[str, bool]


# --- Templates ---------------------------------------------------------------
# Templates must never contain credential-like values or placeholders that
# invite storing secrets; .aperio/local/ is the only sanctioned place for
# sensitive data and it is git-ignored (enforced by tests/test_no_secrets.py).

BRIEF_TEMPLATE = """\
# Project Brief

Describe what this project is building and why, briefly enough that any
agent can absorb it in under a minute.

## Goal

## Out of scope

## Success criteria
"""

DECISIONS_TEMPLATE = """\
# Decision Log

Append-only. Never rewrite or delete past entries; add a new entry that
supersedes an old one instead.

Entry format:

## YYYY-MM-DD - Short title

- **Context:** why this decision came up
- **Decision:** what was decided
- **Consequences:** what this commits the project to
"""

HANDOFF_TEMPLATE = """\
# Handoff Notes

Write whatever the next agent or session needs to continue: current phase,
work in progress, known risks, open questions.

This file describes the *current* handoff: replace its content (do not
append) when handing off. Permanent history belongs in decisions.md.
"""

APERIO_GITIGNORE_TEMPLATE = """\
# Machine-local and sensitive data must never leave this project's disk
# (and never enter any VCS or shared context).
local/
"""

LOCAL_README_TEMPLATE = """\
# Local Data (not shared)

Everything in this directory is machine-local and sensitive-data territory:

- ignored by git via the sibling .gitignore file
- never copied into state.json, brief, decisions, or handoff files
- never published or synced with other machines or agents
- the only place under .aperio/ where credentials or machine-specific
  configuration may ever be stored, and only when a tool requires it
"""


# --- helpers ------------------------------------------------------------------


def aperio_dir_for(target: Path) -> Path:
    """The ``.aperio/`` directory of a project."""
    return target / APERIO_DIR_NAME


def _reject_symlink(path: Path) -> None:
    """Refuse any write target that is a symlink, dangling or not.

    Writing through a symlink could silently redirect content outside the
    project directory. Removal is the user's explicit choice.
    """
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio init' to repair"
        )


def _ensure_dir(path: Path) -> bool:
    """Create ``path`` as a directory; return True if it was created."""
    _reject_symlink(path)
    if path.exists():
        if not path.is_dir():
            raise PathError(
                f"cannot create directory {path}: a file already exists there"
            )
        return False
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        raise PathError(
            f"cannot create directory {path}: {exc.strerror or exc}"
        ) from None
    return True


def _ensure_file(path: Path, content: str) -> bool:
    """Create ``path`` with ``content`` only if missing; return True if created."""
    _reject_symlink(path)
    if path.exists():
        return False
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise PathError(f"cannot write {path}: {exc.strerror or exc}") from None
    return True


def _remove_stale_state_tmp(aperio_dir: Path) -> bool:
    """Delete a leftover ``state.json.tmp`` from a crashed write, if any.

    ``state.json.tmp`` is a reserved Aperio temporary name (see schema.py).
    Symlinks and directories are left alone and reported as errors by the
    normal write path.
    """
    stale = aperio_dir / f"{_STATE}.tmp"
    if stale.exists() and not os.path.islink(stale) and stale.is_file():
        stale.unlink()
        return True
    return False


# --- init ---------------------------------------------------------------------


def init_project(raw_path: str) -> InitResult:
    """Initialize (or safely repair) an Aperio project.

    Never overwrites existing files. If ``state.json`` exists but is corrupt
    or written by a newer schema, aborts without changing anything.
    """
    target = resolve_target(raw_path, for_writing=True)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathError(
            f"cannot create project directory {target}: {exc.strerror or exc}"
        ) from None

    aperio_dir = aperio_dir_for(target)
    result = InitResult(target=target, already_initialized=False)

    # Validate existing state BEFORE writing anything else. A corrupt or
    # too-new state raises here and aborts the whole operation cleanly.
    if state_path(aperio_dir).exists():
        result.state = read_state(aperio_dir)
        result.already_initialized = True

    if _ensure_dir(aperio_dir):
        result.created.append(f"{APERIO_DIR_NAME}/")
    else:
        result.present.append(f"{APERIO_DIR_NAME}/")

    if _remove_stale_state_tmp(aperio_dir):
        result.removed.append(f"{APERIO_DIR_NAME}/{_STATE}.tmp")

    if _ensure_dir(aperio_dir / LOCAL_DIR_NAME):
        result.created.append(f"{APERIO_DIR_NAME}/{LOCAL_DIR_NAME}/")
    else:
        result.present.append(f"{APERIO_DIR_NAME}/{LOCAL_DIR_NAME}/")

    templates = {
        "brief.md": BRIEF_TEMPLATE,
        "decisions.md": DECISIONS_TEMPLATE,
        "handoff.md": HANDOFF_TEMPLATE,
        _GITIGNORE: APERIO_GITIGNORE_TEMPLATE,
        _LOCAL_README: LOCAL_README_TEMPLATE,
    }
    for rel, template in templates.items():
        if _ensure_file(aperio_dir / rel, template):
            result.created.append(f"{APERIO_DIR_NAME}/{rel}")
        else:
            result.present.append(f"{APERIO_DIR_NAME}/{rel}")

    if state_path(aperio_dir).exists():
        # Present and already validated above; never rewritten.
        result.present.append(f"{APERIO_DIR_NAME}/{_STATE}")
    else:
        state = build_initial_state(target, __version__)
        write_state(aperio_dir, state)
        result.state = state
        result.created.append(f"{APERIO_DIR_NAME}/{_STATE}")

    return result


# --- status -------------------------------------------------------------------


def status_project(raw_path: str) -> StatusReport:
    """Read project state and report it. Strictly read-only."""
    target = resolve_target(raw_path, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    state = read_state(aperio_dir)

    files: dict[str, bool] = {}
    for rel in CONTEXT_FILES:
        path = aperio_dir / rel
        files[rel] = path.exists() and path.is_file()
    local_dir = aperio_dir / LOCAL_DIR_NAME
    files[f"{LOCAL_DIR_NAME}/"] = local_dir.exists() and local_dir.is_dir()

    return StatusReport(target=target, state=state, files=files)


def formatted_schema_version(state: ProjectState) -> str:
    """Human-readable schema identifier for reports."""
    return f"v{state.schema_version} (supported up to v{SCHEMA_VERSION})"
