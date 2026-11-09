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
import stat as stat_module
from datetime import date, datetime, timezone
from collections.abc import Mapping
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
_BASIS_RANK = {"not-verified": 0, "unrecorded": 1, "evidence": 2}
_TRUST_RANK = {"proposed": 0, "attested": 1, "verified": 2}

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
#
# ``verify`` additionally requires recorded evidence and a declared reviewer
# (see :meth:`MemoryStore.review`); it is also allowed from ``verified`` so a
# record verified before that requirement existed can be re-affirmed with the
# evidence attached, instead of being retracted and re-proposed.
ALLOWED_TRANSITIONS = {
    "accept": {"from": ("proposed",), "to": "attested"},
    "verify": {"from": ("proposed", "attested", "verified"), "to": "verified"},
    "retract": {"from": ("proposed", "attested", "verified"), "to": "retracted"},
    "supersede": {
        "from": ("proposed", "attested", "verified"),
        "to": "superseded",
    },
    "reinstate": {"from": ("retracted", "superseded"), "to": "proposed"},
}

_CHAIN_FIELD = "chain_sha256"

VALIDITY_RULE = "valid_from <= date <= valid_until (both ends inclusive)"

VERIFICATION_INDEPENDENCE_NOTE = (
    "Declared identifiers only: Aperio cannot authenticate who ran the "
    "review, so this records separation of declared identities, not proven "
    "independence."
)

# Used for partial-write hardening; tests replace this single indirection.
_os_write = os.write


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


def _utc_today() -> str:
    """Today's date in UTC, independent of the machine's local timezone.

    Single source for the default evaluation date, so a store read on the
    same UTC day gives the same answer wherever the operator is.
    """

    return datetime.now(timezone.utc).date().isoformat()


