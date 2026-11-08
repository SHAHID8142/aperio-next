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

# Every reserved transient/lock name aperio may create inside .aperio/.
# init appends any of these that are missing from an existing
# .aperio/.gitignore (additive, never destructive).
RESERVED_GITIGNORE_LINES = (
    "state.json.tmp",
    "handoff.md.tmp",
    "plan.json.tmp",
    "executions.json.tmp",
    "state.lock",
    "decisions.lock",
    "handoff.lock",
    "plan.lock",
    "executions.lock",
    "dispatch.lock",
    "import.lock",
)
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

Use 'aperio decision-add' to append entries. It writes one machine-parseable
entry per decision (headings are recognized only at column 0, timestamp is
ISO-8601 UTC):

    ## DEC-0001 - <UTC timestamp> - Title

    - **Context:** ...
    - **Decision:** ...
    - **Consequences:** ...

Hand-edit only with care: entries aperio cannot fully parse are reported as
malformed and never silently dropped, but they are also not machine-readable.
"""

HANDOFF_TEMPLATE = """\
# Handoff Notes
<!-- aperio:handoff:v1 -->

## Current objective
None noted.

## Completed work
None noted.

## Work in progress
None noted.

## Next recommended action
None noted.

## Blockers
None noted.

## Verification evidence
None noted.

## Verification limitations
None noted.

This file describes the *current* handoff: 'aperio handoff-write' replaces
its content with a validated, structured handoff. Permanent history belongs
in decisions.md.
"""

APERIO_GITIGNORE_TEMPLATE = """\
# Machine-local and sensitive data must never leave this project's disk
# (and never enter any VCS or shared context).
local/

# Transient atomic-write artifacts and coordination locks
# (reserved by aperio; never committed).
state.json.tmp
handoff.md.tmp
plan.json.tmp
executions.json.tmp
state.lock
decisions.lock
handoff.lock
plan.lock
executions.lock
dispatch.lock
import.lock
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


def require_supported_state(aperio_dir: Path) -> ProjectState:
    """Project-boundary check for all memory operations.

    Establishes that the operation is inside a genuine initialized Aperio
    project before anything else happens:

    - a symlinked ``.aperio/`` directory is refused (a leaf-file check
      alone would pass while every access silently traverses the link to
      somewhere outside the project);
    - a symlinked ``state.json`` is refused even when its target is a
      valid state file — portable memory reads must never follow links at
      shareable paths (this is stricter than Feature-01 ``status``, whose
      read-only reporting may follow symlinks);
    - ``state.json`` must exist (NotInitializedError otherwise — a
      directory that merely contains an ``.aperio/brief.md`` is *not* a
      project) and be a supported schema (StateError for corrupt or
      newer-than-supported states).
    """
    if os.path.islink(aperio_dir):
        raise PathError(
            f"refusing to follow symlink at {aperio_dir}; the project "
            "context directory itself is a symlink, so its contents could "
            "live outside the project boundary; remove it and re-run "
            "'aperio init' to repair"
        )
    state_file = state_path(aperio_dir)
    if os.path.islink(state_file):
        raise PathError(
            f"refusing to follow symlink at {state_file} while reading "
            "portable memory (it could point at local or private data); "
            "remove it and re-run 'aperio init' to repair"
        )
    return read_state(aperio_dir)


def _maintain_gitignore(aperio_dir: Path, result: "InitResult") -> None:
    """Additively append missing reserved ignore lines to .aperio/.gitignore.

    init never rewrites user content, so projects initialized by older
    aperio versions keep their original .gitignore; this maintenance only
    APPENDS reserved lines that are missing entirely, never removes or
    reorders anything, and reports what it added.
    """
    gitignore = aperio_dir / _GITIGNORE
    if not gitignore.is_file():
        return  # created (or refused) by the normal _ensure_file path
    try:
        current = gitignore.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return  # unreadable: leave it alone rather than guess
    existing_lines = {line.strip() for line in current.splitlines()}
    missing = [
        line
        for line in RESERVED_GITIGNORE_LINES
        if line not in existing_lines
    ]
    if not missing:
        return
    try:
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write("\n# Added by aperio init (reserved names)\n")
            for line in missing:
                fh.write(f"{line}\n")
    except OSError as exc:
        raise PathError(
            f"cannot append reserved ignore lines to {gitignore}: "
            f"{exc.strerror or exc}"
        ) from None
    for line in missing:
        result.created.append(f"{APERIO_DIR_NAME}/{_GITIGNORE}: {line}")


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

    _maintain_gitignore(aperio_dir, result)

    if state_path(aperio_dir).exists():
        # Present and already validated above; never rewritten.
        result.present.append(f"{APERIO_DIR_NAME}/{_STATE}")
    else:
        state = build_initial_state(target, __version__)
        write_state(aperio_dir, state)
        result.state = state
        result.created.append(f"{APERIO_DIR_NAME}/{_STATE}")

    return result


def ensure_local_scaffolding(aperio_dir: Path) -> list[str]:
    """Create missing machine-local scaffolding after a portable import.

    Adds ``local/``, ``local/README.md``, and ``.gitignore`` when absent —
    never overwriting imported or user content. These files are excluded
    from export (machine scaffolding, not portable context), so a fresh
    import restores them locally. Returns the relative paths created.
    """
    _reject_symlink(aperio_dir)
    created: list[str] = []
    if _ensure_dir(aperio_dir / LOCAL_DIR_NAME):
        created.append(f"{LOCAL_DIR_NAME}/")
    if _ensure_file(aperio_dir / _LOCAL_README, LOCAL_README_TEMPLATE):
        created.append(_LOCAL_README)
    if _ensure_file(aperio_dir / _GITIGNORE, APERIO_GITIGNORE_TEMPLATE):
        created.append(_GITIGNORE)
    return created


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
