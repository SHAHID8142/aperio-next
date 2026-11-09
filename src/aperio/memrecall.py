"""Deterministic, inspectable memory retrieval (memory-recall v1).

Ranking is deliberately simple and explainable: scope applicability, explicit
term overlap with the current objective, review state, freshness, and the
caller's context budget. There are no embeddings, vector stores, model
scores, online lookups, or provider calls, so identical inputs always produce
identical output and every exclusion has a stated reason.

The honest limitation: term overlap is a keyword heuristic, not semantic
understanding. It will miss paraphrase and can select on a shared word. That
is why recall never rewrites, truncates, promotes, or merges memory content,
and why every selection keeps its provenance, review state, and identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from aperio.context_pack import (
    MAX_CONTEXT_ITEM_BYTES,
    ContextItem,
)
from aperio.errors import ValidationError
from aperio.memstore import (
    ACTIVE_STATES,
    INACTIVE_STATES,
    MEMORY_STATES,
    SENSITIVITY_LEVELS,
    SENSITIVITY_RANK,
    VALIDITY_RULE,
    evaluate_on,
    memory_validity,
    resolve_trust,
)

MAX_RECALL_ITEMS = 8
MAX_SELECTED_BYTES = 64 * 1024
_MAX_RELEVANCE_TERMS = 24
_MIN_TERM_LENGTH = 3
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "and", "are", "but", "for", "from", "has", "have", "into", "its",
        "not", "the", "this", "that", "with", "will", "your", "you", "was",
        "were", "what", "when", "where", "which", "who", "how", "why", "all",
        "any", "can", "does", "may", "must", "should", "than", "then",
        "there", "these", "they", "use", "using", "very",
    }
)
_SCOPE_RANK = {"profile": 3, "objective": 4, "workspace": 4, "global": 1}
_TRUST_RANK = {"verified": 3, "attested": 2, "proposed": 1}
RECALL_LIMITATION = (
    "Relevance is a bounded keyword overlap over the objective, title, tags, "
    "and content. It is deterministic and inspectable, not semantic: "
    "paraphrase can be missed and a shared word can match. Scope, review "
    "state, and the memory's own validity window, not guessed similarity, "
    "decide inclusion. An objective-scoped memory applies when most of its "
    "own terms (at least 60%) appear in the current objective, or when the "
    "two texts match exactly. When no date is given, validity is evaluated "
    "against today (UTC); pass --as-of for reproducible historical recall."
)


def _terms(text: str) -> set[str]:
    found = set()
    for match in _TERM_RE.findall(text.lower()):
        if len(match) >= _MIN_TERM_LENGTH and match not in _STOP_WORDS:
            found.add(match)
        if len(found) >= _MAX_RELEVANCE_TERMS:
            break
    return found


def _validate_sensitivity(value: str) -> str:
    if value not in SENSITIVITY_LEVELS:
        raise ValidationError(
            "memory sensitivity filter must be one of: "
            + ", ".join(SENSITIVITY_LEVELS)
        )
    return value


def _scope_rank(
    scope: Mapping,
    *,
    objective_norm: str,
    objective_terms: set[str],
    profile: str,
    scope_values: Mapping,
) -> int:
    kind = scope["kind"]
    if kind == "global":
        return _SCOPE_RANK["global"]
    if kind == "profile":
        return _SCOPE_RANK["profile"] if scope["profile"] == profile else 0
    if kind == "objective":
        value = (scope["value"] or "").strip().lower()
        if not value:
            return 0
        if value == objective_norm:
            return _SCOPE_RANK["objective"]
        # An objective scope is a focus topic, not a hash of one exact
        # sentence: it applies when most of its own terms appear in the
        # current objective. The rule is stated, deterministic, and visible
        # in the recall report rather than hidden in a similarity score.
        scope_terms = _terms(value)
        if scope_terms and (
            len(scope_terms & objective_terms) / len(scope_terms) >= 0.6
        ):
            return _SCOPE_RANK["objective"] - 1
        return 0
    if kind == "workspace":
        expected = scope_values.get("workspace")
        return _SCOPE_RANK["workspace"] if expected and scope["value"] == expected else 0
    return 0


def recall_memories(
    memories: Mapping[str, dict],
    *,
    objective: str = "",
    profile: str = "general",
    scope_values: Mapping[str, str] | None = None,
    tags: Sequence[str] = (),
    budget_bytes: int = 8192,
    max_sensitivity: str = "internal",
    as_of: str | None = None,
    include_proposed: bool = True,
    limit: int = MAX_RECALL_ITEMS,
) -> dict:
    """Rank and select memories with explicit inclusion/exclusion reasons."""

    _validate_sensitivity(max_sensitivity)
    # Default evaluation date: today. Expiry therefore applies to ordinary
    # recall without any flag, while --as-of keeps historical recall exact.
    evaluated_on = evaluate_on(as_of)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("memory recall limit must be a positive integer")
    if limit > MAX_RECALL_ITEMS:
        limit = MAX_RECALL_ITEMS
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int):
        raise ValidationError("memory budget must be an integer number of bytes")
    if budget_bytes < 0:
        raise ValidationError("memory budget must not be negative")
    if not isinstance(memories, Mapping):
        raise ValidationError("memories must be a mapping of memory snapshots")
    wanted_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    scope_map = dict(scope_values or {})
    objective_norm = (objective or "").strip().lower()
    objective_terms = _terms(objective or "")

    excluded: dict[str, int] = {}

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    candidates: list[dict] = []
    for memory_id in sorted(memories):
        memory = memories[memory_id]
        if not isinstance(memory, Mapping):
            raise ValidationError(
                f"memory {memory_id} is not a memory snapshot object"
            )
        missing = [
            key
            for key in ("state", "scope", "title", "content", "tags",
                        "sensitivity", "content_sha256", "memory_id")
            if key not in memory
        ]
        if missing:
            raise ValidationError(
                f"memory {memory_id} is missing required fields: "
                + ", ".join(missing)
            )
        if memory.get("memory_id") != memory_id:
            raise ValidationError(
                f"memory {memory_id} is keyed under a different memory_id; the "
                "map key and the record's own identity must agree"
            )
        # Validate the record's trust contract before any filtering: a
        # malformed or self-promoting record is refused, not quietly skipped.
        resolved = resolve_trust(memory, label="memory")
        state = resolved["state"]
        validity = memory_validity(memory, as_of=evaluated_on)
        if validity["reason"] == "expired" or state == "expired":
            exclude("expired")
            continue
        if not validity["current"]:
            # valid_from has not started: not applicable yet, whatever the
            # review state says.
            exclude("future")
            continue
        if state not in ACTIVE_STATES:
            exclude("state")
            continue
        if state == "proposed" and not include_proposed:
            exclude("state")
            continue
        if SENSITIVITY_RANK.get(memory.get("sensitivity", "internal"), 2) > (
            SENSITIVITY_RANK[max_sensitivity]
        ):
            exclude("sensitivity")
            continue
        scope_rank = _scope_rank(
            memory["scope"],
            objective_norm=objective_norm,
            objective_terms=objective_terms,
            profile=profile,
            scope_values=scope_map,
        )
        if scope_rank == 0:
            exclude("scope")
            continue
        memory_tags = {str(tag).lower() for tag in memory.get("tags", ())}
        if wanted_tags and not (wanted_tags & memory_tags):
            exclude("tag")
            continue
        title_terms = _terms(memory.get("title", ""))
        content_terms = _terms(memory.get("content", ""))
        relevance = (
            len(objective_terms & title_terms) * 3
            + len(objective_terms & memory_tags) * 2
            + len(objective_terms & content_terms)
        )
        if relevance == 0 and scope_rank < _SCOPE_RANK["profile"]:
            # A global-scope memory only enters on explicit relevance unless
            # the operator has reviewed it, in which case it is a deliberate
            # standing preference rather than noise.
            if not (scope_rank == _SCOPE_RANK["global"] and state != "proposed"):
                exclude("no-match")
                continue
        trust = resolved["trust"]
        candidates.append(
            {
                "memory": memory,
                "scope_rank": scope_rank,
                "relevance": relevance,
                "resolved": resolved,
                "trust_rank": _TRUST_RANK.get(trust, 1),
                "score": scope_rank * 1000
                + relevance * 10
                + _TRUST_RANK.get(trust, 1),
            }
        )

    candidates.sort(
        key=lambda entry: (
            -entry["score"],
            -entry["relevance"],
            entry["memory"].get("updated_at") or "",
            entry["memory"]["memory_id"],
        )
    )
    # Most recent first within a score band, then stable id order.
    candidates.sort(key=lambda entry: entry["score"], reverse=True)

    selected: list[dict] = []
    used_bytes = 0
    seen_hashes: set[str] = set()
    for entry in candidates:
        memory = entry["memory"]
        if len(selected) >= limit:
            exclude("limit")
            continue
        if memory["content_sha256"] in seen_hashes:
            exclude("duplicate")
            continue
        content_bytes = len(memory["content"].encode("utf-8"))
        if content_bytes > MAX_CONTEXT_ITEM_BYTES:
            exclude("oversize")
            continue
        if used_bytes + content_bytes > budget_bytes:
            exclude("budget")
            continue
        seen_hashes.add(memory["content_sha256"])
        resolved = entry["resolved"]
        selected.append(
            {
                **memory,
                "relevance": entry["relevance"],
                "scope_rank": entry["scope_rank"],
                "score": entry["score"],
                "content_bytes": content_bytes,
                "recorded_state": resolved["recorded_state"],
                "trust": resolved["trust"],
                "verification": resolved["verification"],
                "verification_basis": resolved["verification"]["basis"],
            }
        )
        used_bytes += content_bytes

    warnings: list[dict] = []
    if not selected and memories:
        warnings.append(
            {
                "code": "no_memories_selected",
                "detail": (
                    "no stored memory was applicable, relevant, permitted by "
                    "the sensitivity filter, or inside the budget"
                ),
            }
        )
    proposed_selected = [m["memory_id"] for m in selected if m["state"] == "proposed"]
    if proposed_selected:
        warnings.append(
            {
                "code": "memory_unreviewed_selected",
                "detail": (
                    "proposed (unreviewed) memories were included as "
                    "proposals only, not as established fact: "
                    + ", ".join(proposed_selected[:5])
                ),
            }
        )
    unrecorded = [
        memory["memory_id"]
        for memory in selected
        if (memory.get("verification") or {}).get("basis") == "unrecorded"
    ]
    if unrecorded:
        warnings.append(
            {
                "code": "memory_verification_unrecorded",
                "detail": (
                    "these memories were marked verified without recorded "
                    "supporting evidence; they are shown as attested, not as "
                    "evidence-backed: " + ", ".join(unrecorded[:5])
                ),
            }
        )
    conflicts = _conflicts(selected)
    if conflicts:
        warnings.append(
            {
                "code": "memory_conflict",
                "detail": (
                    "selected memories disagree about the same scope and "
                    "subject; both are shown and neither is preferred: "
                    + "; ".join(
                        ", ".join(group["memory_ids"]) for group in conflicts[:3]
                    )
                ),
            }
        )
    return {
        "selected": selected,
        "as_of": evaluated_on,
        "validity": {"evaluated_on": evaluated_on, "rule": VALIDITY_RULE},
        "excluded": {"total": sum(excluded.values()), "by_reason": dict(sorted(excluded.items()))},
        "warnings": warnings,
        "conflicts": conflicts,
        "budget": {
            "used_bytes": used_bytes,
            "limit_bytes": budget_bytes,
            "items_considered": len(memories),
            "items_included": len(selected),
        },
        "limitation": RECALL_LIMITATION,
    }


def _conflicts(selected: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for memory in selected:
        scope = memory["scope"]
        key = (
            scope["kind"],
            scope.get("profile"),
            scope.get("value"),
            memory["title"].strip().lower(),
        )
        groups.setdefault(key, []).append(memory)
    conflicts = []
    for key in sorted(groups):
        members = groups[key]
        hashes = {member["content_sha256"] for member in members}
        if len(members) > 1 and len(hashes) > 1:
            conflicts.append(
                {
                    "scope_kind": key[0],
                    "scope_value": key[2],
                    "profile": key[1],
                    "title": members[0]["title"],
                    "memory_ids": sorted(member["memory_id"] for member in members),
                    "resolution": (
                        "not resolved automatically: review both memories and "
                        "retract or supersede the one that no longer applies"
                    ),
                }
            )
    return conflicts


def memory_context_items(
    selected: Sequence[dict], *, as_of: str | None = None
) -> list[ContextItem]:
    """Convert selected memory snapshots into pack context items.

    Every record is re-validated here rather than trusted because a caller
    says so: the review state, trust label and verification must agree with
    the record's own recorded history (see
    :func:`aperio.memstore.resolve_trust`), and validity is evaluated again
    against ``as_of`` (today in UTC by default) instead of carrying whatever
    the caller last computed. A private memory is always marked local-only,
    because a pack is a shareable artifact.

    The item carries the record's **whole review history** and its declared
    proposer, so the pack can re-derive the same authority from the same
    evidence instead of believing a label. Passing more than ``as_of`` is not
    needed: the state, its history and its trust contract are the record's.

    Records that were retracted or superseded are refused: they are recorded
    as no longer knowledge, so offering one as context would be a mistake no
    caller should be able to make silently. A record that is merely outside
    its validity window is still converted; the pack excludes it with an
    explicit reason so the exclusion stays visible.
    """

    evaluated_on = evaluate_on(as_of)
    items = []
    for memory in selected:
        if not isinstance(memory, Mapping):
            raise ValidationError("selected memories must be memory objects")
        resolved = resolve_trust(memory, label="memory")
        if resolved["recorded_state"] in INACTIVE_STATES:
            raise ValidationError(
                f"memory {memory.get('memory_id')!r} is "
                f"{resolved['recorded_state']}; a retracted or superseded "
                "memory cannot be offered as context"
            )
        validity = memory_validity(memory, as_of=evaluated_on)
        detail = {
            "memory_id": memory["memory_id"],
            "memory_type": memory.get("type"),
            "state": resolved["state"],
            "recorded_state": resolved["recorded_state"],
            "trust": resolved["trust"],
            "proposer": memory.get("actor"),
            "review_history": _review_history(memory),
            "scope": memory["scope"],
            "source": memory.get("source"),
            "tags": list(memory.get("tags", ())),
            "evidence": list(memory.get("evidence", ())),
            "verification_evidence": list(
                resolved["verification"].get("evidence") or []
            ),
            "verification_reviewer": resolved["verification"].get("reviewer"),
            "reviewer_distinct_from_proposer": resolved["verification"].get(
                "reviewer_distinct_from_proposer"
            ),
            "independence": resolved["verification"].get("independence"),
            "verification_reasons": list(
                resolved["verification"].get("reasons") or []
            ),
            "valid_until": memory.get("valid_until"),
            "valid_from": memory.get("valid_from"),
            "validity": validity,
            "sensitivity": memory.get("sensitivity", "internal"),
            "content_sha256": memory["content_sha256"],
            "reviewed": resolved["state"] in ("attested", "verified"),
            "verification_basis": resolved["verification"]["basis"],
        }
        items.append(
            ContextItem(
                source="memory",
                source_ref=f"memory:{memory['memory_id']}",
                kind="memory",
                title=memory["title"],
                content=memory["content"],
                trust=resolved["trust"],
                origin="memory-store",
                method="operator-selected-memory",
                observed_at=memory.get("updated_at"),
                sensitivity=(
                    "local_only"
                    if memory.get("sensitivity") == "private"
                    else "shareable"
                ),
                detail=detail,
            )
        )
    return items


def _review_history(memory: Mapping) -> list[dict]:
    """The review events a reader needs, in the shape the pack re-derives from.

    Only the three fields that carry meaning are copied: the transition, its
    evidence and who recorded it. The pack validates them again with the same
    derivation the store uses.
    """

    history = memory.get("history")
    if not isinstance(history, list):
        return []
    events = []
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        to_state = entry.get("to_state")
        if not isinstance(to_state, str) or to_state not in MEMORY_STATES:
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


__all__ = [
    "MAX_RECALL_ITEMS",
    "RECALL_LIMITATION",
    "memory_context_items",
    "recall_memories",
]
