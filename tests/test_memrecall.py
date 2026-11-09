"""Memory retrieval tests: deterministic ranking, scope, trust, and budget."""

from __future__ import annotations

import pytest

from aperio.errors import ValidationError
from aperio.memrecall import (
    MAX_RECALL_ITEMS,
    RECALL_LIMITATION,
    memory_context_items,
    recall_memories,
)
from aperio.memstore import MemoryStore, memory_trust


def snapshot(
    memory_id: str,
    *,
    state: str = "proposed",
    title: str = "T",
    content: str = "body",
    scope: dict | None = None,
    tags: list[str] | None = None,
    sensitivity: str = "internal",
    created_at: str = "2026-01-01T00:00:00Z",
    updated_at: str | None = None,
    sha: str | None = None,
    verified_by: str | None = None,
) -> dict:
    history: list[dict] = []
    if state == "attested":
        # The recorded state must be derivable from the history the record
        # carries: attested exists only through a recorded accept transition.
        history.append(
            {
                "memory_id": memory_id,
                "revision": 2,
                "at": updated_at or created_at,
                "actor": verified_by or "ops-lead",
                "action": "accept",
                "from_state": "proposed",
                "to_state": "attested",
                "note": None,
                "evidence": [],
                "reason": None,
            }
        )
    if verified_by:
        history.append(
            {
                "memory_id": memory_id,
                "revision": 2,
                "at": updated_at or created_at,
                "actor": verified_by,
                "action": "verify",
                "from_state": "proposed",
                "to_state": "verified",
                "note": None,
                "evidence": ["study.pdf"],
                "reason": None,
            }
        )
    return {
        "memory_id": memory_id,
        "type": "observation",
        "title": title,
        "content": content,
        "scope": scope or {"kind": "global", "value": None, "profile": None},
        "source": {"kind": "operator", "ref": None, "note": None},
        "tags": tags or [],
        "sensitivity": sensitivity,
        "evidence": [],
        "valid_from": None,
        "valid_until": None,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "state": state,
        "trust": memory_trust(state),
        "content_sha256": sha or memory_id.lower().replace("mem-", "") * 2,
        "declared_state": None,
        "history": history,
    }


def ids(selection: dict) -> list[str]:
    return [memory["memory_id"] for memory in selection["selected"]]


# ---------------------------------------------------------------------------
# scope and relevance
# ---------------------------------------------------------------------------


def test_objective_scope_memory_is_selected_for_the_task() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Brand colour",
            content="Primary brand colour is deep green.",
            scope={
                "kind": "objective",
                "value": "design a spring campaign poster",
                "profile": None,
            },
            tags=["brand"],
        )
    }
    selection = recall_memories(
        memories,
        objective="Design a spring campaign poster for the garden centre",
        profile="design",
    )
    assert ids(selection) == ["MEM-000000000001"]
    assert selection["budget"]["items_included"] == 1


def test_partially_overlapping_objective_scope_still_applies() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            scope={
                "kind": "objective",
                "value": "spring campaign poster",
                "profile": None,
            },
        )
    }
    selection = recall_memories(
        memories, objective="Produce a spring campaign poster draft"
    )
    assert ids(selection) == ["MEM-000000000001"]


def test_unrelated_objective_scope_is_excluded_with_a_reason() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            scope={
                "kind": "objective",
                "value": "quarterly tax filing",
                "profile": None,
            },
        )
    }
    selection = recall_memories(
        memories, objective="Design a spring campaign poster"
    )
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"scope": 1}


def test_profile_scope_requires_the_matching_profile() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            scope={"kind": "profile", "value": None, "profile": "writing"},
            title="Tone",
            content="Keep prose plain and warm.",
        )
    }
    writing = recall_memories(
        memories, objective="Write a newsletter", profile="writing"
    )
    research = recall_memories(
        memories, objective="Write a newsletter", profile="research"
    )
    assert ids(writing) == ["MEM-000000000001"]
    assert research["excluded"]["by_reason"] == {"scope": 1}


