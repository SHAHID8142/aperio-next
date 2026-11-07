"""Strict input validation for agent-supplied content.

Both ``decision-add --file`` and ``handoff-write --file`` consume a JSON
object of string fields. The contract is deliberately strict — agents are
the typical producers, and a typo'd key must never silently degrade into a
missing field:

- the file must be valid UTF-8 and valid JSON (otherwise a clean
  ValidationError, never a traceback)
- the object must contain exactly the required fields — missing fields and
  unknown/extra fields are both rejected, each named in the error
- every value must be a non-empty string
- validation completes before any filesystem effect (the callers only
  start writing after this returns)

This module also hosts the line-separator rule shared by the decision and
handoff formats: aperio's line model is ``split("\\n")`` only. Characters
that other tools (Python ``str.splitlines()``, CommonMark) treat as line
breaks — CR, NEL, LS, PS, and the ASCII record/field separators — are
rejected in validated values, so injected text can never open a new line
in a consumer that uses different line semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

from aperio.errors import ValidationError

# Line-boundary characters besides "\n" that splitlines()/Markdown treat
# as line endings. Their presence would let a value forge column-0
# headings for consumers with different line semantics.
FORBIDDEN_LINE_SEPARATORS: tuple[str, ...] = (
    "\r",      # CR (CRLF line endings)
    "\x85",    # NEL
    "\u2028",  # LINE SEPARATOR
    "\u2029",  # PARAGRAPH SEPARATOR
    "\x1c",    # FILE (information separator four)
    "\x1d",    # GROUP (information separator three)
    "\x1e",    # RECORD (information separator two)
)


def check_no_foreign_line_separators(artifact: str, field_name: str, value: str) -> None:
    """Reject values containing line separators other than "\\n".

    :raises ValidationError: naming the artifact, field, and offending
        character, with the fix.
    """
    for separator in FORBIDDEN_LINE_SEPARATORS:
        if separator in value:
            raise ValidationError(
                f"{artifact}: field '{field_name}' contains a line "
                f"separator other than a plain newline "
                f"(U+{ord(separator):04X}); replace it with '\\n' so every "
                "reader agrees on line boundaries"
            )


def read_json_fields(
    path: str | Path,
    required_fields: tuple[str, ...],
    optional_int_fields: tuple[str, ...] = (),
    optional_str_fields: tuple[str, ...] = (),
) -> dict:
    """Read and validate a strict JSON fields file.

    ``optional_int_fields`` are known keys that may be present; when given
    they must be non-negative integers (booleans rejected).
    ``optional_str_fields`` likewise require non-empty strings when
    present. Unknown keys — anything outside the required and optional
    sets — are always rejected.

    :raises ValidationError: for any violation; the message names the file
        and the offending field.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read input file {path}: {exc.strerror or exc}"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            f"input file {path} is not valid UTF-8; re-save it as UTF-8"
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"input file {path} is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None

    if not isinstance(data, dict):
        raise ValidationError(f"input file {path} must contain a JSON object")

    allowed = set(required_fields) | set(optional_int_fields) | set(optional_str_fields)
    missing = [name for name in required_fields if name not in data]
    if missing:
        raise ValidationError(
            f"input file {path} is missing required field(s): {', '.join(missing)}"
        )
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(
            f"input file {path} has unknown field(s): {', '.join(unknown)} "
            f"(expected: {', '.join(required_fields)}"
            + (
                f"; optional: {', '.join(optional_int_fields + optional_str_fields)})"
                if (optional_int_fields or optional_str_fields)
                else ")"
            )
        )

    fields: dict = {}
    for name in required_fields:
        value = data[name]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"input file {path}: field '{name}' must be a non-empty string"
            )
        fields[name] = value
    for name in optional_int_fields:
        if name in data:
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(
                    f"input file {path}: field '{name}' must be a "
                    "non-negative integer"
                )
            fields[name] = value
    for name in optional_str_fields:
        if name in data:
            value = data[name]
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"input file {path}: field '{name}' must be a non-empty string"
                )
            fields[name] = value
    return fields
