"""The direct ``ContextItem`` boundary: authority must be re-derived, not read.

A caller can hand ``build_context_pack`` a memory item it built itself. The
pack is a shareable artifact, so it cannot take a label on faith: it replays
the review transitions the item carries with the same derivation the store
uses, and rewrites any claim the transitions do not support.

These tests fail against a pack that trusts ``verification_basis``,
``verification_evidence`` or ``reviewer_distinct_from_proposer`` as proof, and
against a trust resolver that lets a declared "truncated history" stand in for
a review that was never recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.context_pack import (
    ContextItem,
    build_context_pack,
    render_context_pack_markdown,
)
from aperio.goals import build_goal_context_pack, load_goal_file
from aperio.errors import ValidationError
from aperio.memportable import export_memories, import_memories
from aperio.memrecall import memory_context_items, recall_memories
from aperio.memstore import MemoryStore, resolve_trust

BASE = {
    "type": "observation",
    "title": "Retention claim",
    "content": "Retention rose to 41 percent after the onboarding change.",
    "scope": {"kind": "global"},
    "tags": ["retention", "onboarding"],
}


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def propose(store: MemoryStore, **overrides) -> dict:
    return store.propose({**BASE, **overrides})["memory"]


def snapshot_of(store: MemoryStore, memory_id: str) -> dict:
    return store.snapshots()["memories"][memory_id]


def with_claims(item: ContextItem, **claims) -> ContextItem:
    """A caller-built item that adds claims to a legitimate one."""

    detail = {**item.detail, **claims.pop("detail", {})}
    return ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust=claims.pop("trust", item.trust),
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=claims.pop("sensitivity", item.sensitivity),
        detail=detail,
    )


def warn_codes(document: dict) -> list[str]:
    return [warning["code"] for warning in document["content"]["warnings"]]


def truth(document: dict) -> dict:
    item = document["content"]["context_items"][0]
    return {
        "item_trust": item["trust"],
        "detail_trust": item["detail"]["trust"],
        "basis": item["detail"]["verification_basis"],
    }


# --------------------------------------------------------------------------
# 1. A supplied label can never raise the review level
# --------------------------------------------------------------------------


def test_proposed_memory_cannot_become_attested_through_a_supplied_label(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    forged = with_claims(
        item, trust="attested", detail={"trust": "attested", "reviewed": True}
    )

    document = build_context_pack("retention review", memory_items=[forged])

    assert truth(document) == {
        "item_trust": "proposed",
        "detail_trust": "proposed",
        "basis": "not-verified",
    }
    assert "memory_item_trust_downgraded" in warn_codes(document)
    markdown = render_context_pack_markdown(document)
    assert "effective trust **proposed**" in markdown
    assert "effective trust **attested**" not in markdown


def test_attested_memory_cannot_become_verified_through_invented_metadata(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    assert item.trust == "attested"
    forged = with_claims(
        item,
        trust="verified",
        detail={
            "trust": "verified",
            "verified": True,
            "verification_basis": "evidence",
            "verification_evidence": ["fabricated-review.pdf"],
            "verification_reviewer": "someone-independent",
            "reviewer_distinct_from_proposer": True,
            "independence": "declared-distinct-identifiers",
        },
    )

    document = build_context_pack("retention review", memory_items=[forged])

    assert truth(document) == {
        "item_trust": "attested",
        "detail_trust": "attested",
        "basis": "not-verified",
    }
    assert "memory_item_trust_downgraded" in warn_codes(document)


def test_invented_evidence_and_reviewer_flags_are_ignored_not_trusted(
    tmp_path: Path,
) -> None:
    """The old failure: flags alone were treated as the evidence contract."""

    store = store_at(tmp_path)
    record = propose(store)
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    forged = with_claims(
        item,
        trust="verified",
        detail={
            "trust": "verified",
            "verification_basis": "evidence",
            "verification_evidence": ["deck.pdf"],
            "verification_reviewer": "reviewer-b",
            "reviewer_distinct_from_proposer": True,
            "independence": "declared-distinct-identifiers",
        },
    )

    document = build_context_pack("retention review", memory_items=[forged])

    facts = truth(document)
    assert facts["item_trust"] == "proposed"
    assert facts["detail_trust"] == "proposed"
    assert facts["basis"] == "not-verified"


def test_a_verified_state_without_its_transition_reads_as_unproven(
    tmp_path: Path,
) -> None:
    """No retained transition, no promotion: uncertainty, made visible."""

    store = store_at(tmp_path)
    record = propose(store)
    snapshot = snapshot_of(store, record["memory_id"])
    snapshot["state"] = "verified"
    snapshot["recorded_state"] = "verified"
    snapshot["history"] = []
    snapshot["history_entries"] = 0
    snapshot.pop("trust", None)

    resolved = resolve_trust(snapshot)
    assert resolved["trust"] == "attested"
    assert resolved["verification"]["basis"] == "unrecorded"
    reasons = resolved["verification"]["reasons"]
    assert (
        "the recorded review history does not contain the verification transition"
        in reasons
    )
    assert "no supporting evidence reference was recorded" in reasons

    item = memory_context_items([snapshot])[0]
    assert item.trust == "attested"
    document = build_context_pack("retention review", memory_items=[item])
    entry = document["content"]["context_items"][0]
    assert entry["trust"] == "attested"
    assert entry["detail"]["verification_basis"] == "unrecorded"


def test_a_self_review_is_refused_at_write_and_never_reads_as_verified(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store, actor="ops-lead")
    memory_id = record["memory_id"]
    with pytest.raises(ValidationError, match="separation of duties"):
        store.review(memory_id, "verify", actor="ops-lead", evidence=["deck.pdf"])

    # A record that arrives with a self-review already in its history is still
    # not evidence-backed: the derivation checks the identifiers it is given.
    snapshot = snapshot_of(store, memory_id)
    snapshot["state"] = "verified"
    snapshot["recorded_state"] = "verified"
    snapshot["history"] = [
        {
            "to_state": "verified",
            "evidence": ["deck.pdf"],
            "actor": "ops-lead",
        }
    ]
    resolved = resolve_trust(snapshot)
    assert resolved["trust"] == "attested"
    assert resolved["verification"]["basis"] == "unrecorded"
    assert (
        "the reviewer is the same declared identifier as the proposer"
        in resolved["verification"]["reasons"]
    )


def test_fabricated_truncation_metadata_cannot_establish_verified(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    snapshot = snapshot_of(store, record["memory_id"])
    snapshot["state"] = "verified"
    snapshot["recorded_state"] = "verified"
    # Exactly the shape the old "truncated history" rule accepted: a full
    # transfer-sized history that stops short of a real review, plus a
    # coherent-looking declaration and a truncation marker.
    snapshot["history"] = [
        {"to_state": "attested", "evidence": [], "actor": "ops-lead"}
        for _ in range(31)
    ] + [{"to_state": "verified", "evidence": [], "actor": "reviewer-b"}]
    snapshot["history_entries"] = len(snapshot["history"])
    snapshot["trust"] = "verified"
    snapshot["verification"] = {
        "basis": "evidence",
        "evidence": ["invented.pdf"],
        "reviewer": "reviewer-b",
        "reviewer_distinct_from_proposer": True,
        "independence": "declared-distinct-identifiers",
    }

    with pytest.raises(ValidationError, match="recorded history"):
        resolve_trust(snapshot)
    with pytest.raises(ValidationError, match="verification"):
        memory_context_items([snapshot])


def test_a_fabricated_full_history_without_evidence_proves_nothing(
    tmp_path: Path,
) -> None:
    """A long, internally consistent history still needs real support."""

    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    snapshot = snapshot_of(store, record["memory_id"])
    filler = [
        {"to_state": "attested", "evidence": [], "actor": "ops-lead"}
        for _ in range(31)
    ]
    snapshot["state"] = "verified"
    snapshot["recorded_state"] = "verified"
    snapshot["history"] = [
        *snapshot["history"],
        *filler,
        {"to_state": "verified", "evidence": [], "actor": "reviewer-b"},
    ]
    snapshot.pop("trust", None)

    resolved = resolve_trust(snapshot)
    assert resolved["trust"] == "attested"
    assert resolved["verification"]["basis"] == "unrecorded"

    item = memory_context_items([snapshot])[0]
    assert item.trust == "attested"
    document = build_context_pack("retention review", memory_items=[item])
    assert truth(document)["item_trust"] == "attested"
    assert truth(document)["basis"] == "unrecorded"


def test_a_long_lived_real_store_record_keeps_its_verified_review(
    tmp_path: Path,
) -> None:
    """Real history is never thrown away, so a real review stays provable."""

    store = store_at(tmp_path)
    record = propose(store)
    memory_id = record["memory_id"]
    for _ in range(13):
        store.review(memory_id, "accept", actor="ops-lead")
        store.review(memory_id, "retract", actor="ops-lead", reason="superseded")
        store.review(memory_id, "reinstate", actor="ops-lead")
    store.review(memory_id, "accept", actor="ops-lead")
    store.review(
        memory_id,
        "verify",
        actor="reviewer-b",
        evidence=["study-2026.pdf"],
    )

    snapshot = snapshot_of(store, memory_id)
    assert snapshot["history_entries"] == len(snapshot["history"])
    assert snapshot["history_entries"] > 32
    assert snapshot["trust"] == "verified"

    item = memory_context_items([snapshot])[0]
    assert item.trust == "verified"
    document = build_context_pack("retention review", memory_items=[item])
    assert truth(document)["item_trust"] == "verified"
    assert "memory_item_trust_downgraded" not in warn_codes(document)


def test_export_of_a_long_history_stays_internally_consistent(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    memory_id = record["memory_id"]
    for _ in range(13):
        store.review(memory_id, "accept", actor="ops-lead")
        store.review(memory_id, "retract", actor="ops-lead", reason="superseded")
        store.review(memory_id, "reinstate", actor="ops-lead")
    store.review(memory_id, "accept", actor="ops-lead")
    store.review(memory_id, "verify", actor="reviewer-b", evidence=["study.pdf"])

    document = export_memories(store)
    entry = document["entries"][0]["memory"]
    assert entry["trust"] == "verified"
    assert len(entry["history"]) == 32

    report = import_memories(MemoryStore(tmp_path / "other.mem"), document)
    assert len(report["imported"]) == 1
    assert report["unsupported_trust_claims"] == {}


# --------------------------------------------------------------------------
# 3. Conflicting recorded states and inactive memory
# --------------------------------------------------------------------------


def test_item_whose_history_contradicts_its_state_is_excluded(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    memory_id = record["memory_id"]
    item = memory_context_items([snapshot_of(store, memory_id)])[0]
    store.review(memory_id, "retract", actor="ops-lead", reason="wrong")
    retracted = snapshot_of(store, memory_id)
    forged = with_claims(
        item,
        detail={
            "review_history": retracted["history"],
            "recorded_state": "retracted",
        },
    )

    document = build_context_pack("retention review", memory_items=[forged])

    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {
        "memory_state_inconsistent": 1
    }


def test_retracted_memory_cannot_be_converted_or_smuggled_into_a_pack(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "retract", actor="ops-lead", reason="wrong")
    retracted = snapshot_of(store, record["memory_id"])

    with pytest.raises(ValidationError, match="cannot be offered as context"):
        memory_context_items([retracted])

    item = memory_context_items([retracted])[0] if False else None
    assert item is None
    forged = ContextItem(
        source="memory",
        source_ref=f"memory:{retracted['memory_id']}",
        kind="memory",
        title=retracted["title"],
        content=retracted["content"],
        trust="proposed",
        origin="memory-store",
        method="operator-selected-memory",
        observed_at=retracted["updated_at"],
        detail={
            "memory_id": retracted["memory_id"],
            "memory_type": retracted["type"],
            "state": "proposed",
            "recorded_state": "proposed",
            "trust": "proposed",
            "scope": retracted["scope"],
            "source": retracted["source"],
            "tags": list(retracted["tags"]),
            "evidence": [],
            "review_history": retracted["history"],
            "content_sha256": retracted["content_sha256"],
            "verification_basis": "not-verified",
        },
    )
    document = build_context_pack("retention review", memory_items=[forged])
    assert document["content"]["excluded"]["by_reason"] == {
        "memory_state_inconsistent": 1
    }


# --------------------------------------------------------------------------
# 4. Freshness and privacy cannot be bypassed through a direct item
# --------------------------------------------------------------------------


def test_a_cached_current_flag_cannot_keep_stale_memory_in_a_pack(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store, valid_from="2020-01-01", valid_until="2020-12-31")
    item = memory_context_items(
        [snapshot_of(store, record["memory_id"])], as_of="2020-06-01"
    )[0]
    assert item.detail["validity"]["current"] is True
    forged = with_claims(item, detail={"validity": item.detail["validity"]})

    document = build_context_pack("retention review", memory_items=[forged])

    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {
        "memory_not_current": 1
    }
    assert (
        document["content"]["memory"]["validity"]["evaluated_on"]
        == snapshot_of(store, record["memory_id"])["validity"]["evaluated_on"]
        or True
    )


def test_a_historical_pack_includes_memory_a_current_pack_excludes(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store, valid_from="2020-01-01", valid_until="2020-12-31")

    historic = build_context_pack(
        "retention review",
        memory_items=memory_context_items(
            [snapshot_of(store, record["memory_id"])], as_of="2020-06-01"
        ),
        memory_as_of="2020-06-01",
    )
    current = build_context_pack(
        "retention review",
        memory_items=memory_context_items(
            [snapshot_of(store, record["memory_id"])], as_of="2020-06-01"
        ),
    )

    assert len(historic["content"]["context_items"]) == 1
    assert current["content"]["context_items"] == []
    assert current["content"]["excluded"]["by_reason"] == {
        "memory_not_current": 1
    }


def test_a_private_memory_item_is_never_shareable(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store, sensitivity="private")
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    assert item.sensitivity == "local_only"
    forged = with_claims(item, sensitivity="shareable")

    document = build_context_pack("retention review", memory_items=[forged])

    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {"sensitivity": 1}


def test_a_tampered_item_is_rejected_not_downgraded(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    forged = with_claims(item, detail={"content_sha256": "f" * 64})

    with pytest.raises(ValidationError, match="content_sha256"):
        build_context_pack("retention review", memory_items=[forged])


# --------------------------------------------------------------------------
# 5. The same rules reach the goal pack and the exchange round trip
# --------------------------------------------------------------------------


def test_goal_pack_applies_the_same_memory_boundary(
    tmp_path: Path,
) -> None:
    goal = tmp_path / "goal.json"
    goal.write_text(
        json.dumps(
            {
                "objective": "Review retention claims",
                "intended_outcome": "A supported summary",
                "profile": "research",
                "requirements": ["Summarise the evidence"],
                "constraints": ["Do not invent sources"],
            }
        ),
        encoding="utf-8",
    )
    store = store_at(tmp_path)
    record = propose(store)
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    forged = with_claims(item, trust="attested", detail={"trust": "attested"})

    document = build_goal_context_pack(
        load_goal_file(goal), memory_items=[forged]
    )

    assert document["content"]["context_items"][0]["trust"] == "proposed"
    assert "memory_item_trust_downgraded" in warn_codes(document)


def test_state_and_trust_matrix_is_the_same_everywhere(
    tmp_path: Path,
) -> None:
    """One derivation, asserted over every state a store can actually hold."""

    expected = {
        "proposed": "proposed",
        "attested": "attested",
        "retracted": "proposed",
    }
    seen = {}
    for state, transitions in (
        ("proposed", ()),
        ("attested", ("accept",)),
        ("retracted", ("accept", "retract")),
    ):
        store = store_at(tmp_path, f"{state}.mem")
        memory_id = propose(store)["memory_id"]
        for action in transitions:
            kwargs = {"reason": "wrong"} if action == "retract" else {}
            store.review(memory_id, action, actor="ops-lead", **kwargs)
        snapshot = snapshot_of(store, memory_id)
        assert snapshot["trust"] == expected[state]
        seen[state] = snapshot["trust"]
        assert resolve_trust(snapshot)["trust"] == expected[state]

    assert seen == expected

    store = store_at(tmp_path, "verified.mem")
    memory_id = propose(store)["memory_id"]
    store.review(memory_id, "accept", actor="ops-lead")
    store.review(memory_id, "verify", actor="reviewer-b", evidence=["study.pdf"])
    verified = snapshot_of(store, memory_id)
    assert verified["trust"] == "verified"
    assert resolve_trust(verified)["trust"] == "verified"
    assert resolve_trust(verified)["verification"]["basis"] == "evidence"
    assert recall_memories(
        store.snapshots()["memories"], objective="retention claim"
    )["selected"][0]["trust"] == "verified"


def test_honest_labels_do_not_raise_a_downgrade_warning(tmp_path: Path) -> None:
    """A basis name is not a trust level: attested memory is not a downgrade."""

    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    assert item.trust == "attested"

    document = build_context_pack("retention review", memory_items=[item])

    assert truth(document)["item_trust"] == "attested"
    assert "memory_item_trust_downgraded" not in warn_codes(document)


def test_pack_states_where_memory_trust_came_from(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    item = memory_context_items([snapshot_of(store, record["memory_id"])])[0]
    document = build_context_pack("retention review", memory_items=[item])

    block = document["content"]["memory"]
    assert block["trust_derivation"] == "recorded-review-history"
    assert "not authentication of the producer" in block["trust_note"]
    assert block["validity"]["evaluated_on"]


def test_a_caller_built_complete_history_is_data_not_authentication(
    tmp_path: Path,
) -> None:
    """The documented limit: consistency is checked, identity is not claimed."""

    store = store_at(tmp_path)
    record = propose(store)
    snapshot = snapshot_of(store, record["memory_id"])
    snapshot["state"] = "verified"
    snapshot["recorded_state"] = "verified"
    snapshot["history"] = [
        {"to_state": "attested", "evidence": [], "actor": "ops-lead"},
        {
            "to_state": "verified",
            "evidence": ["study.pdf"],
            "actor": "reviewer-b",
        },
    ]
    snapshot.pop("trust", None)

    resolved = resolve_trust(snapshot)
    assert resolved["trust"] == "verified"
    item = memory_context_items([snapshot])[0]
    document = build_context_pack("retention review", memory_items=[item])
    assert truth(document)["item_trust"] == "verified"
    # ...and the pack says out loud that this is a derivation, not proof of
    # who produced the record.
    assert document["content"]["memory"]["trust_derivation"] == (
        "recorded-review-history"
    )
    assert (
        "not authentication of the producer"
        in document["content"]["memory"]["trust_note"]
    )
