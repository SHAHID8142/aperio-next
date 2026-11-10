"""M3 release gate: verification prerequisites, time validity, safe appends.

Three P1 defect classes are pinned here:

* **P1-A** — ``verify`` used to be reachable with no evidence and no reviewer,
  which let an unsupported claim be labelled verified by invoking the command.
* **P1-B** — expiry was only evaluated when an optional ``as_of`` was passed,
  so ordinary recall never applied ``valid_until``/``valid_from`` at all.
* **P1-C** — appends issued one unchecked ``os.write`` and opened the store
  without a no-follow flag or an identity check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from aperio.errors import PathError, StateError, ValidationError
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore

BASE = {
    "type": "observation",
    "title": "Churn assumption",
    "content": "Assume 3% monthly churn until a measured figure exists.",
    "scope": {"kind": "global"},
    "tags": ["churn"],
}


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def text_lines(store: MemoryStore) -> list[str]:
    return store.path.read_text(encoding="utf-8").splitlines()


def today() -> date:
    # The product evaluates validity against *today in UTC* (documented in
    # docs/universal-memory.md); the test frame must match, or a record
    # intended as "expired" becomes current whenever the local date is a
    # day ahead of the UTC date (the trap this test fell into at the
    # local-midnight/UTC-midnight boundary).
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date()


def shift(days: int) -> str:
    return (today() + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# P1-A: verification requires evidence, a reviewer, and declared separation
# ---------------------------------------------------------------------------


def test_verify_without_evidence_fails_closed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "actor": "proposer"})["memory"]

    with pytest.raises(ValidationError, match="verification requires"):
        store.review(memory["memory_id"], "verify", actor="reviewer")

    # Nothing was appended, so the claim is still proposed and the history
    # holds no verification event that a later reader could trust.
    after = store.snapshots()["memories"][memory["memory_id"]]
    assert after["state"] == "proposed"
    assert after["revision"] == 1
    assert after["history"] == []
    assert len(text_lines(store)) == 2


def test_verify_without_reviewer_fails_closed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    with pytest.raises(ValidationError, match="verification requires"):
        store.review(
            memory["memory_id"], "verify", evidence=["Q1 cohort report"]
        )

    assert store.snapshots()["memories"][memory["memory_id"]]["state"] == "proposed"
    assert len(text_lines(store)) == 2


def test_verify_rejects_blank_evidence(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    with pytest.raises(
        ValidationError, match="verification requires|must not be empty"
    ):
        store.review(
            memory["memory_id"],
            "verify",
            actor="reviewer",
            evidence=["   ", ""],
        )
    assert store.snapshots()["memories"][memory["memory_id"]]["state"] == "proposed"


def test_verify_refuses_self_review_when_proposer_is_known(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "actor": "alice"})["memory"]

    with pytest.raises(ValidationError, match="separation of duties"):
        store.review(
            memory["memory_id"],
            "verify",
            actor="alice",
            evidence=["Q1 cohort report"],
        )

    after = store.snapshots()["memories"][memory["memory_id"]]
    assert after["state"] == "proposed"
    assert after["history"] == []


def test_verify_records_reviewer_and_evidence(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "actor": "alice"})["memory"]

    verified = store.review(
        memory["memory_id"],
        "verify",
        actor="bob",
        evidence=["Q1 cohort report", "finance reconciliation"],
        note="checked against the source data",
    )

    assert verified["state"] == "verified"
    assert verified["trust"] == "verified"
    assert verified["verification"] == {
        "basis": "evidence",
        "replayed_state": "verified",
        "evidence": ["Q1 cohort report", "finance reconciliation"],
        "reviewer": "bob",
        "reviewer_distinct_from_proposer": True,
        "independence": "declared-distinct-identifiers",
        "note": (
            "Declared identifiers only: Aperio cannot authenticate who ran "
            "the review, so this records separation of declared identities, "
            "not proven independence."
        ),
    }
    event = verified["history"][-1]
    assert event["action"] == "verify"
    assert event["actor"] == "bob"
    assert event["evidence"] == ["Q1 cohort report", "finance reconciliation"]


def test_accept_is_not_verification(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    accepted = store.review(memory["memory_id"], "accept", actor="operator")

    assert accepted["state"] == "attested"
    assert accepted["trust"] == "attested"
    assert accepted["verification"]["basis"] == "not-verified"
    assert accepted["verification"]["evidence"] == []


def test_legacy_verified_record_without_evidence_is_not_exaggerated(
    tmp_path: Path,
) -> None:
    """Histories written before the gate are readable but never trusted more."""

    store = store_at(tmp_path)
    memory = store.propose({**BASE, "actor": "alice"})["memory"]
    # Hand-write the pre-gate event exactly as 0.10.0 would have stored it.
    record = {
        "record": "transition",
        "memory_id": memory["memory_id"],
        "revision": 2,
        "at": "2026-01-02T00:00:00Z",
        "actor": "bob",
        "action": "verify",
        "from_state": "proposed",
        "to_state": "verified",
        "note": None,
        "evidence": [],
        "reason": None,
    }
    previous = json.loads(text_lines(store)[-1])["chain_sha256"]
    record["chain_sha256"] = __import__(
        "aperio.memstore", fromlist=["line_sha256"]
    ).line_sha256(record, previous)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

    legacy = store.snapshots()["memories"][memory["memory_id"]]
    # The stored state and event are reported as recorded, never rewritten.
    assert legacy["state"] == "verified"
    assert legacy["history"][-1]["action"] == "verify"
    # ...but no evidence-backed confidence is claimed anywhere downstream.
    assert legacy["verification"]["basis"] == "unrecorded"
    assert legacy["verification"]["downgraded"] is True
    assert legacy["trust"] == "attested"


def test_legacy_verified_record_can_be_reverified_with_evidence(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "actor": "alice"})["memory"]
    record = {
        "record": "transition",
        "memory_id": memory["memory_id"],
        "revision": 2,
        "at": "2026-01-02T00:00:00Z",
        "actor": None,
        "action": "verify",
        "from_state": "proposed",
        "to_state": "verified",
        "note": None,
        "evidence": [],
        "reason": None,
    }
    previous = json.loads(text_lines(store)[-1])["chain_sha256"]
    from aperio.memstore import line_sha256

    record["chain_sha256"] = line_sha256(record, previous)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

    upgraded = store.review(
        memory["memory_id"],
        "verify",
        actor="bob",
        evidence=["Q1 cohort report"],
    )
    assert upgraded["state"] == "verified"
    assert upgraded["trust"] == "verified"
    assert upgraded["verification"]["basis"] == "evidence"
    assert upgraded["verification"]["evidence"] == ["Q1 cohort report"]
    assert upgraded["revision"] == 3


def test_downstream_recall_and_pack_use_the_downgraded_trust(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    from aperio.memstore import line_sha256

    record = {
        "record": "transition",
        "memory_id": memory["memory_id"],
        "revision": 2,
        "at": "2026-01-02T00:00:00Z",
        "actor": "bob",
        "action": "verify",
        "from_state": "proposed",
        "to_state": "verified",
        "note": None,
        "evidence": [],
        "reason": None,
    }
    previous = json.loads(text_lines(store)[-1])["chain_sha256"]
    record["chain_sha256"] = line_sha256(record, previous)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

    selection = recall_memories(store.snapshots()["memories"], objective="churn")
    selected = selection["selected"][0]
    assert selected["state"] == "verified"
    assert selected["trust"] == "attested"
    assert selection["warnings"][0]["code"] in (
        "memory_verification_unrecorded",
        "memory_unreviewed_selected",
    )
    assert any(
        warning["code"] == "memory_verification_unrecorded"
        for warning in selection["warnings"]
    )


# ---------------------------------------------------------------------------
# P1-B: current-date applicability without --as-of
# ---------------------------------------------------------------------------


def test_recall_excludes_expired_memory_without_as_of(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(
        {**BASE, "valid_until": shift(-2), "actor": "alice"}
    )["memory"]
    store.review(memory["memory_id"], "accept", actor="alice")

    selection = recall_memories(
        store.snapshots()["memories"], objective="churn"
    )
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"expired": 1}


def test_recall_excludes_not_yet_valid_memory(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_from": shift(1)})

    selection = recall_memories(
        store.snapshots()["memories"], objective="churn"
    )
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"future": 1}


@pytest.mark.parametrize("state_action", ["none", "accept", "verify"])
def test_time_bounds_apply_to_every_active_state(
    tmp_path: Path, state_action: str
) -> None:
    store = store_at(tmp_path)
    memory = store.propose(
        {**BASE, "valid_until": shift(-2), "actor": "alice"}
    )["memory"]
    if state_action == "accept":
        store.review(memory["memory_id"], "accept", actor="alice")
    elif state_action == "verify":
        store.review(
            memory["memory_id"],
            "verify",
            actor="bob",
            evidence=["source check"],
        )

    selection = recall_memories(
        store.snapshots()["memories"], objective="churn"
    )
    assert selection["selected"] == []
    assert selection["excluded"]["by_reason"] == {"expired": 1}


def test_validity_window_is_inclusive_on_both_ends(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(
        {**BASE, "valid_from": today().isoformat(), "valid_until": today().isoformat()}
    )
    selection = recall_memories(
        store.snapshots()["memories"], objective="churn"
    )
    assert [m["memory_id"] for m in selection["selected"]]
    assert selection["excluded"]["by_reason"] == {}


def test_as_of_restores_historically_valid_memory(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_from": shift(-30), "valid_until": shift(-10)})

    now_view = recall_memories(
        store.snapshots()["memories"], objective="churn"
    )
    assert now_view["selected"] == []
    assert now_view["excluded"]["by_reason"] == {"expired": 1}

    historical = recall_memories(
        store.snapshots(as_of=shift(-20))["memories"],
        objective="churn",
        as_of=shift(-20),
    )
    assert [m["memory_id"] for m in historical["selected"]]
    assert historical["as_of"] == shift(-20)


@pytest.mark.parametrize(
    "bad", ["2026-13-01", "2026-02-30", "yesterday", "20260101", "", "2026-1-1"]
)
def test_invalid_as_of_is_refused(tmp_path: Path, bad: str) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    with pytest.raises(ValidationError, match="as_of|as-of|date"):
        recall_memories(
            store.snapshots()["memories"], objective="churn", as_of=bad
        )
    with pytest.raises(ValidationError, match="as_of|as-of|date"):
        store.snapshots(as_of=bad)


def test_recall_report_is_deterministic_for_a_fixed_date(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": shift(-2)})
    store.propose({**BASE, "title": "Open item", "content": "still current"})
    first = recall_memories(
        store.snapshots(as_of=shift(0))["memories"],
        objective="churn",
        as_of=shift(0),
    )
    second = recall_memories(
        store.snapshots(as_of=shift(0))["memories"],
        objective="churn",
        as_of=shift(0),
    )
    assert first == second
    assert first["excluded"]["by_reason"] == {"expired": 1}
    assert first["validity"] == {
        "evaluated_on": shift(0),
        "rule": "valid_from <= date <= valid_until (both ends inclusive)",
    }


# ---------------------------------------------------------------------------
# P1-C: complete appends, no-follow open, identity verification
# ---------------------------------------------------------------------------


def _patch_write(monkeypatch, behaviour):
    """Patch ``os.write`` for the memory store only."""

    import aperio.memstore as memstore_module

    real_write = os.write
    calls = {"count": 0}

    def fake_write(fd: int, data: bytes) -> int:
        calls["count"] += 1
        return behaviour(real_write, fd, data, calls["count"])

    monkeypatch.setattr(memstore_module, "_os_write", fake_write)
    return calls


def test_short_writes_are_retried_until_the_event_is_complete(
    tmp_path: Path, monkeypatch
) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    calls = _patch_write(
        monkeypatch, lambda real, fd, data, n: real(fd, data[:7])
    )
    review = store.review(memory["memory_id"], "accept", actor="operator")

    assert review["state"] == "attested"
    assert calls["count"] > 1
    state = store.snapshots()
    assert state["damaged"] == []
    assert state["memories"][memory["memory_id"]]["state"] == "attested"


def test_zero_byte_write_fails_closed_without_reporting_success(
    tmp_path: Path, monkeypatch
) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    _patch_write(monkeypatch, lambda real, fd, data, n: 0)
    with pytest.raises(StateError, match="could not write"):
        store.review(memory["memory_id"], "accept", actor="operator")

    monkeypatch.undo()
    state = store.snapshots()
    assert state["memories"][memory["memory_id"]]["state"] == "proposed"
    assert state["damaged"] == []


def test_interrupted_write_reports_failure_and_blocks_further_appends(
    tmp_path: Path, monkeypatch
) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    def half_then_fail(real, fd, data, n):
        if n == 1:
            return real(fd, data[: len(data) // 2])
        raise OSError(5, "Input/output error")

    _patch_write(monkeypatch, half_then_fail)
    with pytest.raises(StateError, match="could not write"):
        store.review(memory["memory_id"], "accept", actor="operator")
    monkeypatch.undo()

    # The store still opens for inspection and names the damage...
    state = store.snapshots()
    assert state["damaged"], "the partial line must be reported, not hidden"
    assert state["memories"][memory["memory_id"]]["state"] == "proposed"
    # ...and nothing else is appended on top of an unrecoverable tail.
    with pytest.raises(StateError, match="incomplete line"):
        store.propose({**BASE, "title": "Another memory"})


def test_append_refuses_a_symlink_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim.mem"
    victim.write_text("keep me\n", encoding="utf-8")
    store = store_at(tmp_path)
    store.propose(BASE)
    store.path.unlink()
    os.symlink(victim, store.path)

    with pytest.raises(PathError, match="symlink"):
        store.propose({**BASE, "title": "Second memory"})
    assert victim.read_text(encoding="utf-8") == "keep me\n"


def test_append_detects_a_store_replaced_between_check_and_open(
    tmp_path: Path, monkeypatch
) -> None:
    import aperio.memstore as memstore_module

    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]

    real_open = os.open
    victim = tmp_path / "attacker.mem"
    victim.write_text("unrelated content\n", encoding="utf-8")

    def swapping_open(path, flags, *args, **kwargs):
        if str(path) == str(store.path) and not kwargs.pop("_done", False):
            # Simulate another process swapping the file right after the
            # store was validated but before the descriptor is obtained.
            os.replace(victim, store.path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(memstore_module.os, "open", swapping_open)
    with pytest.raises(PathError, match="changed while"):
        store.review(memory["memory_id"], "accept", actor="operator")
    monkeypatch.undo()
    # The swapped-in file was rejected before any bytes were written to it.
    assert store.path.read_text(encoding="utf-8") == "unrelated content\n"


def test_conflicting_reviews_are_serialised_across_processes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    script = (
        "import sys\n"
        "from aperio.memstore import MemoryStore\n"
        "store = MemoryStore(sys.argv[1])\n"
        "try:\n"
        "    store.review(sys.argv[2], 'accept', actor='operator')\n"
        "    print('ok')\n"
        "except Exception as error:\n"
        "    print('error:' + type(error).__name__)\n"
    )
    results = []
    for _ in range(4):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(store.path), memory["memory_id"]],
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(completed.stdout.strip())
    assert results.count("ok") == 1
    state = store.snapshots()
    assert state["damaged"] == []
    assert state["memories"][memory["memory_id"]]["state"] == "attested"
    assert state["memories"][memory["memory_id"]]["revision"] == 2


def test_removing_a_complete_final_record_is_not_detectable_without_an_anchor(
    tmp_path: Path,
) -> None:
    """Documented limit: a chain proves continuity, never completeness."""

    store = store_at(tmp_path)
    store.propose(BASE)
    memory = store.propose({**BASE, "title": "Second"})["memory"]
    first_id = next(iter(store.snapshots()["memories"]))
    lines = text_lines(store)
    # Drop the *final complete* line: no header, no chain break, no damage.
    store.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    state = store.snapshots()
    assert state["damaged"] == []
    assert list(state["memories"]) == [first_id]
    assert first_id != memory["memory_id"]


# ---------------------------------------------------------------------------
# CLI, pack, and export integration for time validity
# ---------------------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aperio", *args],
        capture_output=True,
        text=True,
    )


def test_cli_recall_applies_validity_without_as_of(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": shift(-2)})
    store.propose({**BASE, "title": "Open item", "content": "current"})

    completed = run_cli("mem-recall", str(store.path), "--objective", "churn")
    assert completed.returncode == 0, completed.stderr
    assert "excluded" not in completed.stdout or "expired=1" in completed.stdout
    assert "Open item" in completed.stdout

    historic = run_cli(
        "mem-recall",
        str(store.path),
        "--objective",
        "churn",
        "--as-of",
        shift(-5),
    )
    assert historic.returncode == 0, historic.stderr
    assert "Churn assumption" in historic.stdout


def test_cli_bad_as_of_is_a_single_line_error(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    completed = run_cli(
        "mem-recall", str(store.path), "--objective", "churn", "--as-of", "nope"
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("aperio: error: ")
    assert "Traceback" not in completed.stderr


def test_context_pack_drops_expired_memory_and_keeps_current(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(
        {**BASE, "title": "Old churn rule", "valid_until": shift(-2)}
    )
    store.propose({**BASE, "title": "Current churn rule", "content": "current"})

    completed = run_cli(
        "context-pack",
        "--objective",
        "churn review",
        "--memory-store",
        str(store.path),
    )
    assert completed.returncode == 0, completed.stderr
    assert "Current churn rule" in completed.stdout
    assert "Old churn rule" not in completed.stdout

    historic = run_cli(
        "context-pack",
        "--objective",
        "churn review",
        "--memory-store",
        str(store.path),
        "--memory-as-of",
        shift(-5),
    )
    assert "Old churn rule" in historic.stdout


def test_goal_pack_reports_time_exclusions(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": shift(-2)})
    goal = tmp_path / "goal.json"
    goal.write_text(
        json.dumps(
            {
                "objective": "Churn review",
                "intended_outcome": "A decision",
                "profile": "business",
            }
        ),
        encoding="utf-8",
    )
    completed = run_cli(
        "goal-pack",
        str(goal),
        "--memory-store",
        str(store.path),
        "--json",
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["content"]["memory"]["excluded"] == {"expired": 1}
    assert document["content"]["memory"]["memory_ids"] == []


def test_export_honours_current_applicability(tmp_path: Path) -> None:
    from aperio.memportable import export_memories

    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": shift(-2)})
    store.propose({**BASE, "title": "Open item", "content": "current"})

    current = export_memories(store)
    assert [entry["memory"]["title"] for entry in current["entries"]] == [
        "Open item"
    ]
    historic = export_memories(store, as_of=shift(-5))
    assert sorted(entry["memory"]["title"] for entry in historic["entries"]) == [
        "Churn assumption",
        "Open item",
    ]


# ---------------------------------------------------------------------------
# consistency audit findings corrected in the same cycle
# ---------------------------------------------------------------------------


def test_source_metadata_cannot_raise_trust(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for kind in ("operator", "workspace", "agent", "import"):
        memory = store.propose(
            {**BASE, "title": f"From {kind}", "content": f"{kind} claim", "source": {"kind": kind}}
        )["memory"]
        assert memory["state"] == "proposed"
        assert memory["trust"] == "proposed"
        assert memory["verification"]["basis"] == "not-verified"


def test_a_declared_state_cannot_raise_trust(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose({**BASE, "declared_state": "verified"})["memory"]
    assert memory["state"] == "proposed"
    assert memory["trust"] == "proposed"
    assert memory["declared_state"] == "verified"
    assert memory["verification"]["basis"] == "not-verified"


def test_identical_content_with_a_different_sensitivity_is_refused(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "sensitivity": "internal"})
    with pytest.raises(ValidationError, match="classification differs"):
        store.propose({**BASE, "sensitivity": "private"})
    memories = store.snapshots()["memories"]
    assert [memory["sensitivity"] for memory in memories.values()] == ["internal"]
    assert len(memories) == 1


def test_identical_content_with_a_different_validity_is_refused(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    store.propose({**BASE, "valid_until": shift(10)})
    with pytest.raises(ValidationError, match="classification differs"):
        store.propose({**BASE, "valid_until": shift(90)})


def test_import_reports_a_sensitivity_mismatch_as_a_conflict(
    tmp_path: Path,
) -> None:
    from aperio.memportable import export_memories, import_memories

    source = store_at(tmp_path, "source.mem")
    memory = source.propose({**BASE, "sensitivity": "private"})["memory"]
    document = export_memories(source, include_private=True)

    target = store_at(tmp_path, "target.mem")
    target.propose({**BASE, "sensitivity": "internal"})
    report = import_memories(target, document, max_sensitivity="private")
    assert report["duplicates"] == []
    assert report["conflicts"][0]["incoming_memory_id"] == memory["memory_id"]
    assert "different sensitivity" in report["conflicts"][0]["reason"]


def test_imported_record_never_carries_verification_state(tmp_path: Path) -> None:
    from aperio.memportable import export_memories, import_memories

    source = store_at(tmp_path, "source.mem")
    memory = source.propose({**BASE, "actor": "alice"})["memory"]
    source.review(
        memory["memory_id"], "verify", actor="bob", evidence=["audited report"]
    )
    document = export_memories(source)
    assert document["entries"][0]["declared_state"] == "verified"

    target = store_at(tmp_path, "target.mem")
    report = import_memories(target, document, actor="agent-b")
    [imported_id] = report["imported"]
    imported = target.snapshots()["memories"][imported_id]
    assert imported["state"] == "proposed"
    assert imported["verification"]["basis"] == "not-verified"
    assert imported["declared_state"] == "verified"


def test_export_excludes_not_yet_valid_memories(tmp_path: Path) -> None:
    from aperio.memportable import export_memories

    store = store_at(tmp_path)
    store.propose({**BASE, "title": "Future rule", "valid_from": shift(5)})
    store.propose({**BASE, "title": "Current rule", "content": "current"})

    current = export_memories(store)
    assert [entry["memory"]["title"] for entry in current["entries"]] == [
        "Current rule"
    ]
    planned = export_memories(store, as_of=shift(10))
    assert sorted(entry["memory"]["title"] for entry in planned["entries"]) == [
        "Current rule",
        "Future rule",
    ]
