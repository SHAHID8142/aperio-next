"""Portable memory exchange (memory-exchange v1).

Manual, file-based, reviewable transfer of selected memories between agents,
machines, and sessions — **Agent A → Aperio memory → Agent B**.

Design rules:

- **Separate from the legacy project archive.** Feature 05's export/import
  v1 is unchanged. This format is its own versioned document
  (``aperio-memory-export`` v1) so neither format can be misread as the
  other.
- **Identity travels, trust does not.** Exported records keep their
  content-derived ``memory_id`` and ``content_sha256`` across stores, so the
  same knowledge is recognisably the same and duplicates are detectable.
  Imported records always enter a store as ``proposed`` regardless of the
  state declared in the file; the declared state is preserved as
  ``declared_state`` for the reviewer and never applied.
- **Hashes are integrity checks.** A valid hash proves the bytes were not
  corrupted or altered since export. It says nothing about who wrote them or
  whether the claim is true.
- **Nothing is overwritten.** Duplicate identities are reported and skipped;
  conflicting subjects are reported and kept side by side. Sensitivity
  ``private`` records are never exported unless explicitly requested, and an
  import cannot raise a record's sensitivity filter.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aperio import __version__
from aperio.errors import StateError, ValidationError
from aperio.memstore import (
    MEMORY_SCHEMA_VERSION,
    STATE_TRUST,
    MEMORY_STATES,
    MemoryStore,
    SENSITIVITY_LEVELS,
    SENSITIVITY_RANK,
    is_active,
    memory_content_sha256,
    memory_identity,
    validate_memory_record,
)
from aperio.schema import utc_now_iso

MEMORY_EXCHANGE_FORMAT = "aperio-memory-export"
MEMORY_EXCHANGE_FORMAT_VERSION = 1
MAX_EXPORT_RECORDS = 500
MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BYTES = 4 * 1024 * 1024
MAX_TRANSFER_HISTORY = 32

_EXPORT_KEYS = (
    "format",
    "format_version",
    "memory_schema_version",
    "exported_at",
    "aperio_version",
    "source",
    "entries",
    "counts",
    "content_sha256",
)
_ENTRY_KEYS = ("memory", "declared_state")
_RECORD_FIELDS = (
    "memory_schema_version",
    "memory_id",
    "type",
    "title",
    "content",
    "scope",
    "source",
    "tags",
    "sensitivity",
    "evidence",
    "valid_from",
    "valid_until",
    "supersedes",
    "created_at",
    "declared_state",
)
_EXPORTED_MEMORY_KEYS = (
    "memory_schema_version",
    "memory_id",
    "type",
    "title",
    "content",
    "scope",
    "source",
    "tags",
    "sensitivity",
    "evidence",
    "valid_from",
    "valid_until",
    "supersedes",
    "created_at",
    "updated_at",
    "state",
    "trust",
    "content_sha256",
    "declared_state",
    "history",
)


def _fail(message: str) -> None:
    raise ValidationError(message)


def _export_digest(entries: list[dict]) -> str:
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _exported_memory(snapshot: dict) -> dict:
    exported = {}
    for key in _EXPORTED_MEMORY_KEYS:
        value = snapshot.get(key)
        if key == "history":
            value = list(snapshot.get("history") or [])[-MAX_TRANSFER_HISTORY:]
        exported[key] = value
    return exported


def export_memories(
    store: MemoryStore,
    *,
    memory_ids: list[str] | None = None,
    tags: list[str] | None = None,
    max_sensitivity: str = "internal",
    include_inactive: bool = False,
    include_private: bool = False,
    as_of: str | None = None,
) -> dict:
    """Build a portable exchange document from an explicit memory store."""

    if max_sensitivity not in SENSITIVITY_LEVELS:
        _fail(
            "memory sensitivity filter must be one of: "
            + ", ".join(SENSITIVITY_LEVELS)
        )
    state = store.snapshots(as_of=as_of)
    memories = state["memories"]
    wanted_ids = {str(value) for value in (memory_ids or [])}
    unknown = sorted(wanted_ids - set(memories))
    if unknown:
        raise StateError(
            "no such memory in this store: " + ", ".join(unknown)
        )
    wanted_tags = {tag.strip().lower() for tag in (tags or []) if tag.strip()}
    entries = []
    for memory_id in sorted(memories):
        snapshot = memories[memory_id]
        if wanted_ids and memory_id not in wanted_ids:
            continue
        if wanted_tags and not wanted_tags & {
            tag.lower() for tag in snapshot["tags"]
        }:
            continue
        if not include_inactive and not is_active(snapshot):
            continue
        if snapshot["sensitivity"] == "private":
            if not include_private:
                continue
        elif (
            SENSITIVITY_RANK[snapshot["sensitivity"]]
            > SENSITIVITY_RANK[max_sensitivity]
        ):
            continue
        entries.append(
            {
                "memory": _exported_memory(snapshot),
                "declared_state": snapshot["state"],
            }
        )
    if len(entries) > MAX_EXPORT_RECORDS:
        raise StateError(
            f"refusing to export {len(entries)} memories; the supported "
            f"maximum is {MAX_EXPORT_RECORDS} per file. Select fewer memories "
            "with --id or --tag."
        )
    document = {
        "format": MEMORY_EXCHANGE_FORMAT,
        "format_version": MEMORY_EXCHANGE_FORMAT_VERSION,
        "memory_schema_version": MEMORY_SCHEMA_VERSION,
        "exported_at": utc_now_iso(),
        "aperio_version": __version__,
        "source": {
            "store": "operator-selected memory store",
            "note": (
                "No paths, hosts, or credentials are included. Content hashes "
                "are integrity checks, not proof of authorship."
            ),
        },
        "entries": entries,
        "counts": {"memories": len(entries)},
        "content_sha256": _export_digest(entries),
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > MAX_EXPORT_BYTES:
        raise StateError(
            f"refusing to export an exchange file larger than "
            f"{MAX_EXPORT_BYTES} bytes; select fewer memories"
        )
    return document


def render_export(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def load_exchange_file(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the memory exchange file; "
            "pass the real file path"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read memory exchange file: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValidationError(
            f"memory exchange file must be at most {MAX_IMPORT_BYTES} bytes"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValidationError(
            "memory exchange file must be valid UTF-8 JSON"
        ) from None
    return validate_exchange_document(document)


def validate_exchange_document(document: object) -> dict:
    if not isinstance(document, dict):
        _fail("memory exchange document must be a JSON object")
    extra = sorted(set(document) - set(_EXPORT_KEYS))
    if extra:
        _fail("memory exchange document has unknown keys: " + ", ".join(extra))
    if document.get("format") != MEMORY_EXCHANGE_FORMAT:
        _fail(
            "not an Aperio memory exchange document (expected format "
            f"'{MEMORY_EXCHANGE_FORMAT}')"
        )
    if document.get("format_version") != MEMORY_EXCHANGE_FORMAT_VERSION:
        _fail(
            "unsupported memory exchange format version "
            f"{document.get('format_version')!r}"
        )
    if document.get("memory_schema_version") != MEMORY_SCHEMA_VERSION:
        _fail(
            "unsupported memory schema version "
            f"{document.get('memory_schema_version')!r}"
        )
    entries = document.get("entries")
    if not isinstance(entries, list):
        _fail("memory exchange 'entries' must be a list")
    if len(entries) > MAX_EXPORT_RECORDS:
        _fail(
            f"memory exchange file holds {len(entries)} entries; the "
            f"supported maximum is {MAX_EXPORT_RECORDS}"
        )
    if document.get("content_sha256") != _export_digest(entries):
        _fail(
            "memory exchange content hash does not match its entries; the "
            "file was altered or truncated"
        )
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _fail(f"memory exchange entry {index} must be an object")
        extra = sorted(set(entry) - set(_ENTRY_KEYS))
        if extra:
            _fail(
                f"memory exchange entry {index} has unknown keys: "
                + ", ".join(extra)
            )
        memory = entry.get("memory")
        if not isinstance(memory, dict):
            _fail(f"memory exchange entry {index} has no memory object")
        extra = sorted(set(memory) - set(_EXPORTED_MEMORY_KEYS))
        if extra:
            _fail(
                f"memory exchange entry {index} has unknown memory keys: "
                + ", ".join(extra)
            )
        record = validate_memory_record(
            {key: value for key, value in memory.items() if key in _RECORD_FIELDS}
        )
        if memory_content_sha256(record) != memory.get("content_sha256"):
            _fail(
                f"memory exchange entry {index} content hash does not match "
                "its content"
            )
        if memory_identity(record) != memory.get("memory_id"):
            _fail(
                f"memory exchange entry {index} memory_id is not derived from "
                "its content"
            )
        if memory.get("state") not in MEMORY_STATES:
            _fail(
                f"memory exchange entry {index} state must be one of: "
                + ", ".join(MEMORY_STATES)
            )
        expected_trust = STATE_TRUST.get(memory["state"], "proposed")
        if memory.get("trust") not in (None, expected_trust):
            _fail(
                f"memory exchange entry {index} trust label disagrees with "
                "its review state"
            )
        declared = entry.get("declared_state", memory.get("state"))
        if declared not in MEMORY_STATES:
            _fail(
                f"memory exchange entry {index} declared_state must be one "
                "of: " + ", ".join(MEMORY_STATES)
            )
        history = memory.get("history")
        if history is not None and not isinstance(history, list):
            _fail(f"memory exchange entry {index} history must be a list")
    return document


def import_memories(
    store: MemoryStore,
    document: dict,
    *,
    actor: str | None = None,
    dry_run: bool = False,
    as_of: str | None = None,
    max_sensitivity: str = "internal",
) -> dict:
    """Import an exchange document, keeping every record untrusted.

    Imported knowledge enters as ``proposed`` no matter what state the file
    declares. Duplicates (same content identity) are skipped, and subject
    conflicts are reported rather than resolved.
    """

    document = validate_exchange_document(document)
    if max_sensitivity not in SENSITIVITY_LEVELS:
        _fail(
            "memory sensitivity filter must be one of: "
            + ", ".join(SENSITIVITY_LEVELS)
        )
    existing = (
        store.snapshots(as_of=as_of)["memories"] if store.path.exists() else {}
    )
    report = {
        "imported": [],
        "duplicates": [],
        "excluded_sensitivity": [],
        "refused": [],
        "conflicts": [],
        "declared_states": {},
        "dry_run": dry_run,
        "actor_attribution": actor or "unspecified",
    }
    for entry in document["entries"]:
        memory = entry["memory"]
        record = validate_memory_record(
            {key: value for key, value in memory.items() if key in _RECORD_FIELDS}
        )
        memory_id = memory_identity(record)
        declared = entry.get("declared_state") or memory.get("state") or "proposed"
        if SENSITIVITY_RANK[record["sensitivity"]] > SENSITIVITY_RANK[max_sensitivity]:
            report["excluded_sensitivity"].append(memory_id)
            continue
        if memory_id in existing:
            report["duplicates"].append(memory_id)
            continue
        conflict = _subject_conflict(existing, record)
        if conflict:
            report["conflicts"].append(conflict)
        if dry_run:
            report["imported"].append(memory_id)
            report["declared_states"][memory_id] = declared
            continue
        proposed = dict(record)
        proposed["source"] = {
            "kind": "import",
            "ref": (record["source"] or {}).get("kind"),
            "note": (
                f"imported from an Aperio memory exchange file; the file "
                f"declared state {declared!r}. Import does not verify the "
                "claim and does not apply that state."
            )[:1000],
        }
        proposed["declared_state"] = (
            declared if declared in MEMORY_STATES else "proposed"
        )
        proposed["created_at"] = record["created_at"] or utc_now_iso()
        result = store.propose(proposed)
        report["imported"].append(memory_id)
        report["declared_states"][memory_id] = declared
        existing[memory_id] = result["memory"]
    return report


def _subject_conflict(existing: dict, record: dict) -> dict | None:
    scope = record["scope"]
    key = (scope["kind"], scope.get("profile"), scope.get("value"), record["title"].lower())
    for memory_id in sorted(existing):
        other = existing[memory_id]
        other_scope = other["scope"]
        other_key = (
            other_scope["kind"],
            other_scope.get("profile"),
            other_scope.get("value"),
            other["title"].lower(),
        )
        if other_key == key and other["content_sha256"] != memory_content_sha256(record):
            return {
                "incoming_memory_id": memory_identity(record),
                "existing_memory_id": memory_id,
                "title": record["title"],
                "resolution": (
                    "both records are kept; review them and supersede or "
                    "retract the one that no longer applies"
                ),
            }
    return None


def summarize_import(report: dict) -> str:
    parts = [
        f"imported {len(report['imported'])}",
        f"duplicates {len(report['duplicates'])}",
        f"conflicts {len(report['conflicts'])}",
        f"excluded for sensitivity {len(report['excluded_sensitivity'])}",
    ]
    return ", ".join(parts)
