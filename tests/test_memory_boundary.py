"""Memory trust-boundary matrix: every entry point, every input form.

The M3 memory layer has several entry points — store snapshots, the recall
API, context-item conversion, context packs, and portable exchange — and each
one can be handed data by a caller. These tests pin the rule that *authority
comes from recorded history, never from a field name*: no supplied ``trust``
string, and no self-declared ``verification.basis``, can grant verification
on its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aperio.context_pack import ContextItem, build_context_pack
from aperio.errors import ValidationError
from aperio.memportable import (
    export_memories,
    import_memories,
    validate_exchange_document,
)
from aperio.memrecall import memory_context_items, recall_memories
from aperio.memstore import (
    MemoryStore,
    canonical_memory_bytes,
    memory_content_sha256,
    memory_identity,
)

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
    record = {**BASE, **overrides}
    return store.propose(record)["memory"]


def raw_record(**overrides) -> dict:
    """A caller-built record: correct shape, no store, no history."""

    record = {
        "memory_id": "MEM-" + "0" * 12,
        "type": "observation",
        "title": BASE["title"],
        "content": BASE["content"],
        "scope": {"kind": "global"},
        "tags": list(BASE["tags"]),
        "sensitivity": "internal",
        "state": "proposed",
        "source": {"kind": "agent", "ref": None, "note": None},
        "evidence": [],
        "supersedes": None,
        "valid_from": None,
        "valid_until": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    record["content_sha256"] = hashlib.sha256(
        canonical_memory_bytes(record)
    ).hexdigest()
    record["memory_id"] = "MEM-" + record["content_sha256"][:12]
    record.update(overrides)
    if record["state"] == "attested" and "history" not in overrides:
        # A claimed state must be derivable from the history the record
        # carries: attested exists only through a recorded accept transition.
        record["history"] = [
            {
                "memory_id": record["memory_id"],
                "revision": 2,
                "at": record["updated_at"],
                "actor": "ops-lead",
                "action": "accept",
                "from_state": "proposed",
                "to_state": "attested",
                "note": None,
                "evidence": [],
                "reason": None,
            }
        ]
    return record


# --------------------------------------------------------------------------
# 1. No supplied trust string may grant verification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["proposed", "attested"])
def test_supplied_verified_trust_on_an_unverified_state_is_refused(state) -> None:
    record = raw_record(state=state, trust="verified")
    with pytest.raises(ValidationError, match="trust label"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_supplied_verified_trust_with_no_state_at_all_is_refused() -> None:
    record = raw_record()
    record["state"] = "verified"  # claimed, never recorded
    record["trust"] = "verified"
    with pytest.raises(ValidationError, match="recorded history"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_self_declared_evidence_basis_without_history_is_refused() -> None:
    """A record cannot manufacture proof by labelling its own verification."""

    record = raw_record(state="verified", trust="verified")
    record["verification"] = {
        "basis": "evidence",
        "evidence": ["deck.pdf"],
        "reviewer": "reviewer-b",
        "reviewer_distinct_from_proposer": True,
        "independence": "declared-distinct-identifiers",
    }
    with pytest.raises(ValidationError, match="recorded history"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_unknown_trust_label_is_refused() -> None:
    record = raw_record(state="proposed", trust="authenticated")
    with pytest.raises(ValidationError, match="trust"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_unknown_state_is_refused() -> None:
    record = raw_record(state="blessed")
    with pytest.raises(ValidationError, match="state"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_malformed_verification_object_is_refused() -> None:
    record = raw_record(state="verified")
    record["verification"] = "trust me"
    with pytest.raises(ValidationError, match="verification"):
        recall_memories({record["memory_id"]: record}, objective="retention claim")


def test_verified_state_without_any_support_reads_as_attested() -> None:
    """No history, no claim: the record is data, not a verified fact."""

    record = raw_record(state="verified")
    selection = recall_memories(
        {record["memory_id"]: record}, objective="retention claim"
    )
    selected = selection["selected"][0]
    assert selected["trust"] == "attested"
    assert selected["verification_basis"] == "unrecorded"


def test_verified_claim_with_weak_history_stays_attested(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store)
    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    forged = dict(snapshot)
    forged["verification"] = {"basis": "evidence", "evidence": ["x"]}
    forged["trust"] = "verified"
    forged["state"] = "verified"
    with pytest.raises(ValidationError, match="recorded history"):
        recall_memories({memory["memory_id"]: forged}, objective="retention")


def test_store_snapshot_with_supported_verification_keeps_verified(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst-a")
    store.review(
        memory["memory_id"], "verify", actor="reviewer-b", evidence=["study.pdf"]
    )
    snapshot = store.snapshots()["memories"][memory["memory_id"]]
    assert snapshot["trust"] == "verified"

    selection = recall_memories(
        store.snapshots()["memories"], objective="retention claim"
    )
    assert selection["selected"][0]["trust"] == "verified"
    assert selection["selected"][0]["verification_basis"] == "evidence"


def test_legacy_downgraded_snapshot_stays_downgraded(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = propose(store, actor="analyst-a")
    store.review(memory["memory_id"], "accept", actor="lead")
    path = store.path
    previous = json.loads(path.read_text().splitlines()[-1])["chain_sha256"]
    from aperio.memstore import line_sha256

    transition = {
        "record": "transition",
        "memory_id": memory["memory_id"],
        "revision": 3,
        "at": "2026-01-02T00:00:00Z",
        "actor": "analyst-a",  # same declared identifier as the proposer
        "action": "verify",
        "from_state": "attested",
        "to_state": "verified",
        "note": None,
        "evidence": ["deck.pdf"],
        "reason": None,
    }
    transition["chain_sha256"] = line_sha256(transition, previous)
    with path.open("a") as handle:
        handle.write(json.dumps(transition, sort_keys=True) + "\n")

    snapshot = MemoryStore(path).snapshots()["memories"][memory["memory_id"]]
    assert snapshot["state"] == "verified"
    assert snapshot["trust"] == "attested"
    selection = recall_memories(
        MemoryStore(path).snapshots()["memories"], objective="retention claim"
    )
    assert selection["selected"][0]["trust"] == "attested"


# --------------------------------------------------------------------------
# 2. Context-item conversion shares the same contract
# --------------------------------------------------------------------------


def test_context_item_trust_is_derived_not_copied() -> None:
    record = raw_record(state="verified", trust=None)
    record.pop("trust", None)
    items = memory_context_items([record])
    assert items[0].trust == "attested"
    assert items[0].detail["verification_basis"] == "unrecorded"


def test_context_item_refuses_an_inconsistent_trust_claim() -> None:
    record = raw_record(state="proposed", trust="verified")
    with pytest.raises(ValidationError, match="trust label"):
        memory_context_items([record])


def test_context_item_carries_a_fresh_validity_evaluation() -> None:
    record = raw_record(valid_until="2026-01-31")
    record["validity"] = {  # a stale value from an earlier read
        "evaluated_on": "2026-01-01",
        "current": True,
        "reason": None,
    }
    items = memory_context_items([record], as_of="2026-06-01")
    assert items[0].detail["validity"]["evaluated_on"] == "2026-06-01"
    assert items[0].detail["validity"]["current"] is False
    assert items[0].detail["validity"]["reason"] == "expired"


def test_context_item_private_sensitivity_stays_local_only() -> None:
    record = raw_record(sensitivity="private")
    items = memory_context_items([record])
    assert items[0].sensitivity == "local_only"


# --------------------------------------------------------------------------
# 3. Context packs re-check the trust and freshness boundary
# --------------------------------------------------------------------------


def _memory_item(**detail_overrides) -> ContextItem:
    record = raw_record(state="attested")
    item = memory_context_items([record])[0]
    item.detail.update(detail_overrides)
    return item


def test_pack_downgrades_an_item_that_claims_verified_without_support() -> None:
    item = _memory_item()
    forged = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust="verified",
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail={**item.detail, "verification_basis": "unrecorded"},
    )
    document = build_context_pack("retention review", memory_items=[forged])
    labels = [
        item["trust"] for item in document["content"]["context_items"]
    ]
    assert labels == ["attested"]
    codes = [warning["code"] for warning in document["content"]["warnings"]]
    assert "memory_item_trust_downgraded" in codes


def test_pack_rejects_a_private_item_labelled_shareable() -> None:
    item = _memory_item()
    leaked = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust=item.trust,
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity="shareable",  # the lie
        detail={**item.detail, "sensitivity": "private"},
    )
    document = build_context_pack("retention review", memory_items=[leaked])
    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {"sensitivity": 1}


def test_pack_rejects_an_unknown_memory_trust_label() -> None:
    item = _memory_item()
    forged = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust="authenticated",
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail=item.detail,
    )
    with pytest.raises(ValidationError, match="trust"):
        build_context_pack("retention review", memory_items=[forged])


def test_pack_rejects_mismatched_memory_identity() -> None:
    item = _memory_item()
    forged = ContextItem(
        source=item.source,
        source_ref="memory:MEM-000000000000",
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust=item.trust,
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail=item.detail,
    )
    with pytest.raises(ValidationError, match="memory_id"):
        build_context_pack("retention review", memory_items=[forged])


def test_pack_rejects_a_tampered_content_hash() -> None:
    item = _memory_item()
    forged = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content + " Changed after selection.",
        trust=item.trust,
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail=item.detail,
    )
    with pytest.raises(ValidationError, match="content_sha256"):
        build_context_pack("retention review", memory_items=[forged])


def test_pack_re_evaluates_validity_instead_of_trusting_a_stale_flag() -> None:
    record = raw_record(valid_until="2026-01-31")
    item = memory_context_items([record], as_of="2026-01-15")[0]
    assert item.detail["validity"]["current"] is True  # was current then

    document = build_context_pack(
        "retention review", memory_items=[item], memory_as_of="2026-06-01"
    )
    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {"memory_not_current": 1}
    assert document["content"]["memory"]["validity"]["evaluated_on"] == "2026-06-01"


def test_pack_records_one_evaluation_date_and_warns_on_a_mismatch() -> None:
    record = raw_record()
    item = memory_context_items([record], as_of="2026-01-15")[0]
    document = build_context_pack(
        "retention review", memory_items=[item], memory_as_of="2026-06-01"
    )
    codes = [warning["code"] for warning in document["content"]["warnings"]]
    assert "memory_validity_reevaluated" in codes
    assert document["content"]["memory"]["validity"]["evaluated_on"] == "2026-06-01"
    assert [
        item["detail"]["validity"]["evaluated_on"]
        for item in document["content"]["context_items"]
    ] == ["2026-06-01"]


@pytest.mark.parametrize(
    "day, expected",
    [("2026-01-31", 1), ("2026-02-01", 0)],
)
def test_pack_boundary_dates_are_inclusive(day, expected) -> None:
    record = raw_record(valid_until="2026-01-31")
    item = memory_context_items([record], as_of=day)[0]
    document = build_context_pack(
        "retention review", memory_items=[item], memory_as_of=day
    )
    assert len(document["content"]["context_items"]) == expected


def test_pack_rejects_an_invalid_memory_evaluation_date() -> None:
    item = memory_context_items([raw_record()])[0]
    with pytest.raises(ValidationError, match="ISO date"):
        build_context_pack(
            "retention review", memory_items=[item], memory_as_of="2026-6-1"
        )


def test_pack_rejects_an_operator_executed_as_a_memory_item() -> None:
    item = memory_context_items([raw_record()])[0]
    with pytest.raises(ValidationError, match="source 'memory'"):
        build_context_pack(
            "retention review",
            memory_items=[
                ContextItem(
                    source="operator",
                    source_ref=item.source_ref,
                    kind=item.kind,
                    title=item.title,
                    content=item.content,
                    trust=item.trust,
                    origin=item.origin,
                    method=item.method,
                    observed_at=item.observed_at,
                    sensitivity=item.sensitivity,
                    detail=item.detail,
                )
            ],
        )


# --------------------------------------------------------------------------
# 4. Portable provenance survives repeated transfer
# --------------------------------------------------------------------------


def test_import_keeps_the_original_declared_source(tmp_path: Path) -> None:
    origin = store_at(tmp_path, "a.mem")
    memory = propose(
        origin,
        source={
            "kind": "agent",
            "ref": "brandbook-v3.pdf",
            "note": "tone guide, section 4",
        },
    )
    target = store_at(tmp_path, "b.mem")
    document = export_memories(origin, include_inactive=True)
    import_memories(target, document, actor="agent-b")

    imported = target.snapshots()["memories"][memory["memory_id"]]
    assert imported["source"]["kind"] == "import"
    assert imported["source"]["ref"] == "brandbook-v3.pdf"
    note = imported["source"]["note"]
    assert "brandbook-v3.pdf" in note
    assert "section 4" in note
    assert "agent" in note
    assert imported["state"] == "proposed"


def test_provenance_chain_survives_three_stores(tmp_path: Path) -> None:
    first = store_at(tmp_path, "a.mem")
    memory = propose(
        first,
        type="requirement",
        source={"kind": "operator", "ref": "brief-2026.md", "note": "signed off"},
    )
    second = store_at(tmp_path, "b.mem")
    import_memories(second, export_memories(first, include_inactive=True), actor="b")

    third = store_at(tmp_path, "c.mem")
    report = import_memories(
        third, export_memories(second, include_inactive=True), actor="c"
    )
    assert report["imported"] == [memory["memory_id"]]

    final = third.snapshots()["memories"][memory["memory_id"]]
    assert final["memory_id"] == memory["memory_id"]
    assert final["source"]["kind"] == "import"
    assert "brief-2026.md" in final["source"]["note"]
    assert "signed off" in final["source"]["note"]
    assert final["scope"] == memory["scope"]
    assert final["sensitivity"] == memory["sensitivity"]
    assert final["state"] == "proposed"
    assert final["verification"]["basis"] == "not-verified"


def test_import_report_names_the_declared_origin(tmp_path: Path) -> None:
    origin = store_at(tmp_path, "a.mem")
    memory = propose(
        origin, source={"kind": "agent", "ref": "study-42.md", "note": "raw notes"}
    )
    target = store_at(tmp_path, "b.mem")
    report = import_memories(
        target, export_memories(origin, include_inactive=True), actor="b"
    )
    provenance = report["transfer_provenance"][memory["memory_id"]]
    assert provenance["declared_source"]["kind"] == "agent"
    assert provenance["declared_source"]["ref"] == "study-42.md"
    assert provenance["immediate"]["kind"] == "import"


# --------------------------------------------------------------------------
# 5. Adversarial end to end: forged file cannot promote anything
# --------------------------------------------------------------------------


def test_forged_file_with_fake_history_imports_as_an_untrusted_proposal(
    tmp_path: Path,
) -> None:
    origin = store_at(tmp_path, "a.mem")
    memory = propose(origin)
    document = export_memories(origin, include_inactive=True)
    entry = document["entries"][0]
    entry["memory"]["state"] = "verified"
    entry["memory"]["trust"] = "verified"
    entry["memory"]["history"] = [
        {
            "memory_id": memory["memory_id"],
            "revision": 2,
            "at": "2026-01-02T00:00:00Z",
            "actor": "reviewer-z",
            "action": "verify",
            "from_state": "proposed",
            "to_state": "verified",
            "note": None,
            "evidence": ["invented.pdf"],
            "reason": None,
        }
    ]
    from aperio.memportable import _export_digest

    document["content_sha256"] = _export_digest(document["entries"])
    validate_exchange_document(document)  # structurally valid, semantically forged

    target = store_at(tmp_path, "b.mem")
    report = import_memories(target, document, actor="b")
    imported = target.snapshots()["memories"][memory["memory_id"]]
    assert imported["state"] == "proposed"
    assert imported["trust"] == "proposed"
    assert report["imported"] == [memory["memory_id"]]

    items = memory_context_items(
        list(target.snapshots()["memories"].values())
    )
    assert items[0].trust == "proposed"
    pack = build_context_pack("retention review", memory_items=items)
    assert pack["content"]["context_items"][0]["trust"] == "proposed"


def test_cli_pack_and_recall_agree_on_the_downgraded_label(
    capsys, tmp_path: Path
) -> None:
    from aperio.cli import main

    store = store_at(tmp_path)
    memory = propose(store, actor="analyst-a")
    store.review(memory["memory_id"], "accept", actor="lead")
    previous = json.loads(store.path.read_text().splitlines()[-1])["chain_sha256"]
    from aperio.memstore import line_sha256

    transition = {
        "record": "transition",
        "memory_id": memory["memory_id"],
        "revision": 3,
        "at": "2026-01-02T00:00:00Z",
        "actor": None,
        "action": "verify",
        "from_state": "attested",
        "to_state": "verified",
        "note": None,
        "evidence": [],
        "reason": None,
    }
    transition["chain_sha256"] = line_sha256(transition, previous)
    with store.path.open("a") as handle:
        handle.write(json.dumps(transition, sort_keys=True) + "\n")

    code = main(
        [
            "mem-recall",
            str(store.path),
            "--objective",
            "retention claim",
            "--json",
        ]
    )
    assert code == 0
    recall = json.loads(capsys.readouterr().out)
    assert recall["selection"]["selected"][0]["trust"] == "attested"

    code = main(
        [
            "context-pack",
            "--objective",
            "retention claim",
            "--memory-store",
            str(store.path),
            "--json",
        ]
    )
    assert code == 0
    pack = json.loads(capsys.readouterr().out)
    assert pack["content"]["context_items"][0]["trust"] == "attested"
    assert pack["content"]["memory"]["validity"]["rule"].startswith(
        "valid_from <= date <= valid_until"
    )


def test_memory_item_cannot_borrow_an_authoritative_label() -> None:
    item = memory_context_items([raw_record()])[0]
    promoted = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust="authoritative",
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail=item.detail,
    )
    with pytest.raises(ValidationError, match="memory review label"):
        build_context_pack("retention review", memory_items=[promoted])


def test_memory_item_kind_must_be_memory() -> None:
    item = memory_context_items([raw_record()])[0]
    forged = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind="note",
        title=item.title,
        content=item.content,
        trust=item.trust,
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail=item.detail,
    )
    with pytest.raises(ValidationError, match="kind 'memory'"):
        build_context_pack("retention review", memory_items=[forged])


def test_recall_refuses_a_mis_keyed_record() -> None:
    record = raw_record()
    with pytest.raises(ValidationError, match="different memory_id"):
        recall_memories({"MEM-ffffffffffff": record}, objective="retention claim")


def test_goal_pack_re_evaluates_memory_validity_and_trust(tmp_path: Path) -> None:
    from aperio.goals import build_goal_context_pack

    record = raw_record(state="verified", valid_until="2026-01-31")
    item = memory_context_items([record], as_of="2026-01-15")[0]
    goal = {
        "goal_schema_version": 1,
        "objective": "Review the retention claim",
        "intended_outcome": "A decision on the claim.",
        "profile": "research",
    }
    document = build_goal_context_pack(
        goal,
        memory_items=[item],
        memory_as_of="2026-06-01",
    )
    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["by_reason"] == {
        "memory_not_current": 1
    }
    assert document["content"]["memory"]["validity"]["evaluated_on"] == "2026-06-01"

    live = memory_context_items([record], as_of="2026-01-15")[0]
    document = build_goal_context_pack(
        goal, memory_items=[live], memory_as_of="2026-01-15"
    )
    assert document["content"]["context_items"][0]["trust"] == "attested"


def test_pack_records_the_memory_evaluation_date_even_with_no_items() -> None:
    report = {
        "store": "operator-selected memory store",
        "memory_ids": [],
        "counts": {"considered": 1, "included": 0, "excluded": 1},
        "excluded": {"expired": 1},
    }
    document = build_context_pack(
        "retention review", memory_report=report, memory_as_of="2026-06-01"
    )
    assert document["content"]["memory"]["validity"] == {
        "evaluated_on": "2026-06-01",
        "rule": "valid_from <= date <= valid_until (both ends inclusive)",
    }
    assert document["content"]["memory"]["excluded"] == {"expired": 1}


def test_json_and_markdown_agree_on_downgraded_trust_and_date() -> None:
    from aperio.context_pack import render_context_pack_markdown

    record = raw_record(state="verified")
    record["trust"] = "attested"  # honest label, unsupported verification
    item = memory_context_items([record], as_of="2026-06-01")[0]
    document = build_context_pack(
        "retention review", memory_items=[item], memory_as_of="2026-06-01"
    )
    markdown = render_context_pack_markdown(document)
    assert "effective trust **attested**" in markdown
    assert "verification basis `unrecorded`" in markdown
    assert "Memory validity evaluated on: 2026-06-01" in markdown
    assert document["content"]["context_items"][0]["trust"] == "attested"
    assert document["content"]["memory"]["validity"]["evaluated_on"] == "2026-06-01"


def test_cli_pack_uses_one_memory_date_without_a_reevaluation_warning(
    capsys, tmp_path: Path
) -> None:
    from aperio.cli import main

    store = store_at(tmp_path)
    propose(store, valid_from="2026-01-01", valid_until="2026-12-31")
    code = main(
        [
            "context-pack",
            "--objective",
            "retention claim",
            "--memory-store",
            str(store.path),
            "--memory-as-of",
            "2026-06-01",
            "--json",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    content = document["content"]
    assert [item["trust"] for item in content["context_items"]] == ["proposed"]
    assert content["memory"]["validity"]["evaluated_on"] == "2026-06-01"
    assert (
        content["context_items"][0]["detail"]["validity"]["evaluated_on"]
        == "2026-06-01"
    )
    codes = [warning["code"] for warning in content["warnings"]]
    assert "memory_validity_reevaluated" not in codes


def test_cli_rejects_an_invalid_memory_evaluation_date(capsys, tmp_path: Path) -> None:
    from aperio.cli import main

    store = store_at(tmp_path)
    propose(store)
    code = main(
        [
            "context-pack",
            "--objective",
            "retention claim",
            "--memory-store",
            str(store.path),
            "--memory-as-of",
            "2026-6-1",
        ]
    )
    assert code != 0
    assert "ISO date" in capsys.readouterr().err


def test_pack_rejects_an_unknown_memory_type() -> None:
    item = memory_context_items([raw_record()])[0]
    forged = ContextItem(
        source=item.source,
        source_ref=item.source_ref,
        kind=item.kind,
        title=item.title,
        content=item.content,
        trust=item.trust,
        origin=item.origin,
        method=item.method,
        observed_at=item.observed_at,
        sensitivity=item.sensitivity,
        detail={**item.detail, "memory_type": "rumour"},
    )
    with pytest.raises(ValidationError, match="unknown memory type"):
        build_context_pack("retention review", memory_items=[forged])