def test_workspace_scope_matches_an_explicit_label() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            scope={"kind": "workspace", "value": "garden-centre", "profile": None},
        )
    }
    matched = recall_memories(
        memories, objective="Poster", scope_values={"workspace": "garden-centre"}
    )
    missed = recall_memories(
        memories, objective="Poster", scope_values={"workspace": "other"}
    )
    assert ids(matched) == ["MEM-000000000001"]
    assert missed["excluded"]["by_reason"] == {"scope": 1}


def test_unreviewed_global_memory_needs_a_relevance_hit() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            title="Sourdough starter",
            content="Feed the starter every morning.",
        )
    }
    selection = recall_memories(memories, objective="Design a poster")
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"no-match": 1}


def test_reviewed_global_preference_applies_without_a_keyword_hit() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Working preference",
            content="Always state assumptions explicitly.",
        )
    }
    selection = recall_memories(memories, objective="Design a poster")
    assert ids(selection) == ["MEM-000000000001"]


# ---------------------------------------------------------------------------
# trust and lifecycle filters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["retracted", "superseded"])
def test_inactive_memories_are_never_recalled(state: str) -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", state=state, title="Brand colour"
        )
    }
    selection = recall_memories(memories, objective="Brand colour brief")
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"state": 1}


def test_expired_memory_is_excluded_and_counted() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="expired",
            title="Old brand colour",
            content="Brand colour is orange.",
        )
    }
    selection = recall_memories(memories, objective="Brand colour brief")
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"expired": 1}


def test_proposed_memories_can_be_included_or_excluded() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand colour", content="Green."
        )
    }
    with_proposals = recall_memories(memories, objective="Brand colour")
    without = recall_memories(
        memories, objective="Brand colour", include_proposed=False
    )
    assert ids(with_proposals) == ["MEM-000000000001"]
    assert without["selected"] == []
    assert without["excluded"]["by_reason"] == {"state": 1}


def test_unreviewed_memory_triggers_a_warning() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand colour", content="Green."
        )
    }
    selection = recall_memories(memories, objective="Brand colour")
    codes = {warning["code"] for warning in selection["warnings"]}
    assert "memory_unreviewed_selected" in codes


def test_verified_memory_outranks_attested_and_proposed() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand colour", content="Green."
        ),
        "MEM-000000000002": snapshot(
            "MEM-000000000002", state="attested", title="Brand colour",
            content="Green."
        ),
        "MEM-000000000003": snapshot(
            "MEM-000000000003", state="verified", title="Brand colour",
            content="Green.", verified_by="reviewer-b"
        ),
    }
    selection = recall_memories(memories, objective="Brand colour")
    assert ids(selection)[0] == "MEM-000000000003"


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------


def test_private_memory_is_excluded_until_explicitly_allowed() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Salary expectations",
            content="Never discuss compensation in public decks.",
            sensitivity="private",
        )
    }
    default = recall_memories(memories, objective="Salary expectations deck")
    allowed = recall_memories(
        memories,
        objective="Salary expectations deck",
        max_sensitivity="private",
    )
    assert default["selected"] == []
    assert default["excluded"]["by_reason"] == {"sensitivity": 1}
    assert ids(allowed) == ["MEM-000000000001"]


def test_private_items_become_local_only_context() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Client note",
            content="The client dislikes green.",
            sensitivity="private",
        )
    }
    selection = recall_memories(
        memories, objective="Client note", max_sensitivity="private"
    )
    items = memory_context_items(selection["selected"])
    assert items[0].sensitivity == "local_only"


def test_invalid_sensitivity_filter_is_refused() -> None:
    with pytest.raises(ValidationError):
        recall_memories({}, max_sensitivity="secret")


