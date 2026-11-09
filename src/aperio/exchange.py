"""Universal portable exchange (M6, aperio-universal-exchange v1).

The second-generation exchange facility: one provider-neutral JSON document
that carries a *selected* set of work material between agents, sessions, and
machines — a goal, its context pack, a quality report, feedback documents,
selected universal memories, and the operator's outstanding uncertainties —
without silently losing provenance and without converting claims into
facts.

Design rules inherited from the rest of Aperio:

* **Reuse, don't replace.** Every section payload is an already-versioned
  contract: a goal-schema v1 document, an ``aperio-context-pack`` v1
  envelope, an ``aperio-quality-report`` v1 envelope, a feedback-schema v1
  document, an ``aperio-memory-export`` v1 document (so memories keep the
  M3 exchange rules: bounded history, digest validation, imports always
  enter as ``proposed``), or bounded operator notes. The legacy Feature 05
  project archive and the standalone M3 memory exchange are untouched;
  this format is additive.
* **Explicit selection.** Only what the operator names is exported. The
  manifest records what is inside and what selection produced it.
* **Content identity.** Every section carries a SHA-256 over its canonical
  payload; the document digest covers the ordered sections. Identical
  selections produce byte-identical files apart from ``created_at``.
* **No trust promotion on import.** Memories enter the destination store as
  ``proposed`` (the M3 rule, via the embedded memory-exchange document).
  Imported files are written as ``imported-*.json`` — visibly separate
  from operator-authored artifacts — and existing files are never
  overwritten. The import report tells the operator to re-declare goal
  provenance after review.
* **Fail closed.** Unknown section kinds, a newer ``format_version``,
  oversized payloads, or any payload that fails its own contract's
  validation refuse the exchange. Nothing executes; nothing is truncated.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.memportable import (
    MAX_IMPORT_BYTES as MAX_MEMORY_SECTION_BYTES,
    validate_exchange_document as _validate_memory_document,
)
from aperio.quality import QUALITY_REPORT_FORMAT

EXCHANGE_FORMAT = "aperio-universal-exchange"
EXCHANGE_FORMAT_VERSION = 1

IMPORT_REPORT_FORMAT = "aperio-universal-exchange-import"
IMPORT_REPORT_VERSION = 1

SECTION_KINDS = (
    "goal",
    "context-pack",
    "quality-report",
    "feedback",
    "memory",
    "operator-notes",
)

MAX_SECTIONS = 100
MAX_GOAL_SECTION_BYTES = 64 * 1024
MAX_PACK_SECTION_BYTES = 2 * 1024 * 1024
MAX_REPORT_SECTION_BYTES = 1024 * 1024
MAX_FEEDBACK_SECTION_BYTES = 64 * 1024
MAX_NOTES_SECTION_BYTES = 256 * 1024
MAX_NOTE_ITEMS = 50
MAX_NOTE_TEXT_BYTES = 8 * 1024
MAX_EXCHANGE_FILE_BYTES = 8 * 1024 * 1024

_SECTION_CAPS = {
    "goal": MAX_GOAL_SECTION_BYTES,
    "context-pack": MAX_PACK_SECTION_BYTES,
    "quality-report": MAX_REPORT_SECTION_BYTES,
    "feedback": MAX_FEEDBACK_SECTION_BYTES,
    "memory": MAX_MEMORY_SECTION_BYTES,
    "operator-notes": MAX_NOTES_SECTION_BYTES,
}

_CONTEXT_PACK_FORMAT = "aperio-context-pack"


def canonical_payload_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# section builders (export side)
# ---------------------------------------------------------------------------


def _read_json_file(path: str | Path, *, label: str, max_bytes: int) -> object:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            f"refusing to follow a symlink for the {label}"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read {label}: {exc.strerror or exc}"
        ) from None
    if len(raw) > max_bytes:
        raise ValidationError(
            f"{label} must be at most {max_bytes} UTF-8 bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{label} must be valid UTF-8") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{label} is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None


def goal_section(data: object) -> dict:
    """Validate a goal document and wrap it as an exchange section."""

    goal = validate_goal(data)
    payload = json.loads(canonical_payload_bytes(goal).decode("utf-8"))
    return {
        "kind": "goal",
        "payload": payload,
    }


def goal_section_from_file(path: str | Path) -> dict:
    data = _read_json_file(path, label="goal file", max_bytes=MAX_GOAL_SECTION_BYTES)
    return goal_section(data)


def context_pack_section(envelope: object) -> dict:
    """Validate a context-pack envelope and wrap it as a section."""

    if not isinstance(envelope, dict):
        raise ValidationError("context pack section must be a JSON object")
    if envelope.get("format") != _CONTEXT_PACK_FORMAT:
        raise ValidationError(
            f"context pack section must be an {_CONTEXT_PACK_FORMAT!r} "
            "document"
        )
    if envelope.get("format_version") != 1:
        raise ValidationError(
            "context pack section has unsupported format_version "
            f"{envelope.get('format_version')!r}"
        )
    content = envelope.get("content")
    if not isinstance(content, dict):
        raise ValidationError("context pack section has no content object")
    declared = envelope.get("content_sha256")
    if not isinstance(declared, str) or declared != _digest(content):
        raise ValidationError(
            "context pack section content_sha256 does not match its "
            "content; the pack was altered"
        )
    return {"kind": "context-pack", "payload": envelope}


def context_pack_section_from_file(path: str | Path) -> dict:
    data = _read_json_file(
        path, label="context pack file", max_bytes=MAX_PACK_SECTION_BYTES
    )
    return context_pack_section(data)


def quality_report_section(envelope: object) -> dict:
    if not isinstance(envelope, dict):
        raise ValidationError("quality report section must be a JSON object")
    if envelope.get("format") != QUALITY_REPORT_FORMAT:
        raise ValidationError(
            "quality report section must be an "
            f"{QUALITY_REPORT_FORMAT!r} document"
        )
    if envelope.get("format_version") != 1:
        raise ValidationError(
            "quality report section has unsupported format_version "
            f"{envelope.get('format_version')!r}"
        )
    content = envelope.get("content")
    if not isinstance(content, dict):
        raise ValidationError("quality report section has no content object")
    declared = envelope.get("content_sha256")
    if not isinstance(declared, str) or declared != _digest(content):
        raise ValidationError(
            "quality report section content_sha256 does not match its "
            "content; the report was altered"
        )
    return {"kind": "quality-report", "payload": envelope}


def quality_report_section_from_file(path: str | Path) -> dict:
    data = _read_json_file(
        path, label="quality report file", max_bytes=MAX_REPORT_SECTION_BYTES
    )
    return quality_report_section(data)


def feedback_section(data: object) -> dict:
    from aperio.feedback import validate_feedback

    feedback = validate_feedback(data)
    payload = json.loads(canonical_payload_bytes(feedback).decode("utf-8"))
    return {"kind": "feedback", "payload": payload}


def feedback_section_from_file(path: str | Path) -> dict:
    data = _read_json_file(
        path, label="feedback file", max_bytes=MAX_FEEDBACK_SECTION_BYTES
    )
    return feedback_section(data)


def memory_section(document: object) -> dict:
    """Wrap an ``aperio-memory-export`` v1 document as a section."""

    validated = _validate_memory_document(document)
    payload = json.loads(
        canonical_payload_bytes(validated).decode("utf-8")
    )
    return {"kind": "memory", "payload": payload}


def notes_section(items: list[dict]) -> dict:
    """Operator-authored outstanding uncertainties and notes (bounded)."""

    if not isinstance(items, list) or not items:
        raise ValidationError("operator notes must be a non-empty list")
    if len(items) > MAX_NOTE_ITEMS:
        raise ValidationError(
            f"operator notes must hold at most {MAX_NOTE_ITEMS} items"
        )
    normalized: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"note[{index}] must be an object")
        extra = sorted(set(item) - {"title", "text"})
        if extra:
            raise ValidationError(
                f"note[{index}]: unknown fields: " + ", ".join(extra)
            )
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError(f"note[{index}].text must be non-empty")
        if "\x00" in text:
            raise ValidationError(
                f"note[{index}].text must not contain NUL"
            )
        if len(text.encode("utf-8")) > MAX_NOTE_TEXT_BYTES:
            raise ValidationError(
                f"note[{index}].text must be at most {MAX_NOTE_TEXT_BYTES} "
                "UTF-8 bytes"
            )
        title = item.get("title")
        if title is not None and (
            not isinstance(title, str) or not title.strip()
            or "\x00" in title
            or len(title.encode("utf-8")) > 200
        ):
            raise ValidationError(
                f"note[{index}].title must be bounded non-empty text"
            )
        normalized.append(
            {"title": title, "text": text}
        )
    return {"kind": "operator-notes", "payload": {"items": normalized}}


# ---------------------------------------------------------------------------
# document assembly and validation
# ---------------------------------------------------------------------------


def build_exchange(sections: list[dict], *, note: str | None = None) -> dict:
    """Assemble, validate, and digest one universal exchange document."""

    if not sections:
        raise ValidationError(
            "an exchange needs at least one section; nothing was selected"
        )
    if len(sections) > MAX_SECTIONS:
        raise ValidationError(
            f"an exchange carries at most {MAX_SECTIONS} sections"
        )
    normalized: list[dict] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValidationError(f"section[{index}] must be an object")
        kind = section.get("kind")
        if kind not in SECTION_KINDS:
            raise ValidationError(
                f"section[{index}].kind must be one of: "
                + ", ".join(SECTION_KINDS)
            )
        payload = section.get("payload")
        payload_bytes = canonical_payload_bytes(payload)
        if len(payload_bytes) > _SECTION_CAPS[kind]:
            raise ValidationError(
                f"section[{index}] ({kind}) exceeds the {kind} payload cap "
                f"of {_SECTION_CAPS[kind]} bytes"
            )
        # Export enforces the same contract import will: a section that
        # could not be imported can never be built.
        _validate_section_payload(index, kind, payload)
        normalized.append(
            {
                "section_id": f"{kind}-{_digest(payload)[:12]}",
                "kind": kind,
                "payload_sha256": _digest(payload),
                "payload": payload,
            }
        )
    # Deterministic order: kind, then content identity.
    normalized.sort(key=lambda section: (section["kind"], section["section_id"]))
    from aperio import __version__

    manifest = {
        "includes": sorted({section["kind"] for section in normalized}),
        "counts": {
            kind: sum(1 for s in normalized if s["kind"] == kind)
            for kind in sorted({s["kind"] for s in normalized})
        },
        "note": note,
    }
    return {
        "format": EXCHANGE_FORMAT,
        "format_version": EXCHANGE_FORMAT_VERSION,
        "aperio_version": __version__,
        "created_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_sha256": _digest(normalized),
        "manifest": manifest,
        "sections": normalized,
    }


def load_exchange_file(path: str | Path) -> dict:
    data = _read_json_file(
        path, label="exchange file", max_bytes=MAX_EXCHANGE_FILE_BYTES
    )
    return validate_exchange(data)


def validate_exchange(document: object) -> dict:
    """Full validation: envelope, digest, every section's own contract."""

    if not isinstance(document, dict):
        raise ValidationError("exchange: the document must be a JSON object")
    if document.get("format") != EXCHANGE_FORMAT:
        raise ValidationError(
            f"exchange: expected format {EXCHANGE_FORMAT!r}, got "
            f"{document.get('format')!r}"
        )
    version = document.get("format_version")
    if version != EXCHANGE_FORMAT_VERSION:
        raise ValidationError(
            f"exchange: unsupported format_version {version!r}; this aperio "
            f"supports version {EXCHANGE_FORMAT_VERSION}"
        )
    sections = document.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValidationError("exchange: sections must be a non-empty list")
    if len(sections) > MAX_SECTIONS:
        raise ValidationError(
            f"exchange: at most {MAX_SECTIONS} sections are supported; "
            "use a newer aperio or split the file"
        )
    recomputed: list[dict] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValidationError(f"exchange: section[{index}] must be an object")
        extra = sorted(
            set(section) - {"section_id", "kind", "payload_sha256", "payload"}
        )
        if extra:
            raise ValidationError(
                f"exchange: section[{index}] has unknown fields: "
                + ", ".join(extra)
            )
        kind = section.get("kind")
        if kind not in SECTION_KINDS:
            raise ValidationError(
                f"exchange: section[{index}] has unsupported kind {kind!r}; "
                "this aperio supports: " + ", ".join(SECTION_KINDS)
            )
        payload = section.get("payload")
        payload_bytes = canonical_payload_bytes(payload)
        if len(payload_bytes) > _SECTION_CAPS[kind]:
            raise ValidationError(
                f"exchange: section[{index}] ({kind}) exceeds its payload cap"
            )
        declared = section.get("payload_sha256")
        if declared != _digest(payload):
            raise ValidationError(
                f"exchange: section[{index}] payload_sha256 does not match "
                "its payload; the section was altered"
            )
        _validate_section_payload(index, kind, payload)
        recomputed.append(
            {
                "section_id": f"{kind}-{_digest(payload)[:12]}",
                "kind": kind,
                "payload_sha256": _digest(payload),
                "payload": payload,
            }
        )
    declared_digest = document.get("content_sha256")
    if declared_digest != _digest(recomputed):
        raise ValidationError(
            "exchange: content_sha256 does not match the sections; the "
            "document was altered or reordered"
        )
    # section_id must match content identity
    for declared, computed in zip(sections, recomputed):
        if declared.get("section_id") != computed["section_id"]:
            raise ValidationError(
                f"exchange: section_id {declared.get('section_id')!r} does "
                "not match the section's content identity "
                f"({computed['section_id']!r})"
            )
    return document


