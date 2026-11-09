"""M6 universal portable exchange: cross-agent transfer with provenance.

Acceptance: ChatGPT → Aperio → Claude → Aperio → Gemini/GLM through manual
portable payloads; the receiving agent understands the goal, context,
constraints, provenance, accepted lessons, and outstanding uncertainties;
an imported unsupported claim remains unverified; legacy formats are
untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.exchange import (
    EXCHANGE_FORMAT,
    apply_import,
    build_exchange,
    context_pack_section,
    feedback_section,
    goal_section,
    load_exchange_file,
    memory_section,
    notes_section,
    plan_import,
    quality_report_section,
    validate_exchange,
)
from aperio.feedback import (
    lesson_memory_record,
    record_review,
    validate_feedback,
)
from aperio.goals import validate_goal
from aperio.memportable import export_memories
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore
from aperio.quality import run_quality_check


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return path


DESIGN_GOAL = {
    "objective": "Design a spring campaign poster for the neighbourhood "
    "garden open day",
    "intended_outcome": "A printable A3 poster the committee can send to "
    "the print shop",
    "profile": "design",
    "constraints": ["Accessible contrast", "Must include the garden logo"],
    "deliverables": ["A3 poster layout"],
    "uncertainties": ["Final event date is not fixed"],
}


# ---------------------------------------------------------------------------
# document contract
# ---------------------------------------------------------------------------


def test_exchange_requires_explicit_selection() -> None:
    with pytest.raises(ValidationError, match="at least one section"):
        build_exchange([])
    with pytest.raises(ValidationError, match="at least one"):
        build_exchange([goal_section(DESIGN_GOAL), goal_section(DESIGN_GOAL)][:0]
                       + [])


def test_sections_are_validated_by_their_own_contracts() -> None:
    bad_goal = {"objective": "no outcome field"}
    with pytest.raises(ValidationError, match="intended_outcome"):
        goal_section(bad_goal)
    with pytest.raises(ValidationError, match="aperio-context-pack"):
        context_pack_section({"format": "something-else"})
    with pytest.raises(ValidationError, match="aperio-quality-report"):
        quality_report_section({"format": "something-else"})


def test_tampering_is_refused_everywhere(tmp_path: Path) -> None:
    exchange = build_exchange([goal_section(DESIGN_GOAL)])
    file = _write(tmp_path, "x.json", exchange)

    loaded = load_exchange_file(file)
    assert loaded["format"] == EXCHANGE_FORMAT

    tampered = json.loads(file.read_text())
    tampered["sections"][0]["payload"]["objective"] = "Tampered"
    with pytest.raises(ValidationError, match="payload_sha256"):
        validate_exchange(tampered)

    reordered = json.loads(file.read_text())
    reordered["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="content_sha256"):
        validate_exchange(reordered)

    with pytest.raises(ValidationError, match="format_version"):
        validate_exchange({**json.loads(file.read_text()),
                           "format_version": 99})

    unknown_kind = json.loads(file.read_text())
    unknown_kind["sections"][0]["kind"] = "executable-code"
    with pytest.raises(ValidationError, match="unsupported kind"):
        validate_exchange(unknown_kind)


def test_deterministic_serialization() -> None:
    first = build_exchange([goal_section(DESIGN_GOAL),
                            notes_section([{"text": "Date is not fixed"}])])
    second = build_exchange([notes_section([{"text": "Date is not fixed"}]),
                             goal_section(DESIGN_GOAL)])
    # order-independent build, deterministic section order and digest
    assert first["content_sha256"] == second["content_sha256"]
    assert [s["section_id"] for s in first["sections"]] == \
        [s["section_id"] for s in second["sections"]]


def test_payload_caps_are_enforced() -> None:
    oversized = {
        "kind": "operator-notes",
        "payload": {"items": [{"text": "x" * (8 * 1024 + 1)}]},
    }
    # the owning contract's own bound fires (the same validation import
    # would apply), never a silent truncation
    with pytest.raises(ValidationError, match="at most 8192"):
        build_exchange([oversized])
    with pytest.raises(ValidationError, match="at most 8192"):
        notes_section([{"text": "x" * (8 * 1024 + 1)}])


# ---------------------------------------------------------------------------
# the cross-agent acceptance walkthrough
# ---------------------------------------------------------------------------


def test_cross_agent_round_trip(tmp_path: Path, capsys) -> None:
    # --- machine A: the operator prepares work with Aperio (ChatGPT step) --
    goal = validate_goal(DESIGN_GOAL)
    pack = run_quality_check.__globals__["build_goal_context_pack"](goal) \
        if False else None
    from aperio.goals import build_goal_context_pack

    pack = build_goal_context_pack(goal)
    pack_file = _write(tmp_path, "pack.json", pack)
    goal_file = _write(tmp_path, "goal.json", goal)

    # memories on machine A: one genuinely verified, one with an
    # unsupported verified claim (no history) — the honesty probe
    store = MemoryStore(tmp_path / "brand.mem")
    verified = store.propose({
        "type": "requirement", "title": "Brand colour",
        "content": "Primary brand colour is deep green #0B5D3B.",
        "scope": {"kind": "global"}, "tags": ["brand"], "actor": "designer",
    })["memory"]
    store.review(verified["memory_id"], "accept", actor="ops-lead")
    store.review(verified["memory_id"], "verify", actor="reviewer-1",
                 evidence=["brand-guide-v3.pdf"])
    unproven = store.propose({
        "type": "limitation", "title": "Old logo licence",
        "content": "The 2019 logo licence allegedly allows print use.",
        "scope": {"kind": "global"}, "tags": ["brand"], "actor": "designer",
    })["memory"]
    # hand-append a verified state with no proof, as an older release could
    import json as _json
    from aperio.memstore import line_sha256
    raw = store.path.read_text(encoding="utf-8").splitlines()
    previous = _json.loads(raw[-1])["chain_sha256"]
    event = {
        "record": "transition", "memory_id": unproven["memory_id"],
        "revision": 2, "at": "2026-01-02T00:00:00Z", "actor": None,
        "action": "verify", "from_state": "proposed",
        "to_state": "verified", "note": None, "evidence": [],
        "reason": None,
    }
    event["chain_sha256"] = line_sha256(event, previous)
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(_json.dumps(event, ensure_ascii=False) + "\n")
    snapshot = store.snapshots()["memories"][unproven["memory_id"]]
    assert snapshot["trust"] == "attested"  # honest downgrade

    memory_doc = export_memories(store, memory_ids=[verified["memory_id"],
                                                    unproven["memory_id"]])
    exchange = build_exchange([
        goal_section(goal),
        context_pack_section(pack),
        memory_section(memory_doc),
        notes_section([{"text": "Final event date is not fixed — confirm "
                                "with the committee before print"}]),
    ])
    exchange_file = _write(tmp_path, "exchange.json", exchange)

    # --- machine B: Claude receives and imports (nothing promoted) --------
    dest_b = tmp_path / "claude"; dest_b.mkdir()
    code, out, err = run_cli(
        capsys, "exchange-import", str(exchange_file),
        "--dest-dir", str(dest_b), "--memory-store", str(tmp_path / "b.mem"),
    )
    assert code == 0, err
    assert "dry-run" in out  # preview is the default
    code, out, err = run_cli(
        capsys, "exchange-import", str(exchange_file),
        "--dest-dir", str(dest_b), "--memory-store", str(tmp_path / "b.mem"),
        "--apply", "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    written = {e["kind"]: e.get("file") for e in report["content"]["planned"]
               if e["action"] == "wrote"}
    assert "goal" in written and "context-pack" in written
    memory_summary = next(e for e in report["content"]["planned"]
                          if e["kind"] == "memory")
    assert memory_summary["imported"] == 2

    store_b = MemoryStore(tmp_path / "b.mem")
    states = {mid: snap["state"]
              for mid, snap in store_b.snapshots()["memories"].items()}
    assert set(states.values()) == {"proposed"}, \
        "imported memories always enter as proposed"
    # the unsupported claim stays visibly unverified with its declared state
    unproven_snap = next(
        s for s in store_b.snapshots()["memories"].values()
        if s["title"] == "Old logo licence"
    )
    assert unproven_snap["declared_state"] == "verified"
    assert unproven_snap["trust"] == "proposed"

    # the receiving agent reads the goal + pack and answers the questions
    imported_goal = json.loads((dest_b / written["goal"]).read_text())
    assert imported_goal["objective"] == DESIGN_GOAL["objective"]
    assert imported_goal["constraints"] == DESIGN_GOAL["constraints"]
    # provenance preserved: uncertainties travel as missing information
    assert imported_goal["uncertainties"] == DESIGN_GOAL["uncertainties"]

    # --- machine B produces work; Aperio evaluates and feedback accrues ---
    deliverable = (
        "# A3 poster layout\n\nThe garden logo sits top-centre; high "
        "contrast type stays accessible. (Event date left blank pending "
        "confirmation.)"
    )
    quality = run_quality_check(imported_goal, deliverable_text=deliverable)
    assert not [i for i in quality["content"]["issues"]
                if i["status"] == "confirmed"]

    feedback = validate_feedback({
        "problem": "Poster projects always need a print checklist section",
        "origin": "operator", "actor": "designer-2", "severity": "medium",
        "lesson": "Every poster spec carries a print checklist (sizes, "
                  "colours, bleed)",
        "scope": {"kind": "profile", "profile": "design"},
        "created_at": "2026-11-09T12:00:00Z",
    })
    accepted = record_review(feedback, decision="accepted",
                             reviewer="designer-3",
                             at="2026-11-09T13:00:00Z")
    lessons_store = MemoryStore(tmp_path / "lessons-b.mem")
    lesson_mem = lessons_store.propose(
        lesson_memory_record(accepted, actor="designer-3")
    )["memory"]
    lessons_store.review(lesson_mem["memory_id"], "accept", actor="editor-1")

    # --- machine B sends work + lesson back (Gemini/GLM step) --------------
    lesson_doc = export_memories(lessons_store)
    exchange2 = build_exchange([
        goal_section(imported_goal),
        quality_report_section(quality),
        feedback_section(accepted),
        memory_section(lesson_doc),
    ])
    exchange2_file = _write(tmp_path, "exchange2.json", exchange2)

    dest_c = tmp_path / "gemini"; dest_c.mkdir()
    code, out, err = run_cli(
        capsys, "exchange-import", str(exchange2_file),
        "--dest-dir", str(dest_c),
        "--memory-store", str(tmp_path / "c.mem"),
        "--apply", "--json",
    )
    assert code == 0, err
    report2 = json.loads(out)
    files2 = {e["kind"]: e.get("file") for e in report2["content"]["planned"]
              if e["action"] == "wrote"}
    assert "quality-report" in files2 and "feedback" in files2
    # the accepted lesson arrived as PROPOSED (no trust promotion anywhere)
    store_c = MemoryStore(tmp_path / "c.mem")
    lesson_snap = next(iter(store_c.snapshots()["memories"].values()))
    assert lesson_snap["state"] == "proposed"
    assert "print checklist" in lesson_snap["content"]

    # duplicate detection on re-import
    code, out, err = run_cli(
        capsys, "exchange-import", str(exchange2_file),
        "--dest-dir", str(dest_c),
        "--memory-store", str(tmp_path / "c.mem"),
        "--apply", "--json",
    )
    assert code == 0, err
    report3 = json.loads(out)
    memory3 = next(e for e in report3["content"]["planned"]
                   if e["kind"] == "memory")
    assert memory3["duplicates"] == 1
    files3 = [e for e in report3["content"]["planned"] if "file" in e]
    assert all(e["action"] == "already-present" for e in files3)


def test_memory_selection_is_explicit(tmp_path: Path, capsys) -> None:
    goal_file = _write(tmp_path, "goal.json", DESIGN_GOAL)
    code, _, err = run_cli(
        capsys, "exchange-export", "--goal", str(goal_file),
        "--memory-store", str(tmp_path / "missing.mem"),
    )
    assert code == 1
    assert "select memories explicitly" in err


def test_memory_needs_store_on_import(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "a.mem")
    mem = store.propose({
        "type": "requirement", "title": "T", "content": "C",
        "scope": {"kind": "global"}, "tags": ["t"], "actor": "a",
    })["memory"]
    doc = export_memories(store)
    exchange = build_exchange([memory_section(doc), goal_section(DESIGN_GOAL)])
    dest = tmp_path / "dest"; dest.mkdir()
    plan = plan_import(exchange, dest)
    memory_plan = next(e for e in plan["content"]["planned"]
                       if e["kind"] == "memory")
    assert memory_plan["action"] == "skipped"
    result = apply_import(exchange, dest)
    memory_applied = next(e for e in result["content"]["planned"]
                          if e["kind"] == "memory")
    assert memory_applied["action"] == "skipped"
    files = list(dest.iterdir())
    assert len(files) == 1  # only the goal file was written


# ---------------------------------------------------------------------------
# legacy compatibility
# ---------------------------------------------------------------------------


def test_memory_export_v1_and_project_archive_formats_unchanged(
    tmp_path: Path,
) -> None:
    from aperio.memportable import MEMORY_EXCHANGE_FORMAT
    from aperio.portable import EXPORT_FORMAT

    store = MemoryStore(tmp_path / "legacy.mem")
    store.propose({"type": "requirement", "title": "T", "content": "C",
                   "scope": {"kind": "global"}, "tags": [], "actor": "a"})
    doc = export_memories(store)
    assert doc["format"] == MEMORY_EXCHANGE_FORMAT == "aperio-memory-export"
    assert doc["format_version"] == 1
    assert EXPORT_FORMAT == "aperio-portable-export"


def test_private_memories_stay_out_unless_deliberate(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "priv.mem")
    store.propose({
        "type": "user_preference", "title": "Home address",
        "content": "Lives at 12 Example Street.",
        "scope": {"kind": "global"}, "tags": ["personal"],
        "sensitivity": "private", "actor": "a",
    })
    doc_default = export_memories(store)
    assert doc_default["entries"] == []
    doc_private = export_memories(store, include_private=True)
    assert len(doc_private["entries"]) == 1
    exchange = build_exchange([memory_section(doc_private)])
    assert exchange["manifest"]["counts"]["memory"] == 1
