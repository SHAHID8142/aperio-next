"""Portable project context reading (Feature 02).

``load_context`` is the single read API for coding agents: it returns the
validated project state plus the parsed shareable context — brief, decision
history, and handoff — with typed metadata. Only the named shareable files
are ever read; ``.aperio/local/``, environment variables, and anything
machine-specific are excluded by construction.

Export integrity: a shareable file that is a symlink (live) is refused
rather than read, so a planted or accidental link (e.g. ``brief.md ->
.aperio/local/credentials`` or ``brief.md -> ~/private-notes``) can never
smuggle machine-local or private content into the exported context. This
deliberately overrides the general read-only "follows symlinks" policy,
which applies to ``status`` — the export boundary is stricter because its
output is treated as shareable. A *dangling* link simply looks missing and
is reported in ``missing``.

Missing optional files are reported (``missing``), never fatal — repair is
``aperio init``'s job. Missing/corrupt ``state.json`` is fatal, matching
Feature 01. Entry-level damage in the decision log is reported, never fatal
(see :mod:`aperio.decisions`).

The ``to_dict`` schema is a stability promise for machine consumers
(``aperio memory --json``): keys will not be removed or renamed within
Feature 02; new keys may be added.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from aperio import __version__
from aperio.decisions import (
    DECISIONS_FILE_NAME,
    DecisionEntry,
    decisions_path,
    read_decisions,
)
from aperio.errors import PathError, StateError
from aperio.handoff import (
    HANDOFF_FILE_NAME,
    HandoffRead,
    handoff_path,
    read_handoff,
)
from aperio.paths import resolve_target
from aperio.runtime import execution_summary
from aperio.planstore import plan_summary
from aperio.project import APERIO_DIR_NAME, aperio_dir_for, require_supported_state
from aperio.schema import ProjectState

BRIEF_FILE_NAME = "brief.md"


@dataclass
class ProjectContext:
    """Portable, shareable project context (never includes local/ data).

    ``target`` is the resolved project location for display only; it is
    deliberately excluded from :meth:`to_dict` — an absolute filesystem
    path is machine-specific information and must not enter exported
    context. ``plan`` is the Feature-03 plan summary (None when the
    project has no plan — a normal state).
    """

    state: ProjectState
    target: Path
    brief: str | None
    decisions: list[DecisionEntry] | None
    handoff: HandoffRead | None
    plan: dict | None
    execution: dict | None
    missing: list[str] = field(default_factory=list)
    skipped_headings: int = 0
    malformed_entries: list[str] = field(default_factory=list)
    duplicate_decision_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Stable machine-readable schema for ``aperio memory --json``.

        Contains only shareable context: state, brief, decisions, handoff,
        the plan summary, and read metadata. Never ``.aperio/local/``
        content, never the local filesystem path.
        """
        return {
            "reader_version": __version__,
            "state": {
                "schema_version": self.state.schema_version,
                "name": self.state.name,
                "created_at": self.state.created_at,
                "updated_at": self.state.updated_at,
                "phase": self.state.phase,
                "aperio_version": self.state.aperio_version,
            },
            "brief": self.brief,
            "decisions": (
                [entry.to_dict() for entry in self.decisions]
                if self.decisions is not None
                else None
            ),
            "handoff": (
                {
                    "structured": self.handoff.structured,
                    "format_version": self.handoff.version,
                    "revision": self.handoff.revision,
                    "fields": self.handoff.fields,
                    "raw": self.handoff.raw,
                }
                if self.handoff is not None
                else None
            ),
            "plan": self.plan,
            "execution": self.execution,
            "missing": list(self.missing),
            "skipped_headings": self.skipped_headings,
            "malformed_entries": list(self.malformed_entries),
            "duplicate_decision_ids": list(self.duplicate_decision_ids),
        }


def load_context(project_path: str | Path) -> ProjectContext:
    """Load the shareable context of an initialized Aperio project.

    :raises NotInitializedError: when ``state.json`` does not exist.
    :raises StateError: when state is corrupt/unsupported, or an optional
        file exists but is unreadable or not valid UTF-8.
    """
    target = resolve_target(str(project_path), for_writing=False)
    aperio_dir = aperio_dir_for(target)

    context = ProjectContext(
        state=require_supported_state(aperio_dir),
        target=target,
        brief=None,
        decisions=None,
        handoff=None,
        plan=plan_summary(aperio_dir),
        execution=execution_summary(aperio_dir),
    )

    brief_path = aperio_dir / BRIEF_FILE_NAME
    _reject_shareable_symlink(brief_path)
    if brief_path.exists():
        context.brief = _read_text(brief_path)
    else:
        context.missing.append(BRIEF_FILE_NAME)

    decisions_file = decisions_path(aperio_dir)
    _reject_shareable_symlink(decisions_file)
    if decisions_file.exists():
        read = read_decisions(aperio_dir)
        context.decisions = read.entries
        context.skipped_headings = read.skipped_headings
        context.malformed_entries = read.malformed_entries
        context.duplicate_decision_ids = read.duplicate_ids
    else:
        context.missing.append(DECISIONS_FILE_NAME)

    handoff_file = handoff_path(aperio_dir)
    _reject_shareable_symlink(handoff_file)
    if handoff_file.exists():
        context.handoff = read_handoff(aperio_dir)
    else:
        context.missing.append(HANDOFF_FILE_NAME)

    return context


def _reject_shareable_symlink(path: Path) -> None:
    """Refuse *live* symlinks at shareable-file paths (export integrity).

    A symlink could point at ``.aperio/local/`` or any private file outside
    the project and leak its content into the exported context. Dangling
    links fall through: they look missing and are reported as such (and
    ``aperio init`` will refuse to write through them, with guidance).
    """
    if os.path.islink(path) and path.exists():
        raise PathError(
            f"refusing to follow symlink at {path} while reading shareable "
            "context (it could point at local or private data); remove the "
            "symlink and re-run 'aperio init' to restore the file"
        )


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None


__all__ = [
    "APERIO_DIR_NAME",
    "ProjectContext",
    "load_context",
]
