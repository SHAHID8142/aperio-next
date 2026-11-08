"""Portable memory exchange tests: identity, trust, duplicates, conflicts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.errors import StateError, ValidationError
from aperio.memportable import (
    MAX_EXPORT_BYTES,
    MAX_EXPORT_RECORDS,
    MEMORY_EXCHANGE_FORMAT,
    MEMORY_EXCHANGE_FORMAT_VERSION,
    export_memories,
    import_memories,
    load_exchange_file,
    render_export,
    summarize_import,
    validate_exchange_document,
)
from aperio.memstore import MemoryStore


def make_store(tmp_path: Path, name: str = "a.mem") -> MemoryStore:
    return MemoryStore(tmp_path / name)


def seed(store: MemoryStore, **overrides) -> dict:
    record = {
        "type": "requirement",
        "title": "Brand colour",
        "content": "Primary brand colour is deep green.",
        "scope": {"kind": "objective", "value": "spring poster"},
        "tags": ["brand"],
        "evidence": ["brand guide v3"],
    }
    record.update(overrides)
    return store.propose(record)["memory"]


def exported(tmp_path: Path, **kwargs) -> tuple[MemoryStore, dict]:
    store = make_store(tmp_path)
    seed(store)
    return store, export_memories(store, **kwargs)


# ---------------------------------------------------------------------------
# export document
# ---------------------------------------------------------------------------


def test_export_document_shape_and_hash(tmp_path: Path) -> None:
    _, document = exported(tmp_path)
    assert document["format"] == MEMORY_EXCHANGE_FORMAT
    assert document["format_version"] == MEMORY_EXCHANGE_FORMAT_VERSION
    assert document["counts"] == {"memories": 1}
    assert len(document["content_sha256"]) == 64
    assert document["source"]["store"] == "operator-selected memory store"
    [entry] = document["entries"]
    assert entry["declared_state"] == "proposed"
    assert entry["memory"]["memory_id"]


def test_export_contains_no_local_paths(tmp_path: Path) -> None:
    _, document = exported(tmp_path)
    text = render_export(document)
    assert str(tmp_path) not in text
    assert "a.mem" not in text


def test_export_preserves_identity_and_state(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    memory = seed(store)
    store.review(memory["memory_id"], "accept", actor="operator", note="current")
    document = export_memories(store)
    [entry] = document["entries"]
    assert entry["declared_state"] == "attested"
    assert entry["memory"]["memory_id"] == memory["memory_id"]
    assert entry["memory"]["content_sha256"] == memory["content_sha256"]
    assert entry["memory"]["history"][-1]["action"] == "accept"


def test_export_excludes_private_memories_by_default(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    seed(store)
    seed(
        store,
        title="Client secret",
        content="Do not share this externally.",
        sensitivity="private",
    )
    default = export_memories(store)
    opted_in = export_memories(store, include_private=True)
    assert default["counts"]["memories"] == 1
    assert opted_in["counts"]["memories"] == 2


def test_export_excludes_inactive_memories_by_default(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    memory = seed(store)
    store.review(memory["memory_id"], "retract", reason="stale")
    assert export_memories(store)["counts"]["memories"] == 0
    assert (
        export_memories(store, include_inactive=True)["counts"]["memories"] == 1
    )


def test_export_selects_by_id_and_tag(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = seed(store)
    seed(store, title="Typography", content="Use the serif for headings.",
         tags=["type"])
    by_id = export_memories(store, memory_ids=[first["memory_id"]])
    by_tag = export_memories(store, tags=["type"])
    assert by_id["counts"]["memories"] == 1
    assert by_tag["counts"]["memories"] == 1
    assert by_tag["entries"][0]["memory"]["title"] == "Typography"


def test_export_refuses_unknown_ids(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    seed(store)
    with pytest.raises(StateError, match="no such memory"):
        export_memories(store, memory_ids=["MEM-000000000000"])


def test_export_record_cap_is_enforced(tmp_path: Path, monkeypatch) -> None:
    import aperio.memportable as module

    store = make_store(tmp_path)
    seed(store)
    seed(store, title="Second", content="Another requirement.")
    monkeypatch.setattr(module, "MAX_EXPORT_RECORDS", 1)
    with pytest.raises(StateError, match="refusing to export"):
        export_memories(store)
    assert MAX_EXPORT_RECORDS == 500


def test_export_size_cap_is_enforced(tmp_path: Path, monkeypatch) -> None:
    import aperio.memportable as module

    store = make_store(tmp_path)
    seed(store)
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(StateError, match="larger than"):
        export_memories(store)
    assert MAX_EXPORT_BYTES == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_preserves_content_identity_but_not_trust(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    memory = seed(source)
    source.review(memory["memory_id"], "accept", actor="operator")
    source.review(memory["memory_id"], "verify", actor="reviewer")
    document = export_memories(source)

    target = make_store(tmp_path, "target.mem")
    report = import_memories(target, document, actor="agent-b")
    [imported_id] = report["imported"]
    imported = target.snapshots()["memories"][imported_id]
    assert imported_id == memory["memory_id"]
    assert imported["content_sha256"] == memory["content_sha256"]
    assert imported["state"] == "proposed"
    assert imported["trust"] == "proposed"
    assert imported["declared_state"] == "verified"
    assert imported["source"]["kind"] == "import"
    assert report["declared_states"][imported_id] == "verified"


def test_reimporting_the_same_file_reports_duplicates(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    target = make_store(tmp_path, "target.mem")
    first = import_memories(target, document)
    second = import_memories(target, document)
    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert second["duplicates"] == first["imported"]


def test_import_dry_run_writes_nothing(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    target = make_store(tmp_path, "target.mem")
    report = import_memories(target, document, dry_run=True)
    assert report["dry_run"] is True
    assert len(report["imported"]) == 1
    assert not target.path.exists()


def test_import_respects_the_sensitivity_filter(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source, sensitivity="private", title="Private note",
         content="Sensitive.")
    document = export_memories(source, include_private=True)
    target = make_store(tmp_path, "target.mem")
    report = import_memories(target, document, max_sensitivity="internal")
    assert report["imported"] == []
    assert len(report["excluded_sensitivity"]) == 1


def test_import_reports_subject_conflicts_without_overwriting(
    tmp_path: Path,
) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source, content="Primary brand colour is navy blue.")
    document = export_memories(source)

    target = make_store(tmp_path, "target.mem")
    local = seed(target, content="Primary brand colour is deep green.")
    report = import_memories(target, document)
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["existing_memory_id"] == local["memory_id"]
    state = target.snapshots()["memories"]
    assert len(state) == 2
    assert state[local["memory_id"]]["content"] == (
        "Primary brand colour is deep green."
    )


def test_tampered_exchange_file_is_refused(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    document["entries"][0]["memory"]["content"] = "Altered claim."
    with pytest.raises(ValidationError, match="content hash"):
        validate_exchange_document(document)


def test_wrong_format_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not an Aperio memory exchange"):
        validate_exchange_document({"format": "aperio-export", "format_version": 1})


def test_future_format_version_is_refused() -> None:
    with pytest.raises(ValidationError, match="unsupported memory exchange"):
        validate_exchange_document(
            {
                "format": MEMORY_EXCHANGE_FORMAT,
                "format_version": 99,
                "memory_schema_version": 1,
                "entries": [],
                "content_sha256": "",
            }
        )


def test_unknown_document_keys_are_refused() -> None:
    document = {
        "format": MEMORY_EXCHANGE_FORMAT,
        "format_version": MEMORY_EXCHANGE_FORMAT_VERSION,
        "memory_schema_version": 1,
        "entries": [],
        "content_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "trust": "verified",
    }
    with pytest.raises(ValidationError, match="unknown keys"):
        validate_exchange_document(document)


def test_symlinked_exchange_file_is_refused(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    path = tmp_path / "exchange.json"
    path.write_text(render_export(export_memories(source)), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValidationError, match="symlink"):
        load_exchange_file(link)


def test_oversized_exchange_file_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    import aperio.memportable as module

    source = make_store(tmp_path, "source.mem")
    seed(source)
    path = tmp_path / "exchange.json"
    path.write_text(render_export(export_memories(source)), encoding="utf-8")
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 10)
    with pytest.raises(ValidationError, match="at most"):
        load_exchange_file(path)


def test_import_rejects_a_forged_memory_id(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    document["entries"][0]["memory"]["memory_id"] = "MEM-00000000dead"
    document["content_sha256"] = __import__(
        "aperio.memportable", fromlist=["_export_digest"]
    )._export_digest(document["entries"])
    with pytest.raises(ValidationError, match="memory_id"):
        validate_exchange_document(document)


def test_summary_is_bounded_and_readable(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    target = make_store(tmp_path, "target.mem")
    summary = summarize_import(import_memories(target, document))
    assert summary == (
        "imported 1, duplicates 0, conflicts 0, excluded for sensitivity 0"
    )


def test_export_json_round_trips_through_a_file(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    memory = seed(source)
    path = tmp_path / "exchange.json"
    path.write_text(render_export(export_memories(source)), encoding="utf-8")
    target = make_store(tmp_path, "target.mem")
    report = import_memories(target, load_exchange_file(path))
    assert report["imported"] == [memory["memory_id"]]
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]


def test_legacy_project_archive_shape_is_untouched(tmp_path: Path) -> None:
    """This format must not be mistakable for the Feature 05 archive."""

    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    assert document["format"] != "aperio-portable-context"
    assert "state" not in document
    assert "decisions" not in document


def test_trust_label_must_agree_with_state(tmp_path: Path) -> None:
    source = make_store(tmp_path, "source.mem")
    seed(source)
    document = export_memories(source)
    document["entries"][0]["memory"]["trust"] = "verified"
    import aperio.memportable as module

    document["content_sha256"] = module._export_digest(document["entries"])
    with pytest.raises(ValidationError, match="trust label disagrees"):
        validate_exchange_document(document)
