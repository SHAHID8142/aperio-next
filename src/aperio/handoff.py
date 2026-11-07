"""Reliable, structured agent handoff (Feature 02).

A handoff is the *current* snapshot for the next agent: it is replaced, not
appended (permanent history belongs in decisions.md). All seven fields are
required and non-empty at write time — an agent must state something for
each, even if only "None noted." — so an accidental empty or destructive
handoff cannot be written. Validation completes before any filesystem
effect.

Structured format v1 (written by this module; the marker sits on line 2,
right after the title):

    # Handoff Notes
    <!-- aperio:handoff:v1 -->

    ## Current objective
    ...

    ## Completed work
    ...
    (sections: Completed work, Work in progress, Next recommended action,
     Blockers, Verification evidence, Verification limitations)

Reader behavior by marker:

- ``v1``: parse sections; a section that is missing or appears more than
  once is damage in a file that claims v1 — reported as StateError, never
  silently guessed around. Unknown extra ``##`` sections are tolerated and
  reported.
- ``v2+`` (newer writer, older reader): never fatal — returned with
  ``structured=None`` and the raw text so nothing is lost.
- no marker: legacy free-form handoff (Feature 01 template) — returned
  with ``structured=False`` and the raw text.

Injection rule: a field value containing a line starting with ``## `` at
column 0 is rejected at validation — it would forge a section boundary.
Line 1 and the marker line are writer-controlled, so validated content
cannot spoof them.

Concurrency: the write replaces the whole file (tmp exclusive + rename).
Without ``expected_raw`` this is unconditional last-writer-wins with NO
Protection against lost updates: every structured handoff carries a
revision counter in its marker (``<!-- aperio:handoff:v1 rev=N -->``).
Writes run under the advisory ``handoff.lock`` and are revision-checked:
when the current handoff is at revision >= 1, the caller must declare the
revision it is replacing (``revises`` in the CLI input JSON,
``expected_revision`` in the library); a stale declaration aborts with a
clear conflict and nothing is modified. Overwriting a revision >= 1
*without* declaring it, or a handoff in a **newer unsupported format**
(v2+ marker), is refused unless ``force=True`` — the explicit, documented
user decision that deliberately discards the current handoff's content
(including, when forced over a newer format, content aperio cannot read).
Legacy free-form files and the fresh init template (revision 0) remain
replaceable without a declaration, preserving Feature 01 compatibility.
Renaming over a symlink swapped in after the islink check replaces the
link as a directory entry — its target is never followed or touched.
Crash-consistency is best-effort (no fsync). A leftover
``handoff.md.tmp`` is a conflict resolved by explicit user action; it is
git-ignored in projects created by this version.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.locks import (
    LOCK_TIMEOUT_SECONDS as LOCK_TIMEOUT_DEFAULT,
    acquire_lock,
    release_lock,
)
from aperio.project import require_supported_state

HANDOFF_FILE_NAME = "handoff.md"
HANDOFF_TMP_NAME = "handoff.md.tmp"
HANDOFF_LOCK_NAME = "handoff.lock"
HANDOFF_MARKER_V1 = "<!-- aperio:handoff:v1 -->"
_MARKER_RE = re.compile(
    r"^<!-- aperio:handoff:v(\d+)(?: rev=(\d+))? -->\r?$"
)
_SECTION_RE = re.compile(r"^## (.+)$")
# A heading anywhere at column 0 inside a validated value would forge a
# section boundary when the file is parsed.
_INJECTED_SECTION_RE = re.compile(r"^## ", re.MULTILINE)

_FIELD_TITLES = {
    "current_objective": "Current objective",
    "completed_work": "Completed work",
    "work_in_progress": "Work in progress",
    "next_recommended_action": "Next recommended action",
    "blockers": "Blockers",
    "verification_evidence": "Verification evidence",
    "verification_limitations": "Verification limitations",
}
_TITLE_TO_FIELD = {title: name for name, title in _FIELD_TITLES.items()}

HANDOFF_FIELDS: tuple[str, ...] = tuple(_FIELD_TITLES)


@dataclass(frozen=True)
class HandoffContent:
    """The seven required handoff fields (all non-empty after strip)."""

    current_objective: str
    completed_work: str
    work_in_progress: str
    next_recommended_action: str
    blockers: str
    verification_evidence: str
    verification_limitations: str

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in HANDOFF_FIELDS}


@dataclass
class HandoffRead:
    """Result of reading ``handoff.md``.

    ``structured`` is True for a parsed v1 handoff, False for a legacy
    free-form file, and None when the file was written by a newer handoff
    format this reader does not understand (never fatal). ``revision`` is
    the structured handoff's revision counter (0 for legacy files, the
    init template, and unknown newer formats).
    """

    structured: bool | None
    version: int | None
    fields: dict[str, str] | None
    raw: str
    revision: int = 0
    extra_sections: list[str] = field(default_factory=list)


def handoff_path(aperio_dir: Path) -> Path:
    """Path of the handoff file inside an ``.aperio/`` directory."""
    return aperio_dir / HANDOFF_FILE_NAME


def validate_handoff_content(content: HandoffContent) -> None:
    """Validate all seven fields; raises before any filesystem effect."""
    if not isinstance(content, HandoffContent):
        raise ValidationError(
            "handoff.md: handoff must be a HandoffContent with all seven fields"
        )
    for name in HANDOFF_FIELDS:
        value = getattr(content, name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"handoff.md: field '{name}' must be a non-empty string "
                "(write 'None noted.' when nothing applies)"
            )
        # A CR/NEL/LS/PS would open a new line for consumers whose line
        # model differs from aperio's split("\\n") (str.splitlines,
        # CommonMark), bypassing the column-0 section rule below.
        check_no_foreign_line_separators("handoff.md", name, value)
        if _INJECTED_SECTION_RE.search(value):
            raise ValidationError(
                f"handoff.md: field '{name}' contains a line starting with "
                "'## ' at column 0, which would forge a section boundary; "
                "reformat it (indent or reword) and retry"
            )


def render_handoff(content: HandoffContent, revision: int = 0) -> str:
    """Render the structured v1 handoff document at the given revision."""
    validate_handoff_content(content)
    marker = (
        HANDOFF_MARKER_V1
        if revision <= 0
        else f"<!-- aperio:handoff:v1 rev={revision} -->"
    )
    out = ["# Handoff Notes", marker, ""]
    for name in HANDOFF_FIELDS:
        out.append(f"## {_FIELD_TITLES[name]}")
        out.append(getattr(content, name).strip())
        out.append("")
    return "\n".join(out)


def read_handoff(aperio_dir: Path) -> HandoffRead:
    """Read ``handoff.md`` in any known (or unknown) format, never fatal
    for newer formats.

    Boundary check first: the project must be initialized with a supported
    schema; ``.aperio/`` and ``handoff.md`` must not be symlinks (portable
    memory reads never follow links at shareable paths).

    :raises NotInitializedError: when the project or the file does not exist.
    :raises StateError: when state is unsupported, the file is unreadable,
        not valid UTF-8, or a v1 file with damaged sections
        (missing/duplicated).
    :raises PathError: when ``.aperio/`` or ``handoff.md`` is a symlink.
    """
    require_supported_state(aperio_dir)
    path = handoff_path(aperio_dir)
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading portable "
            "memory (it could point at local or private data); remove it "
            "and re-run 'aperio init' to repair"
        )
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        raise NotInitializedError(
            f"not an Aperio project: no {path} (run 'aperio init <project-directory>' "
            "to repair missing context files)"
        ) from None
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
    return parse_handoff_text(text)


def parse_handoff_text(text: str) -> HandoffRead:
    """Parse handoff text: structured v1, legacy, or newer-format marker."""
    lines = text.split("\n")
    version: int | None = None
    for line in lines[:2]:  # marker is pinned to the first two lines
        marker = _MARKER_RE.match(line)
        if marker:
            version = int(marker.group(1))
            break

    if version is None:
        return HandoffRead(structured=False, version=None, fields=None, raw=text)
    if version != 1:
        # Newer writer, older reader: surface the raw text, lose nothing.
        return HandoffRead(structured=None, version=version, fields=None, raw=text)

    revision = 0
    for line in lines[:2]:
        marker = _MARKER_RE.match(line)
        if marker and int(marker.group(1)) == 1:
            rev_text = marker.group(2)
            revision = int(rev_text) if rev_text is not None else 0
            break

    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in lines:
        if line.endswith("\r"):
            # CRLF-authored files: one trailing CR per line is a line-
            # ending artifact, not content (validated values never
            # contain CR, so stripping it here is lossless for files
            # aperio wrote and makes CRLF-edited files parse).
            line = line[:-1]
        heading = _SECTION_RE.match(line)
        if heading is not None:
            current = heading.group(1)
            if current in sections:
                raise StateError(
                    f"handoff.md claims format v1 but section '{current}' "
                    "appears more than once; the file may be damaged. "
                    "Inspect it manually; aperio will not guess."
                )
            sections[current] = []
            order.append(current)
        elif current is not None:
            sections[current].append(line)

    missing = [
        title for title in _FIELD_TITLES.values() if title not in sections
    ]
    if missing:
        raise StateError(
            f"handoff.md claims format v1 but is missing section(s): "
            f"{', '.join(missing)}; the file may be damaged. Inspect it "
            "manually; aperio will not guess."
        )
    fields = {
        name: "\n".join(sections[title]).strip()
        for name, title in _FIELD_TITLES.items()
    }
    extra = [title for title in order if title not in _TITLE_TO_FIELD]
    return HandoffRead(
        structured=True,
        version=1,
        fields=fields,
        raw=text,
        revision=revision,
        extra_sections=extra,
    )


def write_handoff(
    aperio_dir: Path,
    content: HandoffContent,
    expected_revision: int | None = None,
    force: bool = False,
    lock_timeout: float = LOCK_TIMEOUT_DEFAULT,
) -> int:
    """Replace ``handoff.md`` with a validated structured handoff.

    Protection against lost updates (the safe default):

    - The read of the current handoff, the revision check, and the write
      all happen under the advisory ``handoff.lock`` (bounded wait; a
      lock is never stolen — an occupied lock past the timeout produces a
      clear conflict, and an abandoned lock is resolved by explicit user
      action; see :mod:`aperio.locks`), so concurrent aperio writers
      serialize.
    - When the current handoff is structured at revision >= 1, the caller
      must declare the revision it is replacing via ``expected_revision``;
      a stale declaration aborts with a clear conflict and nothing is
      modified. A stale agent therefore cannot silently overwrite a newer
      handoff.
    - Overwriting revision >= 1 *without* declaring it, or a handoff in a
      newer unsupported format (v2+ marker), is refused unless
      ``force=True`` — the explicit, documented decision to discard the
      current content. Legacy free-form files and the fresh init template
      (revision 0) stay replaceable without a declaration (Feature 01
      compatibility).
    - A file modified by a non-cooperating tool between the in-lock read
      and the rename is detected by a final byte comparison and aborts.

    Validation completes before any filesystem effect. The write goes to
    an exclusive temp file and renames atomically. On any failure only the
    temp file created by this call is removed; anything found on disk —
    including a leftover ``handoff.md.tmp`` from an earlier interrupted
    write, which blocks new writes until resolved — is left for explicit
    user action.

    :returns: the revision assigned to the newly written handoff.
    :raises ValidationError: for invalid field content or a malformed
        ``expected_revision``.
    :raises NotInitializedError: when the project or ``handoff.md`` does
        not exist.
    :raises PathError: when a target, the lock, or a temp path is unsafe.
    :raises StateError: for revision conflicts, newer-format refusals,
        concurrent-modification detection, lock timeouts, and I/O failures.
    """
    if expected_revision is not None and (
        isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValidationError(
            "handoff.md: expected_revision must be a non-negative integer"
        )

    validate_handoff_content(content)
    require_supported_state(aperio_dir)

    path = handoff_path(aperio_dir)
    tmp = aperio_dir / HANDOFF_TMP_NAME
    lock_path = aperio_dir / HANDOFF_LOCK_NAME

    token = acquire_lock(
        lock_path,
        timeout=lock_timeout,
        artifact="handoff",
    )
    try:
        if os.path.islink(path):
            raise PathError(
                f"refusing to follow symlink at {path}; "
                "remove it and re-run 'aperio init' to repair"
            )
        if os.path.islink(tmp):
            raise PathError(
                f"refusing to follow symlink at {tmp}; "
                "remove it and re-run 'aperio handoff-write' to repair"
            )
        if not path.exists():
            raise NotInitializedError(
                f"not an Aperio project: no {path} (run 'aperio init "
                "<project-directory>' to repair missing context files)"
            )
        if not path.is_file():
            kind = "a directory" if path.is_dir() else "a special file"
            raise PathError(
                f"cannot write {path}: {kind} exists there; "
                "remove it and re-run 'aperio init' to repair"
            )

        # Single-snapshot read under the lock: the revision used for the
        # conflict checks AND the original bytes used for conflict
        # detection come from this one read — never from two reads that
        # could straddle an external modification.
        try:
            expected_raw = path.read_bytes()
        except FileNotFoundError:
            raise NotInitializedError(
                f"not an Aperio project: no {path} (run 'aperio init "
                "<project-directory>' to repair missing context files)"
            ) from None
        except OSError as exc:
            raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
        try:
            snapshot_text = expected_raw.decode("utf-8")
        except UnicodeDecodeError:
            raise StateError(
                f"{path} is corrupt: not valid UTF-8; aperio will not modify "
                "it. Inspect or restore the file manually."
            ) from None
        current = parse_handoff_text(snapshot_text)
        current_rev = current.revision

        if current.structured is None and not force:
            raise StateError(
                f"handoff at {path} declares a newer format "
                f"(v{current.version}) this aperio cannot read; replacing "
                "it would silently destroy content aperio cannot inspect. "
                "Re-run with force=True (--force) only if you accept that."
            )
        if not force:
            if expected_revision is None:
                if current_rev >= 1:
                    raise StateError(
                        f"handoff at {path} is at revision {current_rev}; to "
                        "replace it, declare that revision ('revises': "
                        f"{current_rev} in the input JSON, or "
                        f"expected_revision={current_rev}), or use "
                        "force=True to replace it deliberately. Nothing "
                        "was modified."
                    )
            elif expected_revision != current_rev:
                # Includes a declaration against a legacy/revision-0 file:
                # a declared revision that does not match reality is
                # treated as a stale writer, never silently ignored.
                raise StateError(
                    f"handoff at {path} moved on: current revision is "
                    f"{current_rev}, but the caller declared "
                    f"{expected_revision} (a stale handoff would silently "
                    "overwrite newer work). Re-read the handoff, re-apply "
                    "your changes, and retry. Nothing was modified."
                )

        new_revision = current_rev + 1
        payload = render_handoff(content, revision=new_revision)
        created_tmp = False
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            created_tmp = True
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError as exc:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"cannot prepare handoff write at {tmp}: {exc.strerror or exc}; "
                "the temp name is occupied by a concurrent aperio write (retry "
                "shortly) or a leftover from a crashed write (removing it is "
                "your decision); no existing content was modified"
            ) from None

        # External-tool defense: a non-cooperating writer may have landed
        # between our in-lock read and this rename (the lock does not bind
        # them). Final byte check before publishing.
        try:
            current_bytes = path.read_bytes()
        except OSError as exc:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"cannot read {path} before publishing: {exc.strerror or exc}; "
                "no existing content was modified"
            ) from None
        if current_bytes != expected_raw:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"handoff at {path} changed during the write (a tool outside "
                "aperio's coordination may have written it); nothing was "
                "modified. Re-read the handoff and retry."
            )

        try:
            os.replace(tmp, path)
        except OSError as exc:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"cannot publish handoff at {path}: {exc.strerror or exc}; "
                "no existing content was modified"
            ) from None
        return new_revision
    finally:
        release_lock(lock_path, token)


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    """Remove a temp file this same call created; never touch anything else."""
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover is reported as a conflict by the next write
