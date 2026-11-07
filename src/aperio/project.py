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
    state_tmp_path,
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

# Transient atomic-write artifacts (reserved by aperio; never committed).
state.json.tmp
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
    except FileExistsError:
        # Raced with another creator. Never destroy or follow whatever
        # appeared.
        if os.path.islink(path):
            raise PathError(
                f"refusing to follow symlink at {path} -> "
                f"{os.path.realpath(path)}; "
                "remove it and re-run 'aperio init' to repair"
            ) from None
        if path.is_dir():
            return False
        raise PathError(
            f"cannot create directory {path}: a non-directory appeared there"
        ) from None
    except OSError as exc:
        raise PathError(
            f"cannot create directory {path}: {exc.strerror or exc}"
        ) from None
    return True


def _ensure_file(path: Path, content: str) -> bool:
    """Create ``path`` with ``content`` only if missing; return True if created.

    Creation is exclusive (``O_CREAT | O_EXCL``): an unexpected file that
    appears between the existence check and the write can never be
    truncated or overwritten. Only a regular file counts as legitimately
    present; any other occupant (directory, FIFO, socket, ...) is an
    explicit error, never a silent "present" report.
    """
    _reject_symlink(path)
    if path.exists():
        if path.is_file():
            return False
        kind = "a directory" if path.is_dir() else "a special file"
        raise PathError(
            f"cannot write {path}: {kind} exists there; "
            "remove it and re-run 'aperio init' to repair"
        )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        # Raced with another creator; never destroy or follow whatever
        # appeared.
        if os.path.islink(path):
            raise PathError(
                f"refusing to follow symlink at {path} -> "
                f"{os.path.realpath(path)}; "
                "remove it and re-run 'aperio init' to repair"
            ) from None
        # Treat an appearing regular file as present (its content is not
        # ours to replace), anything else as an explicit conflict.
        if path.is_file():
            return False
        kind = "a directory" if path.is_dir() else "a special file"
        raise PathError(
            f"cannot write {path}: {kind} appeared there; "
            "remove it and re-run 'aperio init' to repair"
        ) from None
    except OSError as exc:
        raise PathError(f"cannot write {path}: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        raise PathError(f"cannot write {path}: {exc.strerror or exc}") from None
    return True


def _check_state_tmp_free(aperio_dir: Path) -> None:
    """Require the reserved temporary name to be unobstructed.

    ``state.json.tmp`` is a reserved Aperio name (see schema.py). Nothing
    is ever deleted from it automatically: ownership of a plain file cannot
    be verified, so ANY occupant — a leftover from a crashed write, a
    directory, or a symlink live or dangling — is a conflict that aborts
    init with an actionable error and is resolved by explicit user action.
    """
    tmp = state_tmp_path(aperio_dir)
    if os.path.islink(tmp):
        raise PathError(
            f"refusing to follow symlink at {tmp} -> {os.path.realpath(tmp)}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if tmp.exists():
        raise PathError(
            f"conflicting path at reserved temporary file {tmp}: it may be a "
            "leftover from a crashed write or unrelated content; removing it "
            "is your decision. Re-run 'aperio init' after resolving it."
        )


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
    if os.path.islink(target):
        # Race guard: a symlink planted between resolve_target and mkdir
        # must not become a write root (exist_ok=True would accept a
        # symlink pointing at a directory).
        raise PathError(
            f"refusing to follow symlink at target path ({target} -> "
            f"{os.path.realpath(target)})"
        )

    aperio_dir = aperio_dir_for(target)
    result = InitResult(target=target, already_initialized=False)

    # A symlinked state file — live or dangling — is never followed or
    # replaced during initialization: state integrity must not depend on
    # anything outside the project directory.
    state_file = state_path(aperio_dir)
    _reject_symlink(state_file)

    # Validate existing state BEFORE writing anything else. A corrupt or
    # too-new state raises here and aborts the whole operation cleanly.
    if state_file.exists():
        result.state = read_state(aperio_dir)
        result.already_initialized = True

    if _ensure_dir(aperio_dir):
        result.created.append(f"{APERIO_DIR_NAME}/")
    else:
        result.present.append(f"{APERIO_DIR_NAME}/")

    _check_state_tmp_free(aperio_dir)

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