def _validate_section_payload(index: int, kind: str, payload: object) -> None:
    """Run the section's owning contract over its payload."""

    if kind == "goal":
        validate_goal(payload)
    elif kind == "context-pack":
        context_pack_section(payload)
    elif kind == "quality-report":
        quality_report_section(payload)
    elif kind == "feedback":
        from aperio.feedback import validate_feedback

        validate_feedback(payload)
    elif kind == "memory":
        _validate_memory_document(payload)
    elif kind == "operator-notes":
        notes_section((payload or {}).get("items", [])
                      if isinstance(payload, dict) else [])


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _import_file_name(kind: str, digest: str) -> str:
    return f"imported-{kind}-{digest[:12]}.json"


def plan_import(
    exchange: dict,
    dest_dir: str | Path,
    *,
    memory_store_path: str | Path | None = None,
    max_sensitivity: str = "internal",
) -> dict:
    """Preview everything an import would do without writing anything.

    Conflicts (existing destination files), duplicates (memories already in
    the destination store), and skipped sections are reported, never
    resolved silently.
    """

    exchange = validate_exchange(exchange)
    destination = Path(dest_dir)
    if not destination.is_dir():
        raise ValidationError(
            f"destination directory does not exist: {destination}"
        )
    from aperio.memstore import MemoryStore

    planned: list[dict] = []
    warnings: list[dict] = []
    memory_report = None
    existing_store = None
    if memory_store_path is not None:
        store = MemoryStore(memory_store_path)
        existing_store = (
            store.snapshots()["memories"] if store.path.exists() else {}
        )
    for section in exchange["sections"]:
        kind = section["kind"]
        if kind == "memory":
            if memory_store_path is None:
                planned.append({
                    "section_id": section["section_id"],
                    "kind": kind,
                    "action": "skipped",
                    "reason": "no --memory-store was given",
                })
                continue
            from aperio.memportable import import_memories

            dry = import_memories(
                MemoryStore(memory_store_path),
                section["payload"],
                dry_run=True,
                max_sensitivity=max_sensitivity,
            )
            memory_report = {
                "imported": dry["imported"],
                "duplicates": dry["duplicates"],
                "excluded_sensitivity": dry["excluded_sensitivity"],
                "conflicts": dry["conflicts"],
                "declared_states": dry["declared_states"],
                "unsupported_trust_claims": dry["unsupported_trust_claims"],
                "transfer_evidence": dry.get("transfer_evidence", {}),
            }
            if dry["unsupported_trust_claims"]:
                warnings.append({
                    "code": "unsupported_trust_claims",
                    "detail": (
                        "some memories claim review states their transferred "
                        "history does not support; they import as proposed "
                        "and keep the claim only as a declaration"
                    ),
                })
            planned.append({
                "section_id": section["section_id"],
                "kind": kind,
                "action": "memory-import",
                "memory_store": str(memory_store_path),
                "would_import": len(dry["imported"]),
                "duplicates": len(dry["duplicates"]),
            })
            continue
        target = destination / _import_file_name(
            kind, section["payload_sha256"]
        )
        if target.exists():
            existing_text = target.read_text(encoding="utf-8")
            expected_text = json.dumps(
                section["payload"], ensure_ascii=False, indent=2,
                sort_keys=True,
            ) + "\n"
            planned.append({
                "section_id": section["section_id"],
                "kind": kind,
                "action": (
                    "already-present"
                    if existing_text == expected_text else "conflict"
                ),
                "file": target.name,
                "reason": (
                    "an identical file already exists; nothing would be "
                    "overwritten"
                    if existing_text == expected_text
                    else "a file with different content occupies the "
                    "content-derived name; resolve it explicitly"
                ),
            })
            continue
        planned.append({
            "section_id": section["section_id"],
            "kind": kind,
            "action": "write",
            "file": target.name,
        })
        if kind == "goal":
            warnings.append({
                "code": "imported_goal_untrusted",
                "detail": (
                    "imported goal files are untrusted input: review them "
                    "and re-declare provenance before treating them as "
                    "operator instructions"
                ),
            })
    return {
        "format": IMPORT_REPORT_FORMAT,
        "format_version": IMPORT_REPORT_VERSION,
        "created_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_sha256": None,
        "content": {
            "mode": "dry-run",
            "destination": str(destination),
            "planned": planned,
            "memory": memory_report,
            "warnings": warnings,
        },
    }