def evaluate_on(value: str | None = None) -> str:
    """Return the ISO date a validity question is evaluated against.

    ``None`` means *today* in UTC — not local time — so ordinary recall
    applies ``valid_from`` and ``valid_until`` without the operator having to
    pass a flag, and two machines on the same UTC day agree. A given value is
    validated strictly (``YYYY-MM-DD``, a real calendar date) so a typo can
    never silently disable expiry.
    """

    if value is None:
        return _utc_today()
    if not isinstance(value, str) or len(value) != 10:
        _fail("memory as_of must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("memory as_of must be an ISO date (YYYY-MM-DD)")
    return value


def memory_validity(memory: dict, *, as_of: str | None = None) -> dict:
    """Whether a memory is applicable on ``as_of`` (default: today).

    Both bounds are **inclusive**: a memory is current from the start of
    ``valid_from`` through the end of ``valid_until``. Expiry is derived at
    read time, so a stored record is never rewritten as time passes, and the
    same inputs plus the same evaluation date always give the same answer.
    """

    evaluated = evaluate_on(as_of)
    valid_from = memory.get("valid_from")
    valid_until = memory.get("valid_until")
    reason: str | None = None
    detail: str | None = None
    if valid_from and evaluated < valid_from:
        reason = "future"
        detail = f"valid_from {valid_from} has not started"
    elif valid_until and evaluated > valid_until:
        reason = "expired"
        detail = f"valid_until {valid_until} passed"
    return {
        "evaluated_on": evaluated,
        "current": reason is None,
        "reason": reason,
        "detail": detail,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "rule": VALIDITY_RULE,
    }


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


def _verification(record: dict, events: list[dict], state: str) -> dict:
    """How a claimed state was reached, without inventing confidence.

    Effective trust is derived from what the recorded transitions actually
    prove, not from the state name alone. A ``verified`` state counts as
    evidence-backed only when a recorded event carries **both** a supporting
    reference and a reviewer identifier, and that reviewer is not the
    memory's declared proposer; anything else stays exactly as recorded in
    history but is reported as ``unrecorded``, downgraded to ``attested``,
    and carries the precise reason. The same rule one tier lower: an
    ``attested`` state is only supported when the history contains the
    ``accept`` transition that produces it — a record that claims acceptance
    without that transition is downgraded to ``proposed``. Store records are
    replayed from their events, so a genuine snapshot always proves its own
    state; only records that arrive from outside a store can claim a state
    their history does not show.
    """

    if state == "attested":
        if not any(event["to_state"] == "attested" for event in events):
            return {
                "basis": "unrecorded",
                "evidence": [],
                "reviewer": None,
                "reviewer_distinct_from_proposer": None,
                "independence": "not-applicable",
                "downgraded": True,
                "reasons": [
                    "the recorded review history does not contain the "
                    "acceptance transition"
                ],
                "detail": (
                    "this record is marked attested without a recorded "
                    "acceptance transition; it is not treated as accepted "
                    "and is shown as proposed. Record the acceptance with "
                    "'aperio mem-review --accept' on the store that owns "
                    "this memory."
                ),
                "note": VERIFICATION_INDEPENDENCE_NOTE,
            }
        return {
            "basis": "not-verified",
            "evidence": [],
            "reviewer": None,
            "reviewer_distinct_from_proposer": None,
            "independence": "not-applicable",
            "note": VERIFICATION_INDEPENDENCE_NOTE,
        }

    if state != "verified":
        return {
            "basis": "not-verified",
            "evidence": [],
            "reviewer": None,
            "reviewer_distinct_from_proposer": None,
            "independence": "not-applicable",
            "note": VERIFICATION_INDEPENDENCE_NOTE,
        }
    verify_event = None
    for event in events:
        if event["to_state"] == "verified":
            verify_event = event
    evidence = list(verify_event["evidence"]) if verify_event else []
    reviewer = verify_event.get("actor") if verify_event else None
    proposer = record.get("actor")
    reasons: list[str] = []
    if verify_event is None:
        # Either the transition was never recorded, or the history no longer
        # reaches back to it. Both mean the same thing to a reader: this
        # record's own history does not prove the claim.
        reasons.append(
            "the recorded review history does not contain the verification "
            "transition"
        )
    if not evidence:
        reasons.append("no supporting evidence reference was recorded")
    if not reviewer:
        reasons.append("no reviewer identifier was recorded for the verification")
        distinct: bool | None = None
        independence = "unknown"
    elif proposer:
        distinct = reviewer.strip().lower() != proposer.strip().lower()
        independence = (
            "declared-distinct-identifiers" if distinct else "same-identifier"
        )
        if not distinct:
            reasons.append(
                "the reviewer is the same declared identifier as the proposer"
            )
    else:
        distinct = None
        independence = "unknown"
    basis = "evidence" if not reasons else "unrecorded"
    verification = {
        "basis": basis,
        "evidence": evidence,
        "reviewer": reviewer,
        "reviewer_distinct_from_proposer": distinct,
        "independence": independence,
        "note": VERIFICATION_INDEPENDENCE_NOTE,
    }
    if reasons:
        verification["downgraded"] = True
        verification["reasons"] = reasons
        verification["detail"] = (
            "this record was marked verified without proof that its claim was "
            "supported and independently attributed; it is not treated as "
            "evidence-backed. Re-run 'aperio mem-review --verify' with "
            "--evidence and a different --actor to record support."
        )
    return verification


def _history_events(history: object) -> list[dict]:
    """Normalise the review events a record carries, ignoring junk.

    Only structurally usable events survive: a transition to ``verified``
    with an evidence list and an optional actor. Anything else is dropped, so
    a malformed history can never be read as stronger proof than it is.
    """

    if not isinstance(history, list):
        return []
    events: list[dict] = []
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        to_state = entry.get("to_state")
        if to_state not in MEMORY_STATES:
            continue
        evidence = entry.get("evidence")
        actor = entry.get("actor")
        events.append(
            {
                "to_state": to_state,
                "evidence": (
                    [item for item in evidence if isinstance(item, str)]
                    if isinstance(evidence, list)
                    else []
                ),
                "actor": actor if isinstance(actor, str) else None,
            }
        )
    return events


def derive_verification(record: Mapping) -> dict:
    """Verification basis proved by the record's own recorded history.

    This is the authoritative derivation: it never reads the record's
    ``verification`` field, so a caller cannot grant itself an evidence-backed
    label by writing one.

    The result carries ``replayed_state``: the state the record's own event
    log ends in (``proposed`` when there is no history). A claimed recorded
    state that disagrees with that replay is not supported: the derivation
    reports ``unrecorded`` with the contradiction named, and every reader
    falls back to the state the history actually proves.
    """

    # ``state`` may be a derived value ("expired"), which says nothing about
    # how the record was reviewed; the recorded state is the authority.
    recorded_state = record.get("recorded_state") or record.get("state")
    events = _history_events(record.get("history"))
    verification = _verification(dict(record), events, recorded_state)
    replayed = events[-1]["to_state"] if events else "proposed"
    verification["replayed_state"] = replayed
    if (
        isinstance(recorded_state, str)
        and recorded_state in MEMORY_STATES
        and recorded_state != replayed
        and not (recorded_state == "proposed" and replayed == "proposed")
    ):
        verification["basis"] = "unrecorded"
        verification["downgraded"] = True
        reasons = verification.setdefault("reasons", [])
        reasons.append(
            f"the recorded review history ends in {replayed!r}, not the "
            f"claimed state {recorded_state!r}"
        )
        verification.setdefault(
            "detail",
            "this record's claimed review state is contradicted by its own "
            "recorded history; it is treated at the level that history "
            "proves, never higher.",
        )
    return verification


def resolve_trust(record: Mapping, *, label: str = "memory") -> dict:
    """The authoritative trust resolution for one memory-shaped record.

    Authority comes from recorded history, never from a field name. A
    supplied ``trust`` may agree with the derivation or honestly downgrade it;
    it may never raise it. A supplied ``verification`` whose basis the history
    does not support is refused rather than silently ignored, so a forged
    record fails loudly instead of being quietly rewritten.

    There is no exception for a truncated or absent history. A ``verified``
    state whose retained events do not include the verification transition is
    reported as ``unrecorded`` — uncertainty, never promotion — and the reason
    says so; an ``attested`` state whose history does not contain the
    acceptance transition is likewise downgraded to ``proposed``. Snapshots
    keep the whole review history so that a real store record can always prove
    what it recorded. This function validates internal
    consistency; it does not authenticate a record's producer, and a caller
    that fabricates a complete review transition is indistinguishable from a
    store that recorded one (documented limitation).
    """

    if not isinstance(record, Mapping):
        _fail(f"{label} must be a memory object")
    state = record.get("state")
    if not isinstance(state, str) or state not in MEMORY_STATES:
        _fail(
            f"{label} {record.get('memory_id')!r} has an unknown review state; "
            "expected one of: " + ", ".join(MEMORY_STATES)
        )
    recorded_state = record.get("recorded_state") or state
    if recorded_state not in MEMORY_STATES:
        _fail(
            f"{label} {record.get('memory_id')!r} has an unknown recorded "
            "state; expected one of: " + ", ".join(MEMORY_STATES)
        )
    verification = derive_verification(record)
    if state not in ACTIVE_STATES and recorded_state in ACTIVE_STATES:
        # Derived expiry: the record is still what it was recorded as, but it
        # is not applicable today, so it claims nothing current.
        trust = "proposed"
    else:
        trust = effective_trust(recorded_state, verification)
    declared_verification = record.get("verification")
    if declared_verification is not None:
        if not isinstance(declared_verification, Mapping):
            _fail(f"{label} {record.get('memory_id')!r} verification must be an object")
        declared_basis = declared_verification.get("basis")
        if declared_basis is not None and _BASIS_RANK.get(
            declared_basis, len(_BASIS_RANK)
        ) > _BASIS_RANK[verification["basis"]]:
            # A declared basis may repeat or honestly understate what history
            # proves, but it may never claim more. The derivation stands.
            _fail(
                f"{label} {record.get('memory_id')!r} declares verification "
                f"basis {declared_basis!r} but its recorded history "
                f"supports only {verification['basis']!r}; authority comes "
                "from recorded review events, not from a declared field"
            )
    declared_trust = record.get("trust")
    if declared_trust is not None:
        if (
            not isinstance(declared_trust, str)
            or declared_trust not in STATE_TRUST.values()
        ):
            _fail(
                f"{label} {record.get('memory_id')!r} has an unknown trust "
                "label; expected one of: "
                + ", ".join(sorted(set(STATE_TRUST.values())))
            )
        # A label may repeat the derivation or honestly understate it (a
        # reader that only shows part of the review). It may never raise it.
        if _TRUST_RANK[declared_trust] > _TRUST_RANK[trust]:
            _fail(
                f"{label} {record.get('memory_id')!r} trust label "
                f"{declared_trust!r} disagrees with its recorded history, "
                f"which supports only {trust!r}"
            )
    return {
        "state": state,
        "recorded_state": recorded_state,
        "trust": trust,
        "verification": verification,
    }


def effective_trust(state: str, verification: dict | None = None) -> str:
    """Trust label a reader must use for a recorded state.

    ``verified`` is only reported as ``verified`` when the verification is
    evidence-backed, and ``attested`` only as ``attested`` when the
    derivation found its acceptance transition; each unsupported claim falls
    back one tier with the reason stated. Called without a derivation (an
    exchange validator checking which labels a state *could* produce) the
    state's own tier applies. This is the single rule used by stores,
    retrieval, packs, and the exchange validator.
    """

    _choice(state, MEMORY_STATES, "memory state")
    if state == "verified":
        if (verification or {}).get("basis") == "evidence":
            return "verified"
        return "attested"
    if state == "attested":
        if (verification or {}).get("basis") == "unrecorded":
            return "proposed"
        return "attested"
    return STATE_TRUST.get(state, "proposed")


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
        "history": history,
        "history_entries": len(events),
    }
    snapshot["recorded_state"] = state
    # One derivation for every reader: the snapshot asks the same function the
    # retrieval, pack and exchange layers ask, so they cannot disagree.
    verification = derive_verification(snapshot)
    snapshot["verification"] = verification
    snapshot["trust"] = effective_trust(state, verification)
    validity = memory_validity(record, as_of=as_of)
    snapshot["validity"] = validity
    if state in ACTIVE_STATES and validity["reason"] == "expired":
        # Derived, never stored: the record itself is untouched and stays
        # current again when evaluated inside its window.
        snapshot["state"] = "expired"
        snapshot["trust"] = "proposed"
        snapshot["expired_reason"] = validity["detail"]
        snapshot["derived_expired"] = True
    return snapshot


