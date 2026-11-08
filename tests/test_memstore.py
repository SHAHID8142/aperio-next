"""Memory store tests: schema boundaries, review states, and data safety."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aperio.errors import PathError, StateError, ValidationError
from aperio.memstore import (
    MAX_CONTENT_BYTES,
    MAX_MEMORIES_PER_STORE,
    MAX_STORE_BYTES,
    MEMORY_SCHEMA_VERSION,
    MEMORY_STORE_FORMAT,
    MemoryStore,
    canonical_memory_bytes,
    memory_content_sha256,
    line_sha256,
    memory_identity,
    memory_trust,
    validate_memory_record,
)

BASE = {
    "type": "requirement",
    "title": "Primary brand colour",
    "content": "Primary brand colour is deep green #0B5D3B.",
    "scope": {"kind": "global"},
    "tags": ["brand", "colour"],
    "evidence": ["brand guide v3"],
}


def store_at(tmp_path: Path, name: str = "store.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


# ---------------------------------------------------------------------------
# record schema
# ---------------------------------------------------------------------------


def test_propose_creates_a_store_and_a_proposed_memory(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    result = store.propose(BASE)
    assert result["created"] is True
    memory = result["memory"]
    assert memory["state"] == "proposed"
    assert memory["trust"] == "proposed"
    assert memory["memory_id"] == memory_identity(BASE)
    lines = store.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["memory_store_format"] == MEMORY_STORE_FORMAT
    assert header["memory_schema_version"] == MEMORY_SCHEMA_VERSION
    assert len(lines) == 2


def test_store_is_created_with_owner_only_permissions(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_identical_content_is_not_stored_twice(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.propose(BASE)
    second = store.propose(BASE)
    assert first["created"] is True
    assert second["created"] is False
    assert second["duplicate"] is True
    assert second["memory"]["memory_id"] == first["memory"]["memory_id"]
    assert len(store.read()["records"]) == 1


def test_identity_is_content_derived_and_store_independent(tmp_path: Path) -> None:
    left = store_at(tmp_path, "left.mem").propose(BASE)["memory"]
    right = store_at(tmp_path, "right.mem").propose(
        {**BASE, "created_at": "2030-01-01T00:00:00Z", "actor": "someone"}
    )["memory"]
    assert left["memory_id"] == right["memory_id"]
    assert left["content_sha256"] == right["content_sha256"]


def test_identity_excludes_state_and_review_metadata(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    canonical_before = canonical_memory_bytes(
        {**BASE, "scope": {"kind": "global", "value": None, "profile": None},
         "evidence": ["brand guide v3"]}
    )
    reviewed = store.review(memory["memory_id"], "accept", actor="operator")
    assert reviewed["state"] == "attested"
    assert reviewed["memory_id"] == memory["memory_id"]
    assert reviewed["content_sha256"] == memory["content_sha256"]
    assert canonical_before


def test_content_hash_covers_content_only(tmp_path: Path) -> None:
    first = memory_content_sha256(BASE)
    changed = memory_content_sha256({**BASE, "content": BASE["content"] + " "})
    assert first != changed
    assert memory_content_sha256({**BASE, "tags": list(BASE["tags"])}) == first


@pytest.mark.parametrize(
    "bad",
    [None, [], "text", 42, True],
    ids=["null", "list", "string", "number", "bool"],
)
def test_record_must_be_an_object(bad) -> None:
    with pytest.raises(ValidationError):
        validate_memory_record(bad)


@pytest.mark.parametrize(
    "field, value",
    [
        ("type", "code_review"),
        ("type", None),
        ("title", ""),
        ("title", ["x"]),
        ("content", ""),
        ("content", 7),
        ("sensitivity", "secret"),
        ("tags", "brand"),
        ("tags", ["ok", 3]),
        ("scope", {"kind": "galaxy"}),
        ("scope", "global"),
        ("source", {"kind": "robot"}),
        ("valid_from", "2026-13-01"),
        ("valid_until", "not-a-date"),
        ("supersedes", "MEM-zzzz"),
        ("memory_schema_version", 2),
    ],
)
def test_invalid_fields_are_refused(field, value) -> None:
    with pytest.raises(ValidationError):
        validate_memory_record({**BASE, field: value})


def test_unknown_keys_are_refused_at_every_level() -> None:
    with pytest.raises(ValidationError, match="unknown keys"):
        validate_memory_record({**BASE, "confidence": 0.9})
    with pytest.raises(ValidationError, match="unknown keys"):
        validate_memory_record({**BASE, "scope": {"kind": "global", "x": 1}})
    with pytest.raises(ValidationError, match="unknown keys"):
        validate_memory_record({**BASE, "source": {"kind": "operator", "x": 1}})


def test_field_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError, match="at most"):
        validate_memory_record({**BASE, "title": "x" * 201})
    with pytest.raises(ValidationError, match="at most"):
        validate_memory_record({**BASE, "content": "x" * (MAX_CONTENT_BYTES + 1)})
    with pytest.raises(ValidationError, match="at most 12 items"):
        validate_memory_record({**BASE, "tags": [f"t{i}" for i in range(13)]})
    with pytest.raises(ValidationError, match="at most 12 items"):
        validate_memory_record({**BASE, "evidence": [f"e{i}" for i in range(13)]})


def test_control_characters_and_foreign_line_separators_are_refused() -> None:
    with pytest.raises(ValidationError):
        validate_memory_record({**BASE, "content": "a\x00b"})
    with pytest.raises(ValidationError):
        validate_memory_record({**BASE, "content": "a\u2028b"})


def test_title_must_be_single_line() -> None:
    with pytest.raises(ValidationError, match="single line"):
        validate_memory_record({**BASE, "title": "line one\nline two"})


def test_valid_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="valid_from"):
        validate_memory_record(
            {**BASE, "valid_from": "2026-05-01", "valid_until": "2026-04-01"}
        )


def test_scope_requirements_are_enforced() -> None:
    with pytest.raises(ValidationError, match="requires scope.profile"):
        validate_memory_record({**BASE, "scope": {"kind": "profile"}})
    with pytest.raises(ValidationError, match="requires scope.value"):
        validate_memory_record({**BASE, "scope": {"kind": "objective"}})
    with pytest.raises(ValidationError, match="must not carry"):
        validate_memory_record(
            {**BASE, "scope": {"kind": "global", "value": "x"}}
        )


def test_trust_mapping_covers_every_state() -> None:
    assert memory_trust("proposed") == "proposed"
    assert memory_trust("attested") == "attested"
    assert memory_trust("verified") == "verified"
    assert memory_trust("retracted") == "proposed"
    assert memory_trust("superseded") == "proposed"
    with pytest.raises(ValidationError):
        memory_trust("certain")


# ---------------------------------------------------------------------------
# review transitions
# ---------------------------------------------------------------------------


def test_accept_then_verify_records_history(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    accepted = store.review(memory["memory_id"], "accept", actor="operator")
    verified = store.review(
        memory["memory_id"],
        "verify",
        actor="reviewer",
        note="checked against brand guide v3",
        evidence=["brand guide v3, page 4"],
    )
    assert accepted["state"] == "attested"
    assert verified["state"] == "verified"
    assert [event["action"] for event in verified["history"]] == [
        "accept",
        "verify",
    ]
    assert verified["history"][-1]["evidence"] == ["brand guide v3, page 4"]


def test_history_is_append_only(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    before = store.path.read_bytes()
    store.review(memory["memory_id"], "accept", actor="operator")
    after = store.path.read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


@pytest.mark.parametrize(
    "action, setup, expected_state",
    [
        ("retract", ["accept"], "retracted"),
        ("supersede", ["accept"], "superseded"),
        ("verify", [], "verified"),
        ("verify", ["accept"], "verified"),
    ],
)
def test_allowed_transitions(tmp_path: Path, action, setup, expected_state) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    for step in setup:
        store.review(memory["memory_id"], step, actor="operator")
    updated = store.review(memory["memory_id"], action, actor="operator")
    assert updated["state"] == expected_state


def test_reinstate_returns_a_record_to_proposed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "retract", reason="stale")
    reinstated = store.review(memory["memory_id"], "reinstate", actor="operator")
    assert reinstated["state"] == "proposed"
    assert reinstated["revision"] == 3


@pytest.mark.parametrize(
    "action, setup",
    [
        ("accept", ["accept"]),
        ("verify", ["verify"]),
        ("retract", ["retract"]),
        ("reinstate", []),
        ("accept", ["retract"]),
    ],
)
def test_disallowed_transitions_fail_closed(tmp_path: Path, action, setup) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    for step in setup:
        store.review(memory["memory_id"], step, actor="operator")
    with pytest.raises(StateError):
        store.review(memory["memory_id"], action, actor="operator")


def test_unknown_action_and_unknown_memory_are_refused(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    with pytest.raises(ValidationError):
        store.review(memory["memory_id"], "promote")
    with pytest.raises(StateError):
        store.review("MEM-000000000000", "accept")
    with pytest.raises(ValidationError):
        store.review("not-a-memory-id", "accept")


def test_review_requires_no_actor_but_records_attribution(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    updated = store.review(memory["memory_id"], "accept")
    assert updated["history"][-1]["actor"] is None


# ---------------------------------------------------------------------------
# durability and damaged input
# ---------------------------------------------------------------------------


def test_missing_store_error_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="mem-propose"):
        store_at(tmp_path).read()


def test_symlinked_store_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.mem"
    store_at(tmp_path, "real.mem").propose(BASE)
    link = tmp_path / "link.mem"
    os.symlink(real, link)
    with pytest.raises(PathError, match="symlink"):
        MemoryStore(link).read()


def test_damaged_lines_are_reported_and_skipped(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    state = store.snapshots()
    assert list(state["memories"]) == [memory["memory_id"]]
    assert len(state["damaged"]) == 1
    assert "not valid JSON" in state["damaged"][0]["reason"]


def test_tampered_content_is_detected(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    lines = store.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["content"] = "A different claim entirely."
    lines[1] = json.dumps(entry)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = store.snapshots()
    assert state["memories"] == {}
    assert "hash chain" in state["damaged"][0]["reason"]


def test_unknown_store_format_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.mem"
    header = {"record": "store", "memory_store_format": "other"}
    header["chain_sha256"] = line_sha256(header, None)
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="memory-store"):
        MemoryStore(path).read()


def test_unsupported_store_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.mem"
    header = {
        "record": "store",
        "memory_store_format": MEMORY_STORE_FORMAT,
        "format_version": 9,
        "memory_schema_version": MEMORY_SCHEMA_VERSION,
    }
    header["chain_sha256"] = line_sha256(header, None)
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="unsupported format version"):
        MemoryStore(path).read()


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "missing" / "store.mem")
    with pytest.raises(PathError, match="parent"):
        store.propose(BASE)


def test_store_size_cap_is_enforced(tmp_path: Path, monkeypatch) -> None:
    import aperio.memstore as module

    store = store_at(tmp_path)
    store.propose(BASE)
    monkeypatch.setattr(module, "MAX_STORE_BYTES", 100)
    with pytest.raises(StateError, match="(would exceed|larger than)"):
        store.propose({**BASE, "title": "Second requirement"})


def test_record_count_cap_is_enforced(tmp_path: Path, monkeypatch) -> None:
    import aperio.memstore as module

    monkeypatch.setattr(module, "MAX_MEMORIES_PER_STORE", 1)
    store = store_at(tmp_path)
    store.propose(BASE)
    with pytest.raises(StateError, match="split it into separate stores"):
        store.propose({**BASE, "title": "Another requirement"})
    assert MAX_MEMORIES_PER_STORE == 2000


def test_expiry_is_derived_from_valid_until(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(
        {**BASE, "valid_from": "2020-01-01", "valid_until": "2021-01-01"}
    )["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    state = store.snapshots(as_of="2026-01-01")["memories"][memory["memory_id"]]
    assert state["state"] == "expired"
    assert "2021-01-01" in state["expired_reason"]
    fresh = store.snapshots(as_of="2020-06-01")["memories"][memory["memory_id"]]
    assert fresh["state"] == "attested"


def test_declared_state_is_preserved_for_review(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(
        {**BASE, "source": {"kind": "import", "ref": "exchange-v1"},
         "declared_state": "verified"}
    )["memory"]
    assert memory["state"] == "proposed"
    assert memory["trust"] == "proposed"
    assert memory["declared_state"] == "verified"


def test_store_max_bytes_constant_is_documented() -> None:
    assert MAX_STORE_BYTES == 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_concurrent_proposals_never_lose_or_corrupt_records(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    script = (
        "import sys\n"
        "from aperio.memstore import MemoryStore\n"
        "store = MemoryStore(sys.argv[1])\n"
        "result = store.propose({'type': 'observation',\n"
        "  'title': sys.argv[2], 'content': 'content ' + sys.argv[2],\n"
        "  'tags': ['race']})\n"
        "print(result['memory']['memory_id'])\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store.path), f"item-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(store.__class__.__module__.replace(".", "/")).parent)},
        )
        for index in range(6)
    ]
    outputs = [process.communicate(timeout=120) for process in processes]
    assert all(process.returncode == 0 for process in processes), [
        out[1] for out in outputs
    ]
    state = store.snapshots()
    assert state["damaged"] == []
    assert len(state["memories"]) == 6
    assert not store.lock_path.exists()


def test_concurrent_identical_proposals_yield_one_record(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    script = (
        "from aperio.memstore import MemoryStore\n"
        "MemoryStore(\n"
        f"    {str(store.path)!r}\n"
        ").propose({'type': 'lesson', 'title': 'Same', 'content': 'Same body.'})\n"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script]) for _ in range(5)
    ]
    for process in processes:
        assert process.wait(timeout=120) == 0
    state = store.snapshots()
    assert state["damaged"] == []
    assert len(state["memories"]) == 1


# ---------------------------------------------------------------------------
# append-only history integrity
# ---------------------------------------------------------------------------


def test_consistent_replay_requires_untouched_lines(tmp_path: Path) -> None:
    """A line edited to stay internally consistent still breaks the chain."""

    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    lines = store.path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["to_state"] = "verified"
    event["action"] = "verify"
    lines[-1] = json.dumps({k: v for k, v in event.items() if k != "chain_sha256"})
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "hash chain" in store.snapshots()["damaged"][0]["reason"]


def test_tampered_transition_chain_is_reported(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    lines = store.path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["from_state"] = "proposed"
    event["to_state"] = "verified"
    event["action"] = "verify"
    lines[-1] = json.dumps(event)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = store.snapshots()
    assert state["memories"][memory["memory_id"]]["state"] == "proposed"
    assert "hash chain" in state["damaged"][0]["reason"]


def test_transition_revision_gap_is_reported(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    lines = store.path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["revision"] = 7
    lines[-1] = json.dumps(event)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "hash chain" in store.snapshots()["damaged"][0]["reason"]


def test_forged_transition_action_pair_is_rejected(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    store.review(memory["memory_id"], "accept", actor="operator")
    lines = store.path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["to_state"] = "superseded"
    lines[-1] = json.dumps(event)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "hash chain" in store.snapshots()["damaged"][0]["reason"]


# ---------------------------------------------------------------------------
# forged identifiers
# ---------------------------------------------------------------------------


def test_forged_memory_id_in_a_store_is_detected(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    lines = store.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["memory_id"] = "MEM-00000000dead"
    entry["chain_sha256"] = line_sha256(entry, json.loads(lines[0])["chain_sha256"])
    lines[1] = json.dumps(entry)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = store.snapshots()
    assert state["memories"] == {}
    assert "memory_id does not match" in state["damaged"][0]["reason"]


def test_caller_supplied_memory_id_must_match_content(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(ValidationError, match="derived from the memory content"):
        store.propose({**BASE, "memory_id": "MEM-00000000dead"})


# ---------------------------------------------------------------------------
# recovery rules
# ---------------------------------------------------------------------------


def test_interrupted_write_is_refused_instead_of_extending_damage(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    memory = store.propose(BASE)["memory"]
    with store.path.open("ab") as handle:
        handle.write(b'{"record": "transition", "memory_id": ')
    with pytest.raises(StateError, match="incomplete line"):
        store.propose({**BASE, "title": "After the interruption"})
    # The complete record is still readable, and nothing was rewritten.
    state = store.snapshots()
    assert memory["memory_id"] in state["memories"]
    assert state["damaged"]


def test_broken_chain_blocks_new_writes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.propose(BASE)
    lines = store.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["title"] = "Edited title"
    lines[1] = json.dumps(entry)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="damaged line"):
        store.propose({**BASE, "title": "New requirement"})


def test_memory_store_inside_project_state_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".aperio").mkdir(parents=True)
    with pytest.raises(PathError, match="inside '.aperio/'"):
        MemoryStore(project / ".aperio" / "mem.store")


def test_symlinked_project_directory_is_also_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".aperio").mkdir(parents=True)
    link = tmp_path / "innocent"
    os.symlink(project, link)
    with pytest.raises(PathError, match="inside '.aperio/'"):
        MemoryStore(link / ".aperio" / "mem.store")
