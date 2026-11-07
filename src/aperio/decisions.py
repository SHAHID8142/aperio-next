"""Append-only decision recording (Feature 02).

The Markdown decision log is the single source of truth — no parallel JSON
copy exists, so history can never diverge. ``decisions.md`` is only ever
**appended to**: existing bytes are never rewritten, and a torn final entry
(left by a crash or a hand edit) is reported in read metadata, never fatal,
and never edited by aperio.

Normative entry grammar (enforced by the writer, parsed leniently by the
reader — headings are recognized only at column 0):

    ## DEC-0001 — 2026-11-07T14:00:00Z — Single-line title

    - **Context:** first line of a possibly multi-line value
      <2-space-indented continuation lines>
    - **Decision:** ...
    - **Consequences:** ...

The 2-space indentation of continuation lines is a security property, not
formatting: a value can never place ``## `` or ``- **`` at column 0, so
agent-supplied text cannot forge headings or fields for aperio's parser or
for consumers sharing its line model. Validation additionally rejects
every other Unicode line-boundary character (CR, NEL, LS/PS, ASCII record
separators), because tools like ``str.splitlines()`` or CommonMark treat
those as line endings. Titles are single-line by validation. The timestamp
is ISO-8601 UTC with a ``Z`` suffix. Entry IDs are ``DEC-`` plus a
zero-padded counter (at least four digits); the next ID is max over *all*
DEC headings in the file — valid or malformed — plus one, and file order
is never assumed to equal ID order.

Concurrency: the single ``os.write`` on an ``O_APPEND`` descriptor means
concurrent appends never interleave or destroy each other's bytes, but two
simultaneous appends can pick the same next ID; serialize writers when
unique IDs must be guaranteed. The descriptor also carries ``O_NOFOLLOW``
where the platform provides it (POSIX); without it the protection degrades
to the islink pre-check. Crash-consistency is best-effort (no ``fsync``):
a power loss can leave a torn final entry — a tear after the DEC heading is
reported as malformed by readers; a tear earlier in the entry may be
indistinguishable from prose and is left untouched (never fatal, never
edited by aperio).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.schema import utc_now_iso

DECISIONS_FILE_NAME = "decisions.md"

# Heading recognized only at column 0: DEC-<digits> — <ISO-UTC Z> — <title>.
_HEADING_RE = re.compile(
    r"^## (DEC-\d+) — (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) — (.+)$"
)
# Any DEC-numbered heading, valid or not (drives next-ID generation so a
# torn entry's number is never reused).
_DEC_CANDIDATE_RE = re.compile(r"^## DEC-\d+")
_ANY_HEADING_RE = re.compile(r"^## ")
_FIELD_RE = re.compile(r"^- \*\*(Context|Decision|Consequences):\*\* ?(.*)$")

_FIELD_ORDER = ("Context", "Decision", "Consequences")

_LABEL_TO_ATTR = {
    "Context": "context",
    "Decision": "decision",
    "Consequences": "consequences",
}


@dataclass(frozen=True)
class DecisionEntry:
    """One decision as stored in ``decisions.md``."""

    id: str
    timestamp: str
    title: str
    context: str
    decision: str
    consequences: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "title": self.title,
            "context": self.context,
            "decision": self.decision,
            "consequences": self.consequences,
        }


@dataclass
class DecisionsRead:
    """Structured result of parsing ``decisions.md``.

    ``entries`` contains only fully valid entries. Damage is reported, not
    fatal: ``malformed_entries`` lists line numbers and reasons for
    DEC-pattern headings whose entry could not be parsed; ``skipped_headings``
    counts non-DEC ``##`` sections (user content); ``duplicate_ids`` lists
    IDs that appear more than once.
    """

    entries: list[DecisionEntry]
    skipped_headings: int = 0
    malformed_entries: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)


def decisions_path(aperio_dir: Path) -> Path:
    """Path of the decision log inside an ``.aperio/`` directory."""
    return aperio_dir / DECISIONS_FILE_NAME


def read_decisions(aperio_dir: Path) -> DecisionsRead:
    """Parse ``decisions.md``. Never fatal for entry-level damage.

    :raises NotInitializedError: when the file does not exist.
    :raises StateError: when the file is unreadable or not valid UTF-8.
    """
    path = decisions_path(aperio_dir)
    text = _read_log_text(path)
    return parse_decisions_text(text)


def parse_decisions_text(text: str) -> DecisionsRead:
    """Parse decision-log text per the normative grammar."""
    lines = text.split("\n")
    entries: list[DecisionEntry] = []
    malformed: list[str] = []
    duplicate_ids: list[str] = []
    seen_ids: dict[str, int] = {}
    skipped_headings = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        if _DEC_CANDIDATE_RE.match(line):
            match = _HEADING_RE.match(line)
            if match is None:
                malformed.append(
                    f"line {i + 1}: DEC heading is not a valid entry heading "
                    "(expected '## DEC-NNNN — <ISO-8601-UTC> — <title>')"
                )
                i += 1
                continue
            entry, consumed, problem = _parse_entry(lines, i, match)
            if problem is not None:
                malformed.append(f"line {i + 1}: {problem}")
            else:
                assert entry is not None
                if entry.id in seen_ids:
                    duplicate_ids.append(entry.id)
                seen_ids[entry.id] = i + 1
                entries.append(entry)
            i = consumed
            continue
        if _ANY_HEADING_RE.match(line):
            skipped_headings += 1
        i += 1

    return DecisionsRead(
        entries=entries,
        skipped_headings=skipped_headings,
        malformed_entries=malformed,
        duplicate_ids=duplicate_ids,
    )


def _parse_entry(
    lines: list[str], start: int, heading: re.Match[str]
) -> tuple[DecisionEntry | None, int, str | None]:
    """Parse one entry block starting at the heading line ``start``.

    Returns (entry, next_index, problem). ``problem`` is None for a valid
    entry; otherwise it describes the damage and the entry is skipped.
    """
    dec_id, timestamp, title = heading.group(1), heading.group(2), heading.group(3)
    if not title.strip():
        return None, start + 1, f"{dec_id}: heading title is empty"

    block: list[str] = []
    j = start + 1
    while j < len(lines) and not _ANY_HEADING_RE.match(lines[j]):
        block.append(lines[j])
        j += 1

    values: dict[str, list[str]] = {}
    current: str | None = None
    for block_line in block:
        field_match = _FIELD_RE.match(block_line)
        if field_match is not None:
            current = field_match.group(1)
            values[current] = [field_match.group(2)]
        elif current is not None and block_line.startswith("  "):
            values[current].append(block_line[2:])
        elif block_line.strip() == "":
            if current is not None:
                values[current].append("")
            # A blank line before the first field (between heading and
            # body) is normal; keep collecting.
            continue
        else:
            # Prose inside the entry block that is not part of any field:
            # keep the entry parseable and ignore the remainder.
            break

    missing = [label for label in _FIELD_ORDER if label not in values]
    if missing:
        return None, j, f"{dec_id}: missing field(s): {', '.join(missing)}"

    kwargs = {"id": dec_id, "timestamp": timestamp, "title": title}
    for label in _FIELD_ORDER:
        value = "\n".join(values[label]).rstrip()
        if not value:
            return None, j, f"{dec_id}: field '{label}' is empty"
        kwargs[_LABEL_TO_ATTR[label]] = value

    return DecisionEntry(**kwargs), j, None


def append_decision(
    aperio_dir: Path,
    *,
    title: str,
    context: str,
    decision: str,
    consequences: str,
    timestamp: str | None = None,
) -> DecisionEntry:
    """Append one decision to ``decisions.md``. History is never rewritten.

    The rendered entry is emitted as a single ``os.write`` on an
    ``O_APPEND | O_NOFOLLOW`` descriptor, so concurrent writers cannot
    interleave bytes and a symlink cannot be swapped in after validation.

    :raises ValidationError: for empty fields, a multi-line title, or a
        malformed explicit timestamp.
    :raises NotInitializedError: when ``decisions.md`` does not exist
        (run ``aperio init`` to repair).
    :raises PathError: when the target or a raced-in symlink is unsafe.
    :raises StateError: when the file is unreadable or not valid UTF-8.
    """
    title = _validate_single_line("title", title)
    context = _validate_field("context", context)
    decision = _validate_field("decision", decision)
    consequences = _validate_field("consequences", consequences)
    ts = timestamp if timestamp is not None else utc_now_iso()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts):
        raise ValidationError(
            "decisions.md: timestamp must be ISO-8601 UTC with a 'Z' suffix "
            f"(got {ts!r})"
        )

    path = decisions_path(aperio_dir)
    _reject_unsafe_target(path)
    text = _read_log_text(path)

    # Next ID: max over ALL DEC headings (valid or malformed), ignoring
    # file order, so a torn entry's number is never reused.
    highest = 0
    for line in text.split("\n"):
        candidate = _DEC_CANDIDATE_RE.match(line)
        if candidate:
            number = int(re.search(r"DEC-(\d+)", line).group(1))
            highest = max(highest, number)
    entry = DecisionEntry(
        id=f"DEC-{highest + 1:04d}",
        timestamp=ts,
        title=title,
        context=context,
        decision=decision,
        consequences=consequences,
    )

    rendered = render_entry(entry)
    if text and not text.endswith("\n"):
        rendered = "\n" + rendered  # start the entry on a fresh line

    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PathError(
            f"cannot open {path} for appending: {exc.strerror or exc}; "
            "if a symlink or permission problem persists, resolve it "
            "manually and re-run the command"
        ) from None
    try:
        payload = rendered.encode("utf-8")
        written = os.write(fd, payload)  # single syscall: never interleaved
        if written != len(payload):
            raise StateError(
                f"append to {path} was partial ({written}/{len(payload)} "
                "bytes); the final entry may be torn and will be reported "
                "as malformed by readers — no existing content was modified"
            )
    except OSError as exc:
        raise StateError(
            f"cannot append to {path}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    finally:
        os.close(fd)

    return entry


def render_entry(entry: DecisionEntry) -> str:
    """Render one entry per the normative grammar (ending with a blank line).

    Continuation lines are indented two spaces, which structurally prevents
    agent-supplied values from forging column-0 headings or field labels.
    """
    out = [f"## {entry.id} — {entry.timestamp} — {entry.title}", ""]
    for label, attr in (
        ("Context", "context"),
        ("Decision", "decision"),
        ("Consequences", "consequences"),
    ):
        value_lines = getattr(entry, attr).split("\n")
        out.append(f"- **{label}:** {value_lines[0]}")
        out.extend(f"  {extra}" for extra in value_lines[1:])
    out.append("")
    return "\n".join(out)


def _validate_single_line(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"decisions.md: field '{field_name}' must be a non-empty string"
        )
    stripped = value.strip()
    if "\n" in stripped:
        raise ValidationError(
            f"decisions.md: field '{field_name}' must be a single line "
            "(multi-line titles would break entry parsing)"
        )
    # \r / NEL / LS / PS would open a new line for consumers whose line
    # model differs from aperio's split("\n") — e.g. str.splitlines() or
    # CommonMark — letting a title forge a column-0 heading.
    check_no_foreign_line_separators("decisions.md", field_name, stripped)
    return stripped


def _validate_field(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"decisions.md: field '{field_name}' must be a non-empty string"
        )
    stripped = value.strip()
    check_no_foreign_line_separators("decisions.md", field_name, stripped)
    return stripped


def _reject_unsafe_target(path: Path) -> None:
    """Feature-01 per-target safety: refuse symlinks and non-regular files."""
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if not path.exists():
        raise NotInitializedError(
            f"not an Aperio project: no {path} (run 'aperio init <project-directory>' "
            "to repair missing context files)"
        )
    if not path.is_file():
        kind = "a directory" if path.is_dir() else "a special file"
        raise PathError(
            f"cannot write {path}: {kind} exists there; "
            "remove it and re-run 'aperio init' to repair"
        )


def _read_log_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}"
        ) from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
