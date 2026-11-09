"""Time applicability: UTC default evaluation, inclusive bounds, determinism.

The contract says validity is evaluated against today's **UTC** date. These
tests pin that against the local system timezone, the UTC midnight boundary,
and explicit historical dates, and confirm that no stored record is mutated
because time passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aperio.errors import ValidationError
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore, evaluate_on, memory_validity

BASE = {
    "type": "decision",
    "title": "Pricing rule",
    "content": "Price at 45 units per unit.",
    "scope": {"kind": "global"},
    "tags": ["pricing"],
}


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def test_default_evaluation_is_utc_not_local_timezone(tmp_path: Path) -> None:
    script = (
        "import json\n"
        "from aperio.memstore import evaluate_on, memory_validity\n"
        "print(json.dumps({\n"
        "  'default': evaluate_on(None),\n"
        "  'expired': memory_validity({'valid_from': None,"
        " 'valid_until': '2020-01-01'})['reason'],\n"
        "}))\n"
    )
    results = {}
    for zone in ("Pacific/Kiritimati", "Etc/GMT+12", "UTC"):
        environment = {**os.environ, "TZ": zone}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        results[zone] = json.loads(completed.stdout)
    assert len({value["default"] for value in results.values()}) == 1, results
    assert all(value["expired"] == "expired" for value in results.values())


def test_utc_midnight_boundary_is_inclusive(monkeypatch) -> None:
    import aperio.memstore as memstore_module

    monkeypatch.setattr(
        memstore_module, "_utc_today", lambda: "2026-03-01"
    )
    assert evaluate_on(None) == "2026-03-01"
    # The final day still counts.
    assert memory_validity({"valid_until": "2026-03-01"})["current"] is True
    assert memory_validity({"valid_until": "2026-02-28"})["current"] is False
    # The first day still counts.
    assert memory_validity({"valid_from": "2026-03-01"})["current"] is True
    assert memory_validity({"valid_from": "2026-03-02"})["current"] is False


def test_recall_default_follows_the_utc_date(monkeypatch, tmp_path: Path) -> None:
    import aperio.memstore as memstore_module
    import aperio.memrecall as memrecall_module

    monkeypatch.setattr(memstore_module, "_utc_today", lambda: "2026-06-15")
    monkeypatch.setattr(memrecall_module, "evaluate_on", lambda value=None: "2026-06-15")
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": "2026-06-14"})
    store.propose({**BASE, "title": "Open rule", "content": "45 until measured"})

    selection = recall_memories(
        store.snapshots(as_of="2026-06-15")["memories"], objective="pricing"
    )
    assert [m["title"] for m in selection["selected"]] == ["Open rule"]
    assert selection["excluded"]["by_reason"] == {"expired": 1}


def test_passing_time_does_not_mutate_the_store(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "valid_until": "2026-01-01"})["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    before = store.path.read_bytes()

    assert store.snapshots(as_of="2030-01-01")["memories"][memory["memory_id"]][
        "state"
    ] == "expired"
    assert store.path.read_bytes() == before
    # Inside its window the same raw record is current again.
    inside = store.snapshots(as_of="2025-06-01")["memories"][memory["memory_id"]]
    assert inside["state"] == "attested"
    assert inside["recorded_state"] == "attested"


def test_historical_as_of_is_not_review_state_reconstruction(tmp_path: Path) -> None:
    """``as_of`` moves the clock for validity only, never rewinds reviews."""

    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "accept", actor="operator", note="today")

    past = store.snapshots(as_of="2020-01-01")["memories"][memory["memory_id"]]
    assert past["state"] == "attested"
    assert past["history_entries"] == 1
    assert past["validity"]["evaluated_on"] == "2020-01-01"


@pytest.mark.parametrize(
    "value", [None, "", "2026-1-1", "2026-13-01", "26-01-01", "2026-01-01T00:00:00Z"]
)
def test_invalid_evaluation_dates_are_refused(tmp_path: Path, value) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    if value is None:
        assert evaluate_on(None)  # the default is allowed
        return
    with pytest.raises(ValidationError, match="ISO date"):
        evaluate_on(value)
    with pytest.raises(ValidationError, match="ISO date"):
        store.snapshots(as_of=value)


def test_recall_is_deterministic_for_a_fixed_utc_date(monkeypatch, tmp_path: Path) -> None:
    import aperio.memstore as memstore_module

    monkeypatch.setattr(memstore_module, "_utc_today", lambda: "2026-05-05")
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": "2026-05-04"})
    store.propose({**BASE, "title": "Open rule", "content": "45 until measured"})
    first = recall_memories(store.snapshots()["memories"], objective="pricing")
    second = recall_memories(store.snapshots()["memories"], objective="pricing")
    assert first == second
    assert first["validity"]["evaluated_on"] == "2026-05-05"


def test_pack_re_derives_validity_instead_of_accepting_a_stale_flag(
    tmp_path: Path,
) -> None:
    """Defence in depth: the pack evaluates the window, not a cached answer."""

    from aperio.context_pack import build_context_pack
    from aperio.memrecall import memory_context_items

    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": "2026-01-01"})
    stale = memory_context_items(
        list(store.snapshots(as_of="2026-06-01")["memories"].values()),
        as_of="2026-06-01",
    )
    # The caller re-labels the item as current at a later date; the pack
    # ignores that opinion and re-evaluates the stored window.
    stale[0].detail["validity"] = {
        "evaluated_on": "2026-06-01",
        "current": True,
        "reason": None,
    }
    document = build_context_pack(
        "pricing review", memory_items=stale, memory_as_of="2026-06-02"
    )
    assert document["content"]["excluded"]["by_reason"] == {
        "memory_not_current": 1
    }
    assert document["content"]["context_items"] == []

    current = memory_context_items(
        list(store.snapshots(as_of="2025-06-01")["memories"].values()),
        as_of="2025-06-01",
    )
    document = build_context_pack(
        "pricing review", memory_items=current, memory_as_of="2025-06-01"
    )
    assert [
        item["title"] for item in document["content"]["context_items"]
    ] == ["Pricing rule"]
