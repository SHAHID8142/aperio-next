"""Universal, provider-neutral memory records and store (memory-schema v1).

This is the M3 portable-memory foundation. A memory is a small, bounded JSON
record about knowledge worth keeping across sessions — a preference, a
requirement, a decision, an observation, a limitation, a lesson, a
convention, a proposal, or an outcome. Nothing here assumes software, a
repository, a plan, a provider, or a model API: a store is a single
operator-named file and works with no ``.aperio/`` project at all.

Design rules enforced here:

- **Append-only history.** The store is one JSON object per line: a header,
  then immutable ``memory`` records and immutable ``transition`` events.
  Current state is derived by replaying the log in order, so a review can
  always see what was known, when, and by whom, and no transition silently
  rewrites the past.
- **Identity from content.** ``memory_id`` is derived from a canonical
  content hash (type, title, content, scope, tags, evidence) and excludes
  state, timestamps, paths, and versions. The same knowledge keeps the same
  identity across stores, machines, and agents, and event timestamps stay
  separate from reproducible identity.
- **Trust is earned, never inherited.** Every record starts ``proposed``.
  Only an explicit transition makes it ``attested`` (the operator accepts it
  as authoritative *for its scope*) or ``verified`` (independent evidence
  supports the claim). Importing a file never raises trust.
- **No executable content.** Records are data: no rules engine, no code, no
  plugin, no automatic promotion, no network.
- **Fail closed.** Unknown keys, wrong types, oversized values, foreign line
  separators, control characters, damaged lines, and unsafe paths are
  refused or reported rather than repaired silently.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

from aperio.artifacts import publish_text_atomic
from aperio.errors import PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.locks import acquire_lock, release_lock
from aperio.schema import utc_now_iso

MEMORY_SCHEMA_VERSION = 1
MEMORY_STORE_FORMAT = "aperio-memory-store"
MEMORY_STORE_FORMAT_VERSION = 1

MAX_STORE_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_TITLE_BYTES = 200
MAX_CONTENT_BYTES = 8 * 1024
MAX_TAG_BYTES = 64
MAX_TAGS = 12
MAX_EVIDENCE_BYTES = 300
MAX_EVIDENCE_ITEMS = 12
MAX_SCOPE_VALUE_BYTES = 120
MAX_MEMORIES_PER_STORE = 2000
MAX_HISTORY_ENTRIES = 32
MAX_NOTE_BYTES = 1000
MAX_ACTOR_BYTES = 64
MAX_REASON_BYTES = 300

MEMORY_TYPES = (
    "user_preference",
    "requirement",
    "decision",
    "observation",
    "limitation",
    "lesson",
    "convention",
    "proposal",
    "outcome",
)

# State names follow the M1 trust vocabulary where the meanings coincide:
# ``proposed`` and ``attested`` are M1 trust tiers, and ``verified`` is the
# M1 evidence tier. The remaining states describe a record's lifecycle, not
# its trust, and are never used to assert more confidence than the evidence.
MEMORY_STATES = (
    "proposed",
    "attested",
    "verified",
    "superseded",
    "retracted",
    "expired",
)
ACTIVE_STATES = ("proposed", "attested", "verified")
INACTIVE_STATES = ("superseded", "retracted", "expired")
STATE_TRUST = {
    "proposed": "proposed",
    "attested": "attested",
    "verified": "verified",
}
SENSITIVITY_LEVELS = ("public", "internal", "private")
DEFAULT_SENSITIVITY = "internal"
SENSITIVITY_RANK = {level: index for index, level in enumerate(SENSITIVITY_LEVELS)}

SCOPE_KINDS = ("global", "profile", "objective", "workspace")
SOURCE_KINDS = ("operator", "agent", "import", "workspace")

MEMORY_RECORD_KEYS = (
    "memory_schema_version",
    "memory_id",
    "revision",
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
    "actor",
    "declared_state",
)
SCOPE_KEYS = ("kind", "value", "profile")
SOURCE_KEYS = ("kind", "ref", "note")
TRANSITION_KEYS = (
    "action",
    "note",
    "evidence",
    "supersedes_reason",
)

# Allowed review transitions. There is deliberately no path that increases
# trust without a named action, actor, and timestamp, and no path from a
# terminal state back to an active one: corrections are new records.
ALLOWED_TRANSITIONS = {
    "accept": {"from": ("proposed",), "to": "attested"},
    "verify": {"from": ("proposed", "attested"), "to": "verified"},
    "retract": {"from": ("proposed", "attested", "verified"), "to": "retracted"},
    "supersede": {
        "from": ("proposed", "attested", "verified"),
        "to": "superseded",
    },
    "reinstate": {"from": ("retracted", "superseded"), "to": "proposed"},
}

_CHAIN_FIELD = "chain_sha256"


def line_sha256(line: dict, previous: str | None = None) -> str:
    """Chain hash for one stored line.

    Each line is hashed together with the hash of the line before it, so an
    edit, reordering, deletion, or truncation anywhere in the file — including
    the last line — is detectable. This is corruption detection only, exactly
    like the project manifest: anyone who can write the file can recompute the
    chain, so it is never authentication.
    """

    payload = {key: value for key, value in line.items() if key != _CHAIN_FIELD}
    return hashlib.sha256(
        (
            (previous or "")
            + "\n"
            + json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        ).encode("utf-8")
    ).hexdigest()


_STORE_HEADER_KEYS = (
    "record",
    "memory_store_format",
    "format_version",
    "memory_schema_version",
    "created_at",
)
_MEMORY_LINE_KEYS = ("record", "content_sha256", "state", "declared_state")
_TRANSITION_LINE_KEYS = ("record", "memory_id", "revision", "at", "action", "from_state", "to_state")

_ID_PREFIX = "MEM-"


# ---------------------------------------------------------------------------
# small validation helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> None:
    raise ValidationError(message)


def _text(
    value: object,
    field: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    if not allow_empty and not value.strip():
        _fail(f"{field} must not be empty")
    check_no_foreign_line_separators(field, field, value)
    if "\x00" in value:
        _fail(f"{field} must not contain NUL bytes")
    if single_line and "\n" in value:
        _fail(f"{field} must be a single line")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        _fail(f"{field} must be at most {max_bytes} UTF-8 bytes")
    return value


def _text_list(
    value: object,
    field: str,
    *,
    max_items: int,
    max_bytes: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        _fail(f"{field} must be a list of strings")
    items = list(value)
    if len(items) > max_items:
        _fail(f"{field} must contain at most {max_items} items")
    return [
        _text(item, f"{field}[{index}]", max_bytes=max_bytes, single_line=True)
        for index, item in enumerate(items)
    ]


def _optional_text(
    value: object, field: str, *, max_bytes: int, single_line: bool = False
) -> str | None:
    if value is None:
        return None
    return _text(
        value, field, max_bytes=max_bytes, single_line=single_line
    )


def _iso_date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 10:
        _fail(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(f"{field} must be an ISO date (YYYY-MM-DD)")
    return value


def _choice(value: object, choices: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail(f"{field} must be one of: " + ", ".join(choices))
    return value


def _unknown(obj: dict, allowed: tuple[str, ...], field: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        _fail(f"{field} has unknown keys: " + ", ".join(extra))


def _memory_id(value: object, field: str = "memory_id") -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    body = value[len(_ID_PREFIX) :] if value.startswith(_ID_PREFIX) else ""
    if len(body) != 12 or any(ch not in "0123456789abcdef" for ch in body):
        _fail(f"{field} must look like MEM-<12 hex characters>")
    return value


# ---------------------------------------------------------------------------
# canonical content and identity
# ---------------------------------------------------------------------------


def normalize_scope(scope: object) -> dict:
    if scope is None:
        return {"kind": "global", "value": None, "profile": None}
    if not isinstance(scope, dict):
        _fail("memory scope must be an object")
    _unknown(scope, SCOPE_KEYS, "memory scope")
    kind = _choice(scope.get("kind", "global"), SCOPE_KINDS, "memory scope.kind")
    value = _optional_text(
        scope.get("value"), "memory scope.value", max_bytes=MAX_SCOPE_VALUE_BYTES
    )
    profile = _optional_text(
        scope.get("profile"), "memory scope.profile", max_bytes=64, single_line=True
    )
    if kind == "global" and (value or profile):
        _fail("memory scope.kind 'global' must not carry a value or profile")
    if kind == "profile" and not profile:
        _fail("memory scope.kind 'profile' requires scope.profile")
    if kind in ("objective", "workspace") and not value:
        _fail(f"memory scope.kind '{kind}' requires scope.value")
    return {"kind": kind, "value": value, "profile": profile}


def normalize_source(source: object) -> dict:
    if source is None:
        return {"kind": "operator", "ref": None, "note": None}
    if not isinstance(source, dict):
        _fail("memory source must be an object")
    _unknown(source, SOURCE_KEYS, "memory source")
    kind = _choice(source.get("kind", "operator"), SOURCE_KINDS, "memory source.kind")
    return {
        "kind": kind,
        "ref": _optional_text(
            source.get("ref"), "memory source.ref", max_bytes=200, single_line=True
        ),
        "note": _optional_text(
            source.get("note"), "memory source.note", max_bytes=MAX_NOTE_BYTES
        ),
    }


def canonical_memory_bytes(record: dict) -> bytes:
    """Canonical bytes for the content identity of a memory record.

    Deliberately excludes state, revisions, timestamps, actor, paths, and
    store versions: the identity must survive export, import, and review.
    """

    canonical = {
        "type": record["type"],
        "title": record["title"],
        "content": record["content"],
        # Scope is normalized so two spellings of the same scope (for example
        # ``{"kind": "global"}`` and the expanded form) are one identity.
        "scope": normalize_scope(record["scope"]),
        "tags": list(record["tags"]),
        "evidence": list(record["evidence"]),
    }
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def memory_content_sha256(record: dict) -> str:
    if not isinstance(record, dict):
        _fail("memory record must be an object")
    for key in ("type", "title", "content", "scope", "tags", "evidence"):
        if key not in record:
            _fail(f"memory record is missing '{key}'")
    return hashlib.sha256(canonical_memory_bytes(record)).hexdigest()


def memory_identity(record: dict) -> str:
    return _ID_PREFIX + memory_content_sha256(record)[:12]


def memory_revision_sha256(record: dict) -> str:
    """Hash over the whole stored record, used for corruption detection."""

    return hashlib.sha256(
        json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def memory_trust(state: str) -> str:
    _choice(state, MEMORY_STATES, "memory state")
    return STATE_TRUST.get(state, "proposed")


def validate_memory_record(record: object, *, require_id: bool = True) -> dict:
    """Validate and normalize a memory record (memory-schema v1).

    Unknown keys are refused at every level. Values are bounded, single-line
    where they are labels, and newline-normalized to ``\\n`` only.
    """

    if not isinstance(record, dict):
        _fail("memory record must be an object")
    _unknown(record, MEMORY_RECORD_KEYS, "memory record")
    version = record.get("memory_schema_version", MEMORY_SCHEMA_VERSION)
    if version != MEMORY_SCHEMA_VERSION:
        _fail(
            "memory record memory_schema_version must be "
            f"{MEMORY_SCHEMA_VERSION}"
        )
    normalized = {
        "memory_schema_version": MEMORY_SCHEMA_VERSION,
        "memory_id": (
            _memory_id(record.get("memory_id"))
            if require_id and record.get("memory_id") is not None
            else None
        ),
        "revision": record.get("revision", 1),
        "type": _choice(record.get("type"), MEMORY_TYPES, "memory type"),
        "title": _text(
            record.get("title"),
            "memory title",
            max_bytes=MAX_TITLE_BYTES,
            single_line=True,
        ),
        "content": _text(
            record.get("content"),
            "memory content",
            max_bytes=MAX_CONTENT_BYTES,
        ),
        "scope": normalize_scope(record.get("scope")),
        "source": normalize_source(record.get("source")),
        "tags": _text_list(
            record.get("tags"), "memory tags", max_items=MAX_TAGS, max_bytes=MAX_TAG_BYTES
        ),
        "sensitivity": _choice(
            record.get("sensitivity", DEFAULT_SENSITIVITY),
            SENSITIVITY_LEVELS,
            "memory sensitivity",
        ),
        "evidence": _text_list(
            record.get("evidence"),
            "memory evidence",
            max_items=MAX_EVIDENCE_ITEMS,
            max_bytes=MAX_EVIDENCE_BYTES,
        ),
        "valid_from": _iso_date(record.get("valid_from"), "memory valid_from"),
        "valid_until": _iso_date(record.get("valid_until"), "memory valid_until"),
        "supersedes": (
            _memory_id(record.get("supersedes"), "memory supersedes")
            if record.get("supersedes") is not None
            else None
        ),
        "created_at": _optional_text(
            record.get("created_at"),
            "memory created_at",
            max_bytes=40,
            single_line=True,
        ),
        "actor": _optional_text(
            record.get("actor"),
            "memory actor",
            max_bytes=MAX_ACTOR_BYTES,
            single_line=True,
        ),
        "declared_state": (
            _choice(
                record.get("declared_state"),
                MEMORY_STATES,
                "memory declared_state",
            )
            if record.get("declared_state") is not None
            else None
        ),
    }
    revision = normalized["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _fail("memory revision must be a positive integer")
    if normalized["valid_from"] and normalized["valid_until"]:
        if normalized["valid_from"] > normalized["valid_until"]:
            _fail("memory valid_from must not be after valid_until")
    if len(json.dumps(normalized).encode("utf-8")) > MAX_RECORD_BYTES:
        _fail(f"memory record must be at most {MAX_RECORD_BYTES} UTF-8 bytes")
    return normalized


# ---------------------------------------------------------------------------
# snapshot derivation
# ---------------------------------------------------------------------------


def _snapshot(record: dict, events: list[dict], *, as_of: str | None) -> dict:
    state = "proposed"
    updated_at = record.get("created_at")
    history = []
    declared_state = record.get("declared_state")
    for event in events:
        state = event["to_state"]
        updated_at = event["at"]
        declared_state = event.get("declared_state", declared_state)
        history.append(dict(event))
    snapshot = {
        "memory_schema_version": record["memory_schema_version"],
        "memory_id": memory_identity(record),
        "revision": 1 + len(events),
        "type": record["type"],
        "title": record["title"],
        "content": record["content"],
        "scope": record["scope"],
        "source": record["source"],
        "tags": list(record["tags"]),
        "sensitivity": record["sensitivity"],
        "evidence": list(record["evidence"]),
        "valid_from": record["valid_from"],
        "valid_until": record["valid_until"],
        "supersedes": record["supersedes"],
        "created_at": record["created_at"],
        "updated_at": updated_at,
        "actor": record["actor"],
        "state": state,
        "trust": memory_trust(state),
        "content_sha256": memory_content_sha256(record),
        "declared_state": declared_state,
        "history": history[-MAX_HISTORY_ENTRIES:],
        "history_entries": len(events),
    }
    if state in ("attested", "verified") and as_of:
        until = snapshot["valid_until"]
        if until and as_of > until:
            snapshot["state"] = "expired"
            snapshot["trust"] = "proposed"
            snapshot["expired_reason"] = f"valid_until {until} passed"
    return snapshot


def is_active(snapshot: dict) -> bool:
    return snapshot["state"] in ACTIVE_STATES


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class MemoryStore:
    """One operator-named JSONL memory store.

    The path is chosen explicitly by the operator. Aperio never creates a
    global store, never scans directories, and never guesses a location. The
    store is created exclusively on first write and appended under the
    project's established advisory lock (``<store>.lock``).
    """

    def __init__(self, path: str | Path, *, artifact: str = "memory store"):
        self.path = Path(path)
        resolved = Path(os.path.realpath(self.path.parent)) / self.path.name
        if ".aperio" in resolved.parts:
            raise PathError(
                "refusing to use a memory file inside '.aperio/' project "
                "state; choose a path outside it (for example ~/notes/work.mem)"
            )
        self.artifact = artifact
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    # -- reading -----------------------------------------------------------
    def read(self) -> dict:
        """Read and replay the store. Damaged lines are reported, not fatal."""

        if not self.path.exists():
            raise StateError(
                f"no memory store at {self.path}; create one with "
                "'aperio mem-propose --store <path>' using an explicit path "
                "you choose"
            )
        if os.path.islink(self.path):
            raise PathError(
                f"refusing to follow symlink at {self.path} for the memory store"
            )
        if not self.path.is_file():
            raise PathError(f"memory store path {self.path} is not a regular file")
        size = self.path.stat().st_size
        if size > MAX_STORE_BYTES:
            raise StateError(
                f"memory store {self.path} is larger than the supported "
                f"{MAX_STORE_BYTES} bytes; split it into separate stores"
            )
        raw = self.path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise StateError(
                f"memory store {self.path} is not valid UTF-8; no memory was read"
            ) from None

        damaged: list[dict] = []
        header: dict | None = None
        expected_prev: str | None = None
        records: dict[str, dict] = {}
        events: dict[str, list[dict]] = {}
        order: list[str] = []

        for index, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                damaged.append({"line": index, "reason": "not valid JSON"})
                continue
            if not isinstance(entry, dict):
                damaged.append({"line": index, "reason": "not a JSON object"})
                continue
            computed = line_sha256(entry, expected_prev)
            if entry.get(_CHAIN_FIELD) != computed:
                damaged.append(
                    {
                        "line": index,
                        "reason": (
                            "line does not continue the store's hash chain; "
                            "the file was edited, reordered, or truncated"
                        ),
                    }
                )
                # Keep reading so the rest of the store stays visible, but
                # re-anchor on this line so one edit is not reported as
                # dozens of cascading mismatches. The line itself is never
                # trusted, even when its fields look internally consistent.
                expected_prev = computed
                continue
            expected_prev = computed
            kind = entry.get("record")
            if kind == "store" and header is None:
                header = entry
            elif kind == "memory":
                try:
                    record = validate_memory_record(
                        {k: v for k, v in entry.items() if k in MEMORY_RECORD_KEYS}
                    )
                    expected = entry.get("content_sha256")
                    if expected != memory_content_sha256(record):
                        raise ValidationError(
                            "content hash does not match the stored record"
                        )
                    memory_id = memory_identity(record)
                    if entry.get("memory_id") != memory_id:
                        raise ValidationError(
                            "memory_id does not match the memory's content"
                        )
                    if memory_id in records:
                        raise ValidationError(
                            "duplicate memory record for the same identity"
                        )
                    records[memory_id] = record
                    events[memory_id] = []
                    order.append(memory_id)
                except ValidationError as error:
                    damaged.append({"line": index, "reason": str(error)})
            elif kind == "transition":
                memory_id = entry.get("memory_id")
                if memory_id not in records:
                    damaged.append(
                        {"line": index, "reason": "transition for unknown memory"}
                    )
                    continue
                try:
                    event = _validate_transition_event(entry)
                    previous = events[memory_id]
                    current_state = (
                        previous[-1]["to_state"] if previous else "proposed"
                    )
                    if event["from_state"] != current_state:
                        raise ValidationError(
                            "transition does not continue the recorded state "
                            f"({event['from_state']!r} after "
                            f"{current_state!r})"
                        )
                    if event["revision"] != 2 + len(previous):
                        raise ValidationError(
                            "transition revision does not follow the "
                            "recorded history"
                        )
                    events[memory_id].append(event)
                except ValidationError as error:
                    damaged.append({"line": index, "reason": str(error)})
            else:
                damaged.append({"line": index, "reason": "unknown record type"})

        if header is None:
            raise StateError(
                f"memory store {self.path} has no valid header line; "
                "Aperio will not guess its format"
            )
        if header.get("memory_store_format") != MEMORY_STORE_FORMAT:
            raise StateError(
                f"memory store {self.path} is not an "
                f"'{MEMORY_STORE_FORMAT}' file"
            )
        if header.get("format_version") != MEMORY_STORE_FORMAT_VERSION:
            raise StateError(
                f"memory store {self.path} has unsupported format version "
                f"{header.get('format_version')!r}"
            )
        if header.get("memory_schema_version") != MEMORY_SCHEMA_VERSION:
            raise StateError(
                f"memory store {self.path} has unsupported memory schema "
                f"{header.get('memory_schema_version')!r}"
            )
        return {
            "path": self.path,
            "header": header,
            "records": [records[key] for key in order],
            "events": {key: events[key] for key in order},
            "damaged": damaged,
            "bytes": size,
        }

    def snapshots(self, *, as_of: str | None = None) -> dict:
        """Current snapshots keyed by memory id, plus read diagnostics."""

        data = self.read()
        snapshots = {
            record_id: _snapshot(record, data["events"][record_id], as_of=as_of)
            for record_id, record in (
                (memory_identity(record), record) for record in data["records"]
            )
        }
        return {
            "memories": snapshots,
            "damaged": data["damaged"],
            "bytes": data["bytes"],
            "header": data["header"],
        }

    # -- writing -----------------------------------------------------------
    def _check_tail_is_intact(self) -> None:
        """Refuse to append after an incomplete or corrupted tail.

        Appending past a broken chain would make every later line unreadable,
        so a store that ends mid-line (an interrupted write) or whose last
        line breaks the chain fails closed with an actionable message. The
        existing records stay readable; nothing is repaired silently.
        """

        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise StateError(
                f"memory store {self.path} ends with an incomplete line (an "
                "interrupted write); inspect it with 'aperio mem-list' and "
                "repair the file before adding memories"
            )
        state = self.snapshots()
        if state["damaged"]:
            first = state["damaged"][0]
            raise StateError(
                f"memory store {self.path} has a damaged line "
                f"({first['line']}: {first['reason']}); inspect it with "
                "'aperio mem-list' and repair the file before adding memories"
            )

    def _mutate(self, build):
        """Run one read-modify-append cycle under the store's advisory lock.

        The decision (create vs append, duplicate check, allowed transition)
        is made *inside* the lock, so two concurrent writers can never both
        decide to create the store or apply the same transition twice.
        """

        parent = self.path.parent
        if not parent.exists():
            raise PathError(
                f"cannot create memory store at {self.path}: the parent "
                "directory does not exist; choose an existing directory"
            )
        if not parent.is_dir():
            raise PathError(
                f"cannot create memory store at {self.path}: the parent path "
                "is not a directory"
            )
        token = acquire_lock(self.lock_path, artifact=self.artifact)
        try:
            exists = self.path.exists()
            if exists and os.path.islink(self.path):
                raise PathError(
                    f"refusing to follow symlink at {self.path} for the "
                    "memory store"
                )
            if exists:
                self._check_tail_is_intact()
            lines = build(exists)
            if not lines:
                return None
            previous: str | None = None
            if exists:
                previous = _last_line_hash(self.path)
            chained = []
            for line in lines:
                chained_line = {**line}
                chained_line[_CHAIN_FIELD] = line_sha256(chained_line, previous)
                chained.append(chained_line)
                previous = chained_line[_CHAIN_FIELD]
            payload = "".join(
                json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n"
                for line in chained
            ).encode("utf-8")
            if exists:
                current = self.path.stat().st_size
                if current + len(payload) > MAX_STORE_BYTES:
                    raise StateError(
                        f"memory store {self.path} would exceed the supported "
                        f"{MAX_STORE_BYTES} bytes; split it into separate stores"
                    )
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
            else:
                publish_text_atomic(
                    self.path,
                    payload.decode("utf-8"),
                    what="memory store",
                )
            return lines
        finally:
            release_lock(self.lock_path, token)

    def _plan_mutation(self, memory_id: str, action: str, **fields) -> dict:
        state = self.snapshots()
        snapshot = state["memories"].get(memory_id)
        if snapshot is None:
            raise StateError(f"no memory {memory_id} in {self.path}")
        rule = ALLOWED_TRANSITIONS.get(action)
        if rule is None:
            raise ValidationError(
                "memory action must be one of: "
                + ", ".join(sorted(ALLOWED_TRANSITIONS))
            )
        if snapshot["state"] not in rule["from"]:
            raise StateError(
                f"memory {memory_id} is {snapshot['state']}; "
                f"'{action}' is only allowed from: " + ", ".join(rule["from"])
            )
        return snapshot

    def propose(self, record: dict) -> dict:
        """Append a new memory record in state ``proposed``.

        Re-proposing identical content is a no-op that returns the existing
        record, so repeated proposals cannot duplicate knowledge.
        """

        normalized = validate_memory_record(
            {
                **record,
                "memory_schema_version": MEMORY_SCHEMA_VERSION,
                "created_at": record.get("created_at") or _utc_now(),
            },
            require_id=False,
        )
        memory_id = memory_identity(normalized)
        if (
            record.get("memory_id") is not None
            and record["memory_id"] != memory_id
        ):
            raise ValidationError(
                "memory_id must be derived from the memory content; omit it "
                "and Aperio will compute it"
            )
        outcome: dict = {}

        def build(exists: bool) -> list[dict]:
            if exists:
                state = self.snapshots()
                existing = state["memories"].get(memory_id)
                if existing is not None:
                    outcome["duplicate"] = existing
                    return []
                if len(state["memories"]) >= MAX_MEMORIES_PER_STORE:
                    raise StateError(
                        f"memory store {self.path} already holds "
                        f"{len(state['memories'])} memories (the supported "
                        f"maximum is {MAX_MEMORIES_PER_STORE}); split it into "
                        "separate stores"
                    )
                lines: list[dict] = []
            else:
                lines = [
                    {
                        "record": "store",
                        "memory_store_format": MEMORY_STORE_FORMAT,
                        "format_version": MEMORY_STORE_FORMAT_VERSION,
                        "memory_schema_version": MEMORY_SCHEMA_VERSION,
                        "created_at": normalized["created_at"],
                    }
                ]
            lines.append(
                {
                    "record": "memory",
                    **{key: normalized[key] for key in MEMORY_RECORD_KEYS},
                    "memory_id": memory_id,
                    "content_sha256": memory_content_sha256(normalized),
                    "state": "proposed",
                }
            )
            return lines

        self._mutate(build)
        if "duplicate" in outcome:
            return {
                "memory": outcome["duplicate"],
                "created": False,
                "duplicate": True,
            }
        snapshot = self.snapshots()["memories"][memory_id]
        return {"memory": snapshot, "created": True, "duplicate": False}

    def review(
        self,
        memory_id: str,
        action: str,
        *,
        actor: str | None = None,
        note: str | None = None,
        evidence: list[str] | None = None,
        reason: str | None = None,
        declared_state: str | None = None,
    ) -> dict:
        """Record an explicit review transition and return the new snapshot."""

        memory_id = _memory_id(memory_id)
        note_text = _optional_text(note, "memory note", max_bytes=MAX_NOTE_BYTES)
        evidence_list = _text_list(
            evidence,
            "memory evidence",
            max_items=MAX_EVIDENCE_ITEMS,
            max_bytes=MAX_EVIDENCE_BYTES,
        )
        reason_text = _optional_text(
            reason, "memory reason", max_bytes=MAX_REASON_BYTES
        )
        if action not in ALLOWED_TRANSITIONS:
            raise ValidationError(
                "memory action must be one of: "
                + ", ".join(sorted(ALLOWED_TRANSITIONS))
            )

        def build(exists: bool) -> list[dict]:
            if not exists:
                raise StateError(
                    f"no memory store at {self.path}; nothing to review"
                )
            snapshot = self._plan_mutation(memory_id, action)
            event = {
                "record": "transition",
                "memory_id": memory_id,
                "revision": snapshot["revision"] + 1,
                "at": _utc_now(),
                "actor": actor,
                "action": action,
                "from_state": snapshot["state"],
                "to_state": ALLOWED_TRANSITIONS[action]["to"],
                "note": note_text,
                "evidence": evidence_list,
                "reason": reason_text,
            }
            if declared_state is not None:
                event["declared_state"] = declared_state
            return [event]

        self._mutate(build)
        return self.snapshots()["memories"][memory_id]


def _last_line_hash(path: Path) -> str:
    """Chain hash of the final stored line, used to extend the chain."""

    raw = path.read_bytes()
    for line in reversed(raw.decode("utf-8", "replace").split("\n")):
        if line.strip():
            try:
                entry = json.loads(line)
            except ValueError:
                return ""
            value = entry.get(_CHAIN_FIELD)
            return value if isinstance(value, str) else ""
    return ""


def _validate_transition_event(entry: dict) -> dict:
    _unknown(
        entry,
        ("record", "memory_id", "revision", "at", "actor", "action", "from_state",
         "to_state", "note", "evidence", "reason", "declared_state",
         _CHAIN_FIELD),
        "memory transition",
    )
    action = _choice(entry.get("action"), tuple(sorted(ALLOWED_TRANSITIONS)), "memory action")
    rule = ALLOWED_TRANSITIONS[action]
    from_state = _choice(entry.get("from_state"), MEMORY_STATES, "memory from_state")
    to_state = _choice(entry.get("to_state"), MEMORY_STATES, "memory to_state")
    if from_state not in rule["from"] or to_state != rule["to"]:
        _fail(
            f"memory transition '{action}' cannot go from {from_state} to "
            f"{to_state}"
        )
    revision = entry.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 2:
        _fail("memory transition revision must be an integer >= 2")
    return {
        "memory_id": _memory_id(entry.get("memory_id")),
        "revision": revision,
        "at": _text(entry.get("at"), "memory transition at", max_bytes=40, single_line=True),
        "actor": _optional_text(
            entry.get("actor"), "memory transition actor", max_bytes=MAX_ACTOR_BYTES,
            single_line=True,
        ),
        "action": action,
        "from_state": from_state,
        "to_state": to_state,
        "note": _optional_text(
            entry.get("note"), "memory transition note", max_bytes=MAX_NOTE_BYTES
        ),
        "evidence": _text_list(
            entry.get("evidence"),
            "memory transition evidence",
            max_items=MAX_EVIDENCE_ITEMS,
            max_bytes=MAX_EVIDENCE_BYTES,
        ),
        "reason": _optional_text(
            entry.get("reason"), "memory transition reason", max_bytes=MAX_REASON_BYTES
        ),
        **(
            {"declared_state": entry["declared_state"]}
            if entry.get("declared_state") is not None
            else {}
        ),
    }


def _utc_now() -> str:
    return utc_now_iso()


__all__ = [
    "ACTIVE_STATES",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_SENSITIVITY",
    "INACTIVE_STATES",
    "MAX_STORE_BYTES",
    "MEMORY_SCHEMA_VERSION",
    "MEMORY_STATES",
    "MEMORY_STORE_FORMAT",
    "MEMORY_STORE_FORMAT_VERSION",
    "MEMORY_TYPES",
    "MemoryStore",
    "SCOPE_KINDS",
    "SENSITIVITY_LEVELS",
    "SENSITIVITY_RANK",
    "SOURCE_KINDS",
    "STATE_TRUST",
    "canonical_memory_bytes",
    "is_active",
    "memory_content_sha256",
    "memory_identity",
    "memory_revision_sha256",
    "memory_trust",
    "normalize_scope",
    "normalize_source",
    "validate_memory_record",
]