# ---------------------------------------------------------------------------
# determinism, budget, and limits
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_identical_output() -> None:
    memories = {
        f"MEM-00000000000{index}": snapshot(
            f"MEM-00000000000{index}",
            title="Brand guide",
            content=f"Rule {index}",
            tags=["brand"],
        )
        for index in range(1, 5)
    }
    first = recall_memories(memories, objective="Brand guide")
    second = recall_memories(memories, objective="Brand guide")
    assert ids(first) == ids(second)
    assert first["excluded"] == second["excluded"]


def test_stable_ordering_ignores_mapping_insertion_order() -> None:
    forward = {
        f"MEM-00000000000{index}": snapshot(
            f"MEM-00000000000{index}", title="Brand guide", content="rule"
        )
        for index in range(1, 4)
    }
    backward = {
        key: forward[key] for key in reversed(list(forward))
    }
    assert ids(recall_memories(forward, objective="Brand guide")) == ids(
        recall_memories(backward, objective="Brand guide")
    )


def test_budget_excludes_whole_items_and_reports_them() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand guide", content="a" * 60
        ),
        "MEM-000000000002": snapshot(
            "MEM-000000000002", title="Brand guide", content="b" * 60
        ),
    }
    selection = recall_memories(
        memories, objective="Brand guide", budget_bytes=100
    )
    assert selection["budget"]["used_bytes"] <= 100
    assert selection["budget"]["items_included"] == 1
    assert selection["excluded"]["by_reason"] == {"budget": 1}


def test_memory_statement_is_never_truncated() -> None:
    body = "Sentence one. " * 10
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand guide", content=body
        )
    }
    selection = recall_memories(memories, objective="Brand guide")
    assert selection["selected"][0]["content"] == body


def test_item_limit_is_bounded_and_reported() -> None:
    memories = {
        f"MEM-{index:012d}": snapshot(
            f"MEM-{index:012d}", title="Brand guide", content="rule"
        )
        for index in range(20)
    }
    selection = recall_memories(
        memories, objective="Brand guide", limit=3
    )
    assert selection["budget"]["items_included"] == 3
    assert selection["excluded"]["by_reason"] == {"limit": 17}
    assert MAX_RECALL_ITEMS == 8


def test_oversized_limit_is_clamped_not_crashing() -> None:
    memories = {
        f"MEM-{index:012d}": snapshot(
            f"MEM-{index:012d}", title="Brand guide", content="rule"
        )
        for index in range(12)
    }
    selection = recall_memories(memories, objective="Brand guide", limit=500)
    assert selection["budget"]["items_included"] == MAX_RECALL_ITEMS


def test_no_memory_at_all_still_returns_a_usable_result() -> None:
    selection = recall_memories({}, objective="Anything")
    assert selection["selected"] == []
    assert selection["excluded"]["total"] == 0
    assert selection["warnings"] == []
    assert selection["limitation"] == RECALL_LIMITATION


def test_duplicate_content_is_selected_once() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand guide", content="Same body.",
            sha="a" * 64,
        ),
        "MEM-000000000002": snapshot(
            "MEM-000000000002", title="Brand guide", content="Same body.",
            sha="a" * 64,
        ),
    }
    selection = recall_memories(memories, objective="Brand guide")
    assert selection["budget"]["items_included"] == 1
    assert selection["excluded"]["by_reason"] == {"duplicate": 1}


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def test_conflicting_memories_are_both_shown_and_flagged() -> None:
    scope = {"kind": "objective", "value": "brand refresh", "profile": None}
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Brand colour",
            content="Primary colour is deep green.",
            scope=scope,
            sha="1" * 64,
        ),
        "MEM-000000000002": snapshot(
            "MEM-000000000002",
            state="attested",
            title="Brand colour",
            content="Primary colour is navy blue.",
            scope=scope,
            sha="2" * 64,
        ),
    }
    selection = recall_memories(
        memories, objective="brand refresh", profile="design"
    )
    assert len(selection["selected"]) == 2
    assert len(selection["conflicts"]) == 1
    assert selection["conflicts"][0]["memory_ids"] == [
        "MEM-000000000001",
        "MEM-000000000002",
    ]
    assert any(
        warning["code"] == "memory_conflict" for warning in selection["warnings"]
    )