def is_active(snapshot: dict) -> bool:
    return snapshot["state"] in ACTIVE_STATES


def is_current(snapshot: dict) -> bool:
    """Active *and* applicable on the date its validity was evaluated."""

    if not is_active(snapshot):
        return False
    validity = snapshot.get("validity")
    if validity is None:
        return memory_validity(snapshot)["current"]
    return bool(validity["current"])


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
        return self._parse_store(self.path.read_bytes(), size)

    def _parse_store(self, raw: bytes, size: int) -> dict:
        if len(raw) != size:
            size = len(raw)
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
        """Current snapshots keyed by memory id, plus read diagnostics.

        ``as_of`` selects the date validity is evaluated against; omitted it
        means today, so expiry applies to ordinary reads by default.
        """

        return _snapshots_from(self.read(), as_of=as_of)

    # -- writing -----------------------------------------------------------
    def _mutate(self, build):
        """Run one read-modify-append cycle under the store's advisory lock.

        The decision (create vs append, duplicate check, allowed transition)
        is made *inside* the lock, so two concurrent writers can never both
        decide to create the store or apply the same transition twice.

        The append itself never trusts the path twice: the file is opened
        without following symlinks, the opened descriptor is proven to be the
        same file that was just validated, the chain is re-checked from that
        descriptor, and the payload is written until every byte is accepted.
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
            if not exists:
                lines = build(False, None)
                if not lines:
                    return None
                payload = _encode_lines(lines)
                publish_text_atomic(
                    self.path,
                    payload.decode("utf-8"),
                    what="memory store",
                )
                return lines

            before = self.path.stat()
            if before.st_size > MAX_STORE_BYTES:
                raise StateError(
                    f"memory store {self.path} is larger than the supported "
                    f"{MAX_STORE_BYTES} bytes; split it into separate stores"
                )
            fd = _open_append_fd(self.path)
            try:
                _verify_same_file(fd, before, self.path)
                raw = _read_fd(fd, self.path, before)
                _reject_bad_tail(raw, self.path)
                data = self._parse_store(raw, len(raw))
                if data["damaged"]:
                    first = data["damaged"][0]
                    raise StateError(
                        f"memory store {self.path} has a damaged line "
                        f"({first['line']}: {first['reason']}); inspect it "
                        "with 'aperio mem-list' and repair the file before "
                        "adding memories"
                    )
                state = _snapshots_from(data, as_of=None)
                lines = build(True, state)
                if not lines:
                    return None
                previous = _last_line_hash(raw)
                payload = _encode_lines(lines, previous)
                if len(raw) + len(payload) > MAX_STORE_BYTES:
                    raise StateError(
                        f"memory store {self.path} would exceed the supported "
                        f"{MAX_STORE_BYTES} bytes; split it into separate stores"
                    )
                _append_fully(fd, payload, self.path)
            finally:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - defensive
                    pass
            return lines
        finally:
            release_lock(self.lock_path, token)

    def _plan_mutation(self, state: dict, memory_id: str, action: str) -> dict:
        snapshot = state["memories"].get(memory_id)
        if snapshot is None:
            raise StateError(f"no memory {memory_id} in {self.path}")
        recorded_state = snapshot.get("recorded_state", snapshot["state"])
        rule = ALLOWED_TRANSITIONS.get(action)
        if rule is None:
            raise ValidationError(
                "memory action must be one of: "
                + ", ".join(sorted(ALLOWED_TRANSITIONS))
            )
        if recorded_state not in rule["from"]:
            raise StateError(
                f"memory {memory_id} is {recorded_state}; "
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

        def build(exists: bool, state: dict | None) -> list[dict]:
            if exists:
                if state is None:  # pragma: no cover - defensive
                    state = self.snapshots()
                existing = state["memories"].get(memory_id)
                if existing is not None:
                    _reject_reclassified_duplicate(existing, normalized)
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

        def build(exists: bool, state: dict | None) -> list[dict]:
            if not exists or state is None:
                raise StateError(
                    f"no memory store at {self.path}; nothing to review"
                )
            snapshot = self._plan_mutation(state, memory_id, action)
            if action == "verify":
                _require_verification_basis(snapshot, actor, evidence_list)
            event = {
                "record": "transition",
                "memory_id": memory_id,
                "revision": snapshot["revision"] + 1,
                "at": _utc_now(),
                "actor": actor,
                "action": action,
                "from_state": snapshot.get("recorded_state", snapshot["state"]),
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


def _snapshots_from(data: dict, *, as_of: str | None) -> dict:
    """Derive current snapshots (replayed state + validity) from parsed data."""

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


def _reject_bad_tail(raw: bytes, path: Path) -> None:
    """Require the store to end on a complete line before appending.

    Appending past a broken chain would make every later line unreadable, so
    a store that ends mid-line (an interrupted write) is refused here. The
    deeper chain check uses :func:`_parse_store`'s damage report.
    """

    if raw and not raw.endswith(b"\n"):
        raise StateError(
            f"memory store {path} ends with an incomplete line (an "
            "interrupted write); inspect it with 'aperio mem-list' and "
            "repair the file before adding memories"
        )


def _encode_lines(lines: list[dict], previous: str | None = None) -> bytes:
    """Chain and serialize new lines, starting from ``previous``."""

    chained = []
    for line in lines:
        chained_line = {**line}
        chained_line[_CHAIN_FIELD] = line_sha256(chained_line, previous)
        chained.append(chained_line)
        previous = chained_line[_CHAIN_FIELD]
    return "".join(
        json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n"
        for line in chained
    ).encode("utf-8")


def _open_append_fd(path: Path) -> int:
    """Open the store for appending without following symlinks.

    ``O_NOFOLLOW`` closes the common swap-to-symlink race where available;
    :func:`_verify_same_file` then proves the descriptor is the file that was
    validated, which covers filesystems without that flag.
    """

    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as error:
        if error.errno in (getattr(os, "ENOENT", 2),):
            raise StateError(
                f"memory store {path} disappeared while it was being opened; "
                "nothing was written"
            ) from None
        if getattr(os, "O_NOFOLLOW", 0) and error.errno in (
            getattr(os, "ELOOP", None),
            getattr(os, "EMLINK", None),
        ):
            raise PathError(
                f"refusing to follow symlink at {path} for the memory store"
            ) from None
        raise PathError(
            f"cannot append to memory store {path}: {error.strerror or error}"
        ) from None


def _verify_same_file(fd: int, before: os.stat_result, path: Path) -> None:
    """Prove the opened descriptor is the file that was validated."""

    opened = os.fstat(fd)
    if not stat_module.S_ISREG(opened.st_mode):
        raise PathError(
            f"memory store {path} is not a regular file; refusing to append"
        )
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        raise PathError(
            f"memory store {path} changed while it was being opened; "
            "nothing was written. Re-run the command."
        )


def _read_fd(fd: int, path: Path, before: os.stat_result) -> bytes:
    """Read the whole store through the already-verified descriptor."""

    if before.st_size > MAX_STORE_BYTES:
        raise StateError(
            f"memory store {path} is larger than the supported "
            f"{MAX_STORE_BYTES} bytes; split it into separate stores"
        )
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = before.st_size
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != before.st_size:
        raise StateError(
            f"memory store {path} changed size while it was being read; "
            "nothing was written. Re-run the command."
        )
    return raw


def _append_fully(fd: int, payload: bytes, path: Path) -> None:
    """Write every byte of an event, or fail without claiming success.

    Partial writes are retried, a zero-byte write is a hard failure, and an
    I/O error is reported as damage rather than a completed transition. No
    crash or power-loss atomicity is promised: ``fsync`` flushes this file's
    contents, not the directory entry that creates it.
    """

    view = memoryview(payload)
    written = 0
    attempts = 0
    while written < len(payload):
        try:
            count = _os_write(fd, view[written:])
        except OSError as error:
            raise StateError(
                f"could not write memory store {path}: "
                f"{error.strerror or error}. The store may now contain a "
                "partial line; inspect it with 'aperio mem-list' and repair "
                "it before adding memories."
            ) from None
        if not count or count < 0:
            raise StateError(
                f"could not write memory store {path}: the filesystem "
                "accepted 0 bytes. The store may now contain a partial line; "
                "inspect it with 'aperio mem-list' and repair it before "
                "adding memories."
            )
        written += count
        attempts += 1
        if attempts > len(payload) + 8:  # pragma: no cover - defensive
            raise StateError(
                f"could not write memory store {path}: the write made no "
                "progress; inspect the store before adding memories."
            )
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        raise StateError(
            f"wrote the memory event but could not flush memory store {path} "
            "to disk; inspect it with 'aperio mem-list' before relying on it."
        ) from None


def _reject_reclassified_duplicate(existing: dict, record: dict) -> None:
    """Refuse to treat a different classification as the same memory.

    Identity is content-derived, so identical text is one memory. That must
    never silently keep an older, less private classification (or an older
    validity window) when the caller states a different one: the operator has
    to see the conflict instead of believing the new classification took
    effect.
    """

    differences = []
    if existing.get("sensitivity") != record.get("sensitivity"):
        differences.append(
            f"sensitivity is stored as {existing.get('sensitivity')!r} but was "
            f"proposed as {record.get('sensitivity')!r}"
        )
    for field in ("valid_from", "valid_until"):
        if existing.get(field) != record.get(field):
            differences.append(
                f"{field} is stored as {existing.get(field)!r} but was "
                f"proposed as {record.get(field)!r}"
            )
    if not differences:
        return
    raise ValidationError(
        "a memory with identical type, title, content, scope, tags, and "
        "evidence already exists, and its classification differs: "
        + "; ".join(differences)
        + ". Identity is derived from content, so re-proposing cannot change "
        "it. Retract the existing memory and propose the corrected record, or "
        "change the content so the two are distinguishable."
    )


def _require_verification_basis(
    snapshot: dict, actor: str | None, evidence: list[str]
) -> None:
    """Refuse a ``verify`` transition that cannot state its own support.

    Verification means *this claim is supported by evidence someone can
    check*. Without a recorded evidence reference and a named reviewer the
    command would only assert confidence, so it fails closed before anything
    is appended.
    """

    usable = [item for item in evidence if item.strip()]
    if not usable:
        raise ValidationError(
            "verification requires at least one evidence reference; pass "
            "--evidence with something a reviewer could check"
        )
    if not actor or not actor.strip():
        raise ValidationError(
            "verification requires a declared reviewer; pass --actor. An "
            "actor id is attribution, not authentication"
        )
    proposer = snapshot.get("actor")
    if proposer and proposer.strip().lower() == actor.strip().lower():
        raise ValidationError(
            "verification requires separation of duties: the declared "
            f"reviewer {actor!r} is the memory's declared proposer. Record "
            "the review under a different declared identifier; Aperio cannot "
            "authenticate identities, so this only enforces distinct "
            "declarations"
        )


def _last_line_hash(raw: bytes) -> str:
    """Chain hash of the final stored line, used to extend the chain."""


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
    "effective_trust",
    "is_active",
    "is_current",
    "memory_content_sha256",
    "memory_validity",
    "evaluate_on",
    "VERIFICATION_INDEPENDENCE_NOTE",
    "derive_verification",
    "resolve_trust",
    "memory_identity",
    "memory_revision_sha256",
    "memory_trust",
    "normalize_scope",
    "normalize_source",
    "validate_memory_record",
]