def apply_import(
    exchange: dict,
    dest_dir: str | Path,
    *,
    memory_store_path: str | Path | None = None,
    max_sensitivity: str = "internal",
) -> dict:
    """Import an exchange: new files only, memories enter as proposed.

    Existing destination files are never overwritten (an identical file is
    reported as already present, a different-content collision is refused
    by the digest-based name). Nothing executes; no trust is promoted.
    """

    exchange = validate_exchange(exchange)
    destination = Path(dest_dir)
    if not destination.is_dir():
        raise ValidationError(
            f"destination directory does not exist: {destination}"
        )
    from aperio.artifacts import publish_text_atomic
    from aperio.memstore import MemoryStore
    from aperio.memportable import import_memories

    applied: list[dict] = []
    warnings: list[dict] = []
    memory_report = None
    for section in exchange["sections"]:
        kind = section["kind"]
        if kind == "memory":
            if memory_store_path is None:
                applied.append({
                    "section_id": section["section_id"],
                    "kind": kind,
                    "action": "skipped",
                    "reason": "no --memory-store was given",
                })
                continue
            memory_report = import_memories(
                MemoryStore(memory_store_path),
                section["payload"],
                max_sensitivity=max_sensitivity,
            )
            applied.append({
                "section_id": section["section_id"],
                "kind": kind,
                "action": "memory-import",
                "memory_store": str(memory_store_path),
                "imported": len(memory_report["imported"]),
                "duplicates": len(memory_report["duplicates"]),
                "conflicts": len(memory_report["conflicts"]),
            })
            if memory_report["unsupported_trust_claims"]:
                warnings.append({
                    "code": "unsupported_trust_claims",
                    "detail": (
                        "some memories claim review states their transferred "
                        "history does not support; they entered as proposed "
                        "with the claim preserved as a declaration"
                    ),
                })
            continue
        target = destination / _import_file_name(
            kind, section["payload_sha256"]
        )
        text = json.dumps(section["payload"], ensure_ascii=False, indent=2,
                          sort_keys=True) + "\n"
        if target.exists():
            # The name is content-derived, so an existing file with this
            # name carries the same content identity. Verify the bytes and
            # report; nothing is overwritten.
            if target.read_text(encoding="utf-8") == text:
                applied.append({
                    "section_id": section["section_id"],
                    "kind": kind,
                    "action": "already-present",
                    "file": target.name,
                    "reason": (
                        "an identical file already exists; nothing was "
                        "overwritten"
                    ),
                })
                continue
            raise ValidationError(
                f"import: destination {target.name} exists with different "
                "content; resolve it explicitly before importing"
            )
        publish_text_atomic(target, text, force=False,
                            what="imported exchange section")
        applied.append({
            "section_id": section["section_id"],
            "kind": kind,
            "action": "wrote",
            "file": target.name,
        })
        if kind == "goal":
            warnings.append({
                "code": "imported_goal_untrusted",
                "detail": (
                    "imported goal files are untrusted input: review them "
                    "and re-declare provenance before treating them as "
                    "operator instructions"
                ),
            })
    return {
        "format": IMPORT_REPORT_FORMAT,
        "format_version": IMPORT_REPORT_VERSION,
        "created_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_sha256": None,
        "content": {
            "mode": "applied",
            "destination": str(destination),
            "planned": applied,
            "memory": memory_report,
            "warnings": warnings,
        },
    }