def test_same_title_in_different_scopes_is_not_a_conflict() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001",
            state="attested",
            title="Tone",
            content="Be warm.",
            scope={"kind": "profile", "value": None, "profile": "writing"},
            sha="1" * 64,
        ),
        "MEM-000000000002": snapshot(
            "MEM-000000000002",
            state="attested",
            title="Tone",
            content="Be brief.",
            scope={"kind": "profile", "value": None, "profile": "business"},
            sha="2" * 64,
        ),
    }
    selection = recall_memories(
        memories, objective="Tone review", profile="writing"
    )
    assert selection["conflicts"] == []


# ---------------------------------------------------------------------------
# real store round-trip and context items
# ---------------------------------------------------------------------------


def test_recall_from_a_real_store(tmp_path) -> None:
    store = MemoryStore(tmp_path / "store.mem")
    memory = store.propose(
        {
            "type": "requirement",
            "title": "Brand colour",
            "content": "Primary brand colour is deep green.",
            "scope": {"kind": "objective", "value": "spring poster"},
            "tags": ["brand"],
            "evidence": ["brand guide v3"],
        }
    )["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    state = store.snapshots()
    selection = recall_memories(
        state["memories"], objective="spring poster draft", profile="design"
    )
    assert ids(selection) == [memory["memory_id"]]
    [item] = memory_context_items(selection["selected"])
    assert item.source == "memory"
    assert item.trust == "attested"
    assert item.detail["memory_id"] == memory["memory_id"]
    assert item.detail["evidence"] == ["brand guide v3"]
    assert item.detail["reviewed"] is True


def test_memory_items_report_proposed_state_honestly() -> None:
    memories = {
        "MEM-000000000001": snapshot(
            "MEM-000000000001", title="Brand guide", content="Green."
        )
    }
    selection = recall_memories(memories, objective="Brand guide")
    [item] = memory_context_items(selection["selected"])
    assert item.trust == "proposed"
    assert item.detail["reviewed"] is False


def test_non_mapping_memory_input_is_refused() -> None:
    with pytest.raises(ValidationError):
        recall_memories({"MEM-000000000001": "not a snapshot"})


def test_incomplete_snapshot_is_refused_with_a_clear_error() -> None:
    with pytest.raises(ValidationError, match="missing required fields"):
        recall_memories(
            {"MEM-000000000001": {"memory_id": "MEM-000000000001", "state": "proposed"}},
            objective="anything",
        )


def test_workspace_scoped_memory_needs_an_explicit_workspace_label() -> None:
    """A workspace scope the CLI can create must also be selectable."""

    memories = {
        "MEM-000000000001": {
            "memory_id": "MEM-000000000001",
            "type": "convention",
            "title": "Brand voice",
            "content": "Lead with the client's own words.",
            "scope": {"kind": "workspace", "value": "brandbook"},
            "tags": ["voice"],
            "state": "attested",
            "trust": "attested",
            "sensitivity": "internal",
            "content_sha256": "a" * 64,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "history": [
                {
                    "memory_id": "MEM-000000000001",
                    "revision": 2,
                    "at": "2026-01-01T00:00:00Z",
                    "actor": "ops-lead",
                    "action": "accept",
                    "from_state": "proposed",
                    "to_state": "attested",
                    "note": None,
                    "evidence": [],
                    "reason": None,
                }
            ],
        }
    }

    without = recall_memories(memories, objective="brand voice")
    assert without["selected"] == []
    assert without["excluded"]["by_reason"] == {"scope": 1}

    with_label = recall_memories(
        memories,
        objective="brand voice",
        scope_values={"workspace": "brandbook"},
    )
    assert [m["memory_id"] for m in with_label["selected"]] == [
        "MEM-000000000001"
    ]
    other = recall_memories(
        memories,
        objective="brand voice",
        scope_values={"workspace": "newsletter"},
    )
    assert other["selected"] == []
