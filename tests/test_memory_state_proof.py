"""State proof: a claimed review state must be derivable from its history.

The M3 trust boundary proved a ``verified`` label from recorded events. This
module closes the same gap one tier lower: a record that *claims*
``state: "attested"`` without a recorded ``accept`` transition in the history
it carries must never receive attested authority — not in the derivation, not
in retrieval ranking, not in context-item conversion, and not in either pack
builder. Genuine store records are unaffected: replay always carries the
transition that produced the recorded state.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from aperio.context_pack import build_context_pack, render_context_pack_markdown
from aperio.errors import ValidationError
from aperio.goals import build_goal_context_pack
from aperio.memportable import export_memories, import_memories
from aperio.memrecall import memory_context_items, recall_memories
from aperio.memstore import (
    MemoryStore,
    memory_content_sha256,
    memory_identity,
    resolve_trust,
)

BASE = {
    "type": "observation",
    "title": "Retention claim",
    "content": "Retention doubled after onboarding.",
    "scope": {"kind": "global"},
    "tags": ["retention"],
}

# The real content identity of BASE, so a forged record is indistinguishable
# from a relabelled genuine one except for the state/history it claims.
_BASE_IDENTITY = {**BASE, "evidence": []}
REAL_ID = memory_identity(_BASE_IDENTITY)
REAL_SHA = memory_content_sha256(_BASE_IDENTITY)


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def propose(store: MemoryStore) -> dict:
    record = dict(BASE)
    record["actor"] = "author-1"
    return store.propose(record)["memory"]


def forged_attested(memory_id: str = REAL_ID,
                    content_sha256: str = REAL_SHA) -> dict:
    """A raw record in snapshot shape that claims acceptance with no history."""

    return {
        "memory_schema_version": 1,
        "memory_id": memory_id,
        "revision": 2,
        "type": BASE["type"],
        "title": BASE["title"],
        "content": BASE["content"],
        "scope": dict(BASE["scope"]),
        "source": {"kind": "agent"},
        "tags": list(BASE["tags"]),
        "sensitivity": "internal",
        "evidence": [],
        "valid_from": None,
        "valid_until": None,
        "supersedes": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "actor": "author-1",
        "state": "attested",
        "recorded_state": "attested",
        "content_sha256": content_sha256,
        "history": [],
    }


# ---------------------------------------------------------------------------
# 1. The derivation itself
# ---------------------------------------------------------------------------


def test_attested_state_requires_an_accept_transition() -> None:
    record = forged_attested()

    resolved = resolve_trust(record)

    assert resolved["trust"] == "proposed"
    assert resolved["state"] == "attested"
    assert resolved["recorded_state"] == "attested"
    assert resolved["verification"]["basis"] == "unrecorded"
    assert resolved["verification"]["downgraded"] is True
    assert any("acceptance" in reason for reason in resolved["verification"]["reasons"])


def test_declared_attested_label_without_accept_is_refused() -> None:
    record = forged_attested()
    record["trust"] = "attested"

    with pytest.raises(ValidationError, match="disagrees"):
        resolve_trust(record)


def test_declared_verification_basis_above_history_is_refused() -> None:
    record = forged_attested()
    record["verification"] = {"basis": "not-verified"}

    resolved = resolve_trust(record)
    assert resolved["trust"] == "proposed"


def test_genuine_attested_history_keeps_attested_trust(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    snapshot = store.snapshots()["memories"][record["memory_id"]]

    resolved = resolve_trust(snapshot)

    assert resolved["trust"] == "attested"
    assert resolved["verification"]["basis"] == "not-verified"
    assert "downgraded" not in resolved["verification"]


def test_genuine_verified_history_stays_verified(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(
        record["memory_id"],
        "verify",
        actor="reviewer-1",
        evidence=["review-checklist.md"],
    )
    snapshot = store.snapshots()["memories"][record["memory_id"]]

    assert resolve_trust(snapshot)["trust"] == "verified"


# ---------------------------------------------------------------------------
# 2. Retrieval ranking
# ---------------------------------------------------------------------------


def test_unsupported_attested_recall_ranks_as_proposed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)

    selection = recall_memories(
        {record["memory_id"]: forged_attested(record["memory_id"],
                                              record["content_sha256"])},
        objective="retention claim",
    )

    assert [m["trust"] for m in selection["selected"]] == ["proposed"]
    assert selection["selected"][0]["state"] == "attested"
    assert selection["selected"][0]["verification_basis"] == "unrecorded"


def test_unsupported_attested_loses_the_global_scope_bypass(
    tmp_path: Path,
) -> None:
    """A reviewed global memory may enter recall without keyword overlap; an
    *unsupported* attested claim may not borrow that privilege."""

    store = store_at(tmp_path)
    record = propose(store)
    forged = forged_attested(record["memory_id"], record["content_sha256"])
    forged["title"] = "Completely unrelated widget note"
    forged["content"] = "Nothing to do with the objective at all."
    forged["tags"] = ["unrelated"]

    selection = recall_memories(
        {record["memory_id"]: forged}, objective="retention claim"
    )

    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"].get("no-match") == 1


def test_genuine_attested_keeps_the_global_scope_bypass(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")

    selection = recall_memories(
        store.snapshots()["memories"], objective="something unrelated entirely"
    )

    assert [m["trust"] for m in selection["selected"]] == ["attested"]


# ---------------------------------------------------------------------------
# 3. Context-item conversion
# ---------------------------------------------------------------------------


def test_context_items_mark_unsupported_attested_unreviewed() -> None:
    items = memory_context_items(
        [forged_attested()]
    )

    assert len(items) == 1
    assert items[0].trust == "proposed"
    assert items[0].detail["reviewed"] is False
    assert items[0].detail["verification_basis"] == "unrecorded"


def test_context_items_mark_genuine_attested_reviewed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")

    items = memory_context_items([store.snapshots()["memories"]
                                  [record["memory_id"]]])

    assert items[0].trust == "attested"
    assert items[0].detail["reviewed"] is True


# ---------------------------------------------------------------------------
# 4. Both pack builders
# ---------------------------------------------------------------------------


def _pack_facts(document: dict) -> dict:
    item = document["content"]["context_items"][0]
    return {
        "item_trust": item["trust"],
        "detail_trust": item["detail"]["trust"],
        "basis": item["detail"]["verification_basis"],
    }


def _warn_codes(document: dict) -> list[str]:
    return [warning["code"] for warning in document["content"]["warnings"]]


def test_context_pack_downgrades_unsupported_attested_item() -> None:
    item = memory_context_items(
        [forged_attested()]
    )[0]

    document = build_context_pack("retention review", memory_items=[item])

    assert _pack_facts(document) == {
        "item_trust": "proposed",
        "detail_trust": "proposed",
        "basis": "unrecorded",
    }
    assert "memory_item_trust_downgraded" in _warn_codes(document)
    markdown = render_context_pack_markdown(document)
    assert "effective trust **proposed**" in markdown


def test_goal_pack_downgrades_unsupported_attested_item(tmp_path: Path) -> None:
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(
        json.dumps(
            {
                "objective": "Review the retention claim",
                "intended_outcome": "A decision on the claim",
            }
        ),
        encoding="utf-8",
    )
    from aperio.goals import load_goal_file

    goal = load_goal_file(goal_file)
    item = memory_context_items(
        [forged_attested()]
    )[0]

    document = build_goal_context_pack(goal, memory_items=[item])

    assert _pack_facts(document) == {
        "item_trust": "proposed",
        "detail_trust": "proposed",
        "basis": "unrecorded",
    }
    assert "memory_item_trust_downgraded" in _warn_codes(document)


# ---------------------------------------------------------------------------
# 5. History consistency: a claim its own event log contradicts
# ---------------------------------------------------------------------------


def _accept_history(memory_id: str, at: str = "2026-01-02T00:00:00Z") -> dict:
    return {
        "memory_id": memory_id,
        "revision": 2,
        "at": at,
        "actor": "ops-lead",
        "action": "accept",
        "from_state": "proposed",
        "to_state": "attested",
        "note": None,
        "evidence": [],
        "reason": None,
    }


def test_history_ending_in_retract_contradicts_a_claimed_attested_state(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    store.review(record["memory_id"], "retract", actor="ops-lead")
    snapshot = store.snapshots()["memories"][
        record["memory_id"]
    ]
    assert snapshot["recorded_state"] == "retracted"

    # The genuine snapshot reports its true state; the derivation agrees.
    resolved = resolve_trust(snapshot)
    assert resolved["trust"] == "proposed"
    assert resolved["verification"]["replayed_state"] == "retracted"

    # A forger relabels it attested while keeping the withdrawal history.
    forged = dict(snapshot)
    forged["state"] = "attested"
    forged["recorded_state"] = "attested"
    resolved = resolve_trust(forged)
    assert resolved["trust"] == "proposed"
    assert resolved["verification"]["basis"] == "unrecorded"
    assert resolved["verification"]["downgraded"] is True
    assert any(
        "retracted" in reason
        for reason in resolved["verification"]["reasons"]
    )


def test_recall_excludes_a_record_whose_history_ends_inactive(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    store.review(record["memory_id"], "retract", actor="ops-lead")
    snapshot = store.snapshots()["memories"][
        record["memory_id"]
    ]
    forged = dict(snapshot)
    forged["state"] = "attested"
    forged["recorded_state"] = "attested"

    selection = recall_memories(
        {record["memory_id"]: forged}, objective="retention claim"
    )

    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"].get("state") == 1


def test_context_items_refuse_a_claim_whose_history_ends_inactive(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    store.review(record["memory_id"], "retract", actor="ops-lead")
    snapshot = store.snapshots()["memories"][
        record["memory_id"]
    ]
    forged = dict(snapshot)
    forged["state"] = "attested"
    forged["recorded_state"] = "attested"

    with pytest.raises(ValidationError, match="retracted"):
        memory_context_items([forged])


def test_reinstated_record_claims_proposed_again(tmp_path: Path) -> None:
    """accept → retract → reinstate ends in proposed; a claim of attested
    on top of that history is contradicted, and the reinstated record itself
    stays honest at proposed."""

    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    store.review(record["memory_id"], "retract", actor="ops-lead")
    store.review(record["memory_id"], "reinstate", actor="ops-lead")
    snapshot = store.snapshots()["memories"][record["memory_id"]]

    resolved = resolve_trust(snapshot)
    assert resolved["recorded_state"] == "proposed"
    assert resolved["trust"] == "proposed"
    assert resolved["verification"]["replayed_state"] == "proposed"

    forged = dict(snapshot)
    forged["state"] = "attested"
    forged["recorded_state"] = "attested"
    assert resolve_trust(forged)["trust"] == "proposed"


# ---------------------------------------------------------------------------
# 6. Compatibility guards
# ---------------------------------------------------------------------------


def test_legacy_verified_without_history_still_attested() -> None:
    record = forged_attested()
    record["state"] = "verified"
    record["recorded_state"] = "verified"

    resolved = resolve_trust(record)

    assert resolved["trust"] == "attested"
    assert resolved["verification"]["basis"] == "unrecorded"


def test_exchange_claiming_attested_without_history_imports_as_proposed(
    tmp_path: Path,
) -> None:
    """A truncated or hand-built exchange file may claim ``attested``; it is
    accepted as data and always enters the destination store as ``proposed``."""

    store = store_at(tmp_path)
    record = propose(store)
    store.review(record["memory_id"], "accept", actor="ops-lead")
    document = export_memories(store)
    document = copy.deepcopy(document)
    document["entries"][0]["memory"]["history"] = []
    # Recompute the document digest so the forged file is internally
    # consistent, as a real tampered file could be. An import never applies
    # the claimed state either way.
    document["content_sha256"] = hashlib.sha256(
        json.dumps(
            document["entries"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    report = import_memories(store_at(tmp_path, "dest.mem"), document)

    assert report["imported"] == [record["memory_id"]]
    imported = store_at(tmp_path, "dest.mem").snapshots()["memories"][
        record["memory_id"]
    ]
    assert imported["state"] == "proposed"
    assert imported["trust"] == "proposed"
