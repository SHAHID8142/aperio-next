"""Legacy verification histories: recorded state vs effective trust.

Pre-gate stores could record a ``verify`` transition with no evidence, no
reviewer, or a reviewer identical to the proposer. History is never rewritten;
instead every reader derives an *effective* trust from what the event actually
proves, and reports exactly why a downgrade happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.errors import StateError, ValidationError
from aperio.memrecall import memory_context_items, recall_memories
from aperio.memstore import MemoryStore, line_sha256

BASE = {
    "type": "observation",
    "title": "Retention claim",
    "content": "Retention doubled after onboarding.",
    "scope": {"kind": "global"},
    "tags": ["retention"],
}


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def append_event(
    store: MemoryStore,
    *,
    action: str = "verify",
    from_state: str = "proposed",
    to_state: str = "verified",
    actor: str | None = None,
    evidence: list[str] | None = None,
    revision: int = 2,
) -> None:
    """Append a transition exactly as an older Aperio release could have."""

    raw = store.path.read_text(encoding="utf-8").splitlines()
    previous = json.loads(raw[-1])["chain_sha256"]
    event = {
        "record": "transition",
        "memory_id": json.loads(raw[1])["memory_id"],
        "revision": revision,
        "at": "2026-01-02T00:00:00Z",
        "actor": actor,
        "action": action,
        "from_state": from_state,
        "to_state": to_state,
        "note": None,
        "evidence": list(evidence or []),
        "reason": None,
    }
    event["chain_sha256"] = line_sha256(event, previous)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def propose(store: MemoryStore, actor: str | None = None) -> dict:
    record = dict(BASE)
    if actor is not None:
        record["actor"] = actor
    return store.propose(record)["memory"]


def test_legacy_verify_without_evidence_or_reviewer(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor=None, evidence=[])

    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["recorded_state"] == "verified"
    assert snapshot["state"] == "verified"
    assert snapshot["trust"] == "attested"
    verification = snapshot["verification"]
    assert verification["basis"] == "unrecorded"
    assert verification["downgraded"] is True
    assert any("evidence" in reason for reason in verification["reasons"])
    assert any("reviewer" in reason for reason in verification["reasons"])


def test_legacy_verify_with_evidence_but_no_reviewer_is_downgraded(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor=None, evidence=["vendor deck"])

    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    # A missing reviewer must never be reported as reviewer-attributed.
    assert snapshot["trust"] == "attested"
    assert snapshot["verification"]["basis"] == "unrecorded"
    assert snapshot["verification"]["reviewer"] is None
    assert snapshot["verification"]["evidence"] == ["vendor deck"]
    assert snapshot["verification"]["reasons"] == [
        "no reviewer identifier was recorded for the verification"
    ]


def test_legacy_verify_with_reviewer_but_no_evidence_is_downgraded(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor="reviewer", evidence=[])

    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["trust"] == "attested"
    assert snapshot["verification"]["basis"] == "unrecorded"
    assert snapshot["verification"]["reviewer"] == "reviewer"
    assert snapshot["verification"]["reasons"] == [
        "no supporting evidence reference was recorded"
    ]


def test_legacy_self_verification_is_not_evidence_backed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor="ANALYST", evidence=["own notes"])

    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["trust"] == "attested"
    assert snapshot["verification"]["basis"] == "unrecorded"
    assert snapshot["verification"]["reviewer_distinct_from_proposer"] is False
    assert snapshot["verification"]["independence"] == "same-identifier"
    assert "same declared identifier" in snapshot["verification"]["reasons"][0]


def test_legacy_verify_by_distinct_reviewer_stays_verified(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor="reviewer", evidence=["cohort report"])

    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["trust"] == "verified"
    assert snapshot["verification"]["basis"] == "evidence"
    assert snapshot["verification"]["evidence"] == ["cohort report"]
    assert snapshot["verification"]["reviewer"] == "reviewer"
    assert "downgraded" not in snapshot["verification"]


def test_newer_supported_verification_replaces_older_unsupported_one(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor=None, evidence=[])
    assert store.snapshots()["memories"][memory["memory_id"]]["trust"] == "attested"

    store.review(
        memory["memory_id"],
        "verify",
        actor="reviewer",
        evidence=["audited cohort report"],
    )
    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["revision"] == 3
    assert snapshot["trust"] == "verified"
    assert snapshot["verification"]["basis"] == "evidence"
    assert snapshot["verification"]["evidence"] == ["audited cohort report"]
    # The older event is still in the history, unmodified.
    assert [event["evidence"] for event in snapshot["history"]] == [
        [],
        ["audited cohort report"],
    ]


def test_retracted_and_superseded_records_claim_no_verification(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    store.review(
        memory["memory_id"], "verify", actor="reviewer", evidence=["report"]
    )
    retracted = store.review(
        memory["memory_id"], "retract", reason="source withdrawn"
    )
    assert retracted["state"] == "retracted"
    assert retracted["trust"] == "proposed"
    assert retracted["verification"]["basis"] == "not-verified"

    reinstated = store.review(memory["memory_id"], "reinstate")
    assert reinstated["state"] == "proposed"
    assert reinstated["trust"] == "proposed"
    assert reinstated["verification"]["basis"] == "not-verified"


def test_downgraded_legacy_verification_never_ranks_as_verified(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    append_event(store, actor=None, evidence=["vendor deck"])

    selection = recall_memories(
        store.snapshots()["memories"], objective="retention onboarding"
    )
    ranks = {m["memory_id"]: m["trust"] for m in selection["selected"]}
    assert ranks[memory["memory_id"]] == "attested"
    assert any(
        warning["code"] == "memory_verification_unrecorded"
        for warning in selection["warnings"]
    )

    items = memory_context_items(selection["selected"])
    by_id = {item.source_ref: item for item in items}
    assert by_id[f"memory:{memory['memory_id']}"].trust == "attested"
    assert (
        by_id[f"memory:{memory['memory_id']}"].detail["verification_basis"]
        == "unrecorded"
    )


def test_malformed_history_cannot_raise_trust(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst")
    # A verify event whose evidence is the wrong type is damage, not trust.
    append_event(store, actor="reviewer", evidence=[])
    raw = store.path.read_text(encoding="utf-8").splitlines()
    event = json.loads(raw[-1])
    del event["chain_sha256"]
    event["evidence"] = "not a list"
    event["chain_sha256"] = line_sha256(event, json.loads(raw[-2])["chain_sha256"])
    store.path.write_text(
        "\n".join([*raw[:-1], json.dumps(event, sort_keys=True)]) + "\n",
        encoding="utf-8",
    )

    state = store.snapshots()
    assert state["damaged"], "the malformed transition must be reported"
    snapshot = state["memories"][memory["memory_id"]]
    assert snapshot["state"] == "proposed"
    assert snapshot["trust"] == "proposed"
    # The store refuses to extend a damaged history at all.
    with pytest.raises((ValidationError, StateError)):
        store.review(
            memory["memory_id"], "verify", actor="reviewer", evidence=["x"]
        )


# ---------------------------------------------------------------------------
# exchange: recorded state vs effective trust must survive a round trip
# ---------------------------------------------------------------------------


def _round_trip(store: MemoryStore, tmp_path: Path, *, name: str = "b.mem"):
    from aperio.memportable import (
        export_memories,
        import_memories,
        validate_exchange_document,
    )

    document = export_memories(store, include_inactive=True)
    # Aperio's own output must survive its own validator, byte for byte.
    validate_exchange_document(json.loads(json.dumps(document)))
    target = MemoryStore(tmp_path / name)
    report = import_memories(target, document, actor="agent-b")
    return document, target, report


def test_downgraded_legacy_verification_round_trips(tmp_path: Path) -> None:
    store = store_at(tmp_path, "a.mem")
    memory = propose(store, actor="analyst")
    append_event(store, actor=None, evidence=["vendor deck"])

    document, target, report = _round_trip(store, tmp_path)
    entry = document["entries"][0]
    assert entry["memory"]["state"] == "verified"
    assert entry["memory"]["trust"] == "attested"
    assert entry["declared_state"] == "verified"
    assert report["imported"] == [memory["memory_id"]]
    assert report["unsupported_trust_claims"] == {
        memory["memory_id"]: (
            "the file declares 'verified' but its recorded history does not show "
            "evidence and a distinct reviewer; imported as proposed"
        )
    }

    imported = target.snapshots()["memories"][memory["memory_id"]]
    assert imported["state"] == "proposed"
    assert imported["trust"] == "proposed"
    assert imported["verification"]["basis"] == "not-verified"
    assert imported["declared_state"] == "verified"
    assert report["transfer_evidence"][memory["memory_id"]] == ["vendor deck"]


def test_supported_verification_round_trips_with_provenance(tmp_path: Path) -> None:
    store = store_at(tmp_path, "a.mem")
    memory = propose(store, actor="analyst")
    store.review(
        memory["memory_id"],
        "verify",
        actor="reviewer",
        evidence=["audited cohort report"],
    )

    document, target, report = _round_trip(store, tmp_path)
    entry = document["entries"][0]
    assert entry["memory"]["trust"] == "verified"
    assert entry["memory"]["history"][-1]["actor"] == "reviewer"
    assert entry["memory"]["history"][-1]["evidence"] == ["audited cohort report"]
    assert report["unsupported_trust_claims"] == {}

    imported = target.snapshots()["memories"][memory["memory_id"]]
    assert imported["state"] == "proposed"
    assert imported["source"]["kind"] == "import"
    # Record-level evidence is part of the identity and is not rewritten; the
    # review's supporting references travel in the report instead.
    assert imported["evidence"] == []
    assert report["transfer_evidence"][memory["memory_id"]] == [
        "audited cohort report"
    ]


@pytest.mark.parametrize(
    "state, trust",
    [
        ("proposed", "verified"),
        ("proposed", "attested"),
        ("attested", "verified"),
        ("retracted", "verified"),
        ("superseded", "attested"),
        ("verified", "bogus"),
    ],
)
def test_forged_state_and_trust_combinations_are_rejected(
    tmp_path: Path, state: str, trust: str
) -> None:
    from aperio.memportable import export_memories, validate_exchange_document

    store = store_at(tmp_path)
    propose(store)
    document = export_memories(store)
    document["entries"][0]["memory"]["state"] = state
    document["entries"][0]["memory"]["trust"] = trust
    from aperio.memportable import _export_digest

    document["content_sha256"] = _export_digest(document["entries"])
    with pytest.raises(ValidationError, match="trust label disagrees"):
        validate_exchange_document(document)


def test_legacy_v1_file_claiming_verified_still_imports_as_proposed(
    tmp_path: Path,
) -> None:
    """0.10.0 files may claim verified with no evidence: accept, never apply."""

    from aperio.memportable import (
        _export_digest,
        export_memories,
        import_memories,
        validate_exchange_document,
    )

    store = store_at(tmp_path, "a.mem")
    propose(store)
    document = export_memories(store)
    entry = document["entries"][0]
    entry["memory"]["state"] = "verified"
    entry["memory"]["trust"] = "verified"
    entry["declared_state"] = "verified"
    entry["memory"]["history"] = []
    document["content_sha256"] = _export_digest(document["entries"])

    validate_exchange_document(document)
    target = MemoryStore(tmp_path / "b.mem")
    report = import_memories(target, document)
    [memory_id] = report["imported"]
    imported = target.snapshots()["memories"][memory_id]
    assert imported["state"] == "proposed"
    assert imported["trust"] == "proposed"
    assert imported["verification"]["basis"] == "not-verified"
    assert memory_id in report["unsupported_trust_claims"]


def test_raw_record_without_snapshot_cannot_rank_as_verified(tmp_path: Path) -> None:
    """Defence in depth: ranking never trusts a bare ``verified`` state name."""

    store = store_at(tmp_path)
    memory = propose(store)
    raw = {
        "memory_id": memory["memory_id"],
        "type": "observation",
        "title": "Retention claim",
        "content": "Retention is 40 percent.",
        "scope": {"kind": "global"},
        "tags": ["retention"],
        "state": "verified",
        "sensitivity": "internal",
        "content_sha256": memory["content_sha256"],
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
    }
    selection = recall_memories(
        {memory["memory_id"]: raw}, objective="retention claim"
    )
    assert [m["trust"] for m in selection["selected"]] == ["attested"]
    assert selection["selected"][0]["verification_basis"] == "unrecorded"