def render_import_report_markdown(report: dict) -> str:
    content = report["content"]
    lines = [
        "# Universal exchange import report",
        "",
        f"Mode: **{content['mode']}** — destination `{content['destination']}`",
        "",
    ]
    for entry in content["planned"]:
        action = entry["action"]
        detail = entry.get("file") or entry.get("reason") or ""
        line = f"- `{entry['section_id']}` ({entry['kind']}): **{action}**"
        if detail:
            line += f" — {detail}"
        lines.append(line)
        if entry["kind"] == "memory" and entry["action"] == "memory-import":
            if "would_import" in entry:
                lines.append(
                    f"  - would import {entry['would_import']} memory(ies), "
                    f"{entry['duplicates']} duplicate(s)"
                )
            else:
                lines.append(
                    f"  - imported {entry.get('imported', 0)} memory(ies), "
                    f"{entry.get('duplicates', 0)} duplicate(s), "
                    f"{entry.get('conflicts', 0)} conflict(s)"
                )
    lines.append("")
    memory = content.get("memory")
    if memory:
        if memory.get("unsupported_trust_claims"):
            lines.append(
                "Unsupported trust claims (imported as proposed, claim "
                "kept as a declaration):"
            )
            lines.append("")
            for memory_id in sorted(memory["unsupported_trust_claims"]):
                lines.append(f"- `{memory_id}`")
            lines.append("")
    for warning in content["warnings"]:
        lines.append(f"- *{warning['code']}*: {warning['detail']}")
    lines.append("")
    return "\n".join(lines)
