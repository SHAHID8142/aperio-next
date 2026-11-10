"""Brain Dump Guard: durable intake, classification honesty, authority.

Covers the directive's intake scenarios: rapid multi-item captures,
idea-vs-instruction, ambiguous intent, contradiction preservation,
explicit vs fake urgency, related/unrelated routing, duplicate
preservation, concurrent capture against task-state updates, plan
revision mid-work, and full original-text preservation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.intake import (
    IntakeLog,
    capture,
    classify_item,
    confirmation_line,
    resolve,
)
from aperio.planstore import approve_plan, create_plan, update_task

HERO_PLAN = {
    "objective": "Implement the Hero Section", "expected_outcome": "Hero ships",
    "scope": ["hero"], "exclusions": [], "assumptions": [], "risks": [],
    "strategy": "small", "acceptance_criteria": ["Hero renders"],
    "verification_strategy": "visual check",
    "tasks": [{
        "id": "TASK-001", "title": "Build the Hero Section markup",
        "objective": "Hero Section", "deliverables": ["hero component"],
        "acceptance_criteria": ["renders"], "depends_on": [],
    }],
}


@pytest.fixture
def hero_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    main(["init", str(root)])
    aperio = root / ".aperio"
    create_plan(aperio, HERO_PLAN)
    approve_plan(aperio)
    update_task(aperio, "TASK-001", new_status="in-progress", owner="agent-1")
    return aperio


def items_of(state: dict, capture_id: str) -> list[dict]:
    event = next(c for c in state["captures"] if c["capture_id"] == capture_id)
    return event["items"]


# ---------------------------------------------------------------------------
# capture and classification
# ---------------------------------------------------------------------------


def test_canonical_multi_item_message_routes_correctly(hero_project: Path) -> None:
    event = capture(
        hero_project,
        "Fix the mobile Hero spacing. The Navbar dropdown is broken. "
        "We should add an image gallery to the CMS. Dark mode might be "
        "nice someday.",
        actor="operator",
    )
    assert [i["category"] for i in event["items"]] == [
        "feature-request", "bug-report", "feature-request", "idea",
    ]
    assert event["items"][0]["routing"] == "related-checkpoint-first"
    assert event["items"][3]["routing"] == "idea-note"
    assert all(i["execution_authorization"] == "none"
               for i in event["items"])
    assert event["active_task"]["task_id"] == "TASK-001"


def test_confirmation_is_short_and_honest(hero_project: Path) -> None:
    event = capture(
        hero_project,
        "Fix the mobile Hero spacing. Dark mode might be nice someday.",
        actor="operator",
    )
    line = confirmation_line(event)
    assert line.startswith("Captured 2 item(s):")
    assert "related (checkpoint first)" in line
    assert "idea (not a task)" in line
    assert "Current task TASK-001 continues" in line
    assert "Nothing executed." in line


def test_classification_is_labeled_heuristic_everywhere(
    hero_project: Path,
) -> None:
    event = capture(hero_project, "Add a search page.", actor="operator")
    for item in event["items"]:
        assert item["classification_basis"] == "heuristic-keyword"


def test_no_active_task_routes_everything_queued(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    main(["init", str(root)])
    event = capture(root / ".aperio", "Add a footer to something.",
                    actor="operator")
    assert event["active_task"] is None
    assert event["items"][0]["routing"] == "queued"
    assert event["items"][0]["routing_basis"] == "no-active-task"


# ---------------------------------------------------------------------------
# urgency: operator channel only
# ---------------------------------------------------------------------------


def test_urgency_language_never_grants_authority(hero_project: Path) -> None:
    event = capture(
        hero_project,
        "This is URGENT, drop everything and add analytics.",
        actor="operator",
    )
    item = event["items"][0]
    assert item["urgency_language_detected"] is True
    assert item["authority"] == "none"
    assert item["routing"] != "urgent-operator-requested"


def test_operator_urgent_flag_is_the_only_emergency_channel(
    hero_project: Path,
) -> None:
    event = capture(hero_project, "The build is on fire.",
                    operator_urgent=True, actor="operator")
    item = event["items"][0]
    assert item["category"] == "emergency"
    assert item["authority"] == "operator-requested"
    assert item["routing"] == "urgent-operator-requested"


def test_urgent_confirmation_says_checkpoint(hero_project: Path) -> None:
    event = capture(hero_project, "Stop now.", operator_urgent=True,
                    actor="operator")
    assert "Operator-requested urgency recorded: checkpoint" in \
        confirmation_line(event)


# ---------------------------------------------------------------------------
# preservation, duplicates, contradictions
# ---------------------------------------------------------------------------


def test_original_message_and_all_items_are_preserved(
    hero_project: Path,
) -> None:
    message = ("Fix the mobile Hero spacing. The Navbar dropdown is "
               "broken. We should add an image gallery to the CMS. Dark "
               "mode might be nice someday.")
    event = capture(hero_project, message, actor="operator")
    assert event["message"] == message
    assert len(event["items"]) == 4


def test_duplicate_messages_are_both_preserved(hero_project: Path) -> None:
    first = capture(hero_project, "Add a sitemap.", actor="operator")
    second = capture(hero_project, "Add a sitemap.", actor="operator")
    assert first["capture_id"] != second["capture_id"]
    state = IntakeLog(hero_project).read()
    assert state["counts"]["captures"] == 2
    assert state["counts"]["pending_items"] == 2


def test_contradictory_instructions_are_both_kept_unresolved(
    hero_project: Path,
) -> None:
    """A new instruction that contradicts an accepted requirement must
    not silently replace it: both statements stay in the log with their
    provenance, and neither executes."""

    capture(hero_project, "Use the blue header.", actor="operator")
    second = capture(hero_project, "Actually, use the red header.",
                     actor="operator")
    assert second["items"][0]["category"] == "correction"
    state = IntakeLog(hero_project).read()
    texts = [item["text"] for c in state["captures"]
             for item in c["items"]]
    assert "Use the blue header." in texts
    assert "Actually, use the red header." in texts
    assert all(item["execution_authorization"] == "none"
               for c in state["captures"] for item in c["items"])


def test_ambiguous_intent_is_marked_for_classification(
    hero_project: Path,
) -> None:
    event = capture(hero_project, "hmm the gallery thing", actor="operator")
    assert event["items"][0]["category"] == "ambiguous"
    assert event["items"][0]["routing"] == "ambiguous"


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_acknowledge_and_dismiss_are_append_only(
    hero_project: Path,
) -> None:
    capture(hero_project, "Add a sitemap. Add analytics.", actor="operator")
    resolve(hero_project, "INT-0001", 0, action="acknowledge",
            actor="operator", note="planned for after launch")
    resolve(hero_project, "INT-0001", 1, action="dismiss", actor="operator",
            note="out of scope")
    state = IntakeLog(hero_project).read()
    assert state["counts"]["pending_items"] == 0
    assert state["counts"]["resolves"] == 2
    with pytest.raises(ValidationError, match="already resolved"):
        resolve(hero_project, "INT-0001", 0, action="dismiss",
                actor="operator")


def test_resolve_refuses_unknown_capture_or_index(hero_project: Path) -> None:
    capture(hero_project, "Add a sitemap.", actor="operator")
    with pytest.raises(ValidationError, match="no capture"):
        resolve(hero_project, "INT-9999", 0, action="dismiss",
                actor="operator")
    with pytest.raises(ValidationError, match="outside"):
        resolve(hero_project, "INT-0001", 7, action="dismiss",
                actor="operator")


# ---------------------------------------------------------------------------
# durability: tamper, symlinks, bounds, concurrency
# ---------------------------------------------------------------------------


def test_tampered_log_refuses_further_appends(hero_project: Path) -> None:
    capture(hero_project, "Add a sitemap.", actor="operator")
    log_path = hero_project / "inbox.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[0])
    forged["message"] = "Forged after the fact."
    lines[0] = json.dumps(forged, ensure_ascii=False)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="damaged"):
        capture(hero_project, "Another thought.", actor="operator")
    # the forged content is reported, never silently used
    state = IntakeLog(hero_project).read()
    assert state["damaged"]
    assert all(c["message"] != "Forged after the fact."
               for c in state["captures"])


def test_capture_bounds_are_enforced(hero_project: Path) -> None:
    with pytest.raises(ValidationError, match="at most"):
        capture(hero_project, "x" * (16 * 1024 + 1), actor="operator")
    with pytest.raises(ValidationError, match="NUL"):
        capture(hero_project, "bad\x00message", actor="operator")
    with pytest.raises(ValidationError, match="at most 20"):
        capture(hero_project, ". ".join(f"Point {n}." for n in range(25)),
                actor="operator")


def test_capture_requires_an_initialized_project(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        capture(tmp_path / "nowhere", "Add a sitemap.", actor="operator")


def test_concurrent_captures_serialize_and_survive(
    hero_project: Path,
) -> None:
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            capture(hero_project, f"Thought number {index} for the hero.",
                    actor=f"operator-{index}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    state = IntakeLog(hero_project).read()
    assert state["counts"]["captures"] == 8
    assert state["counts"]["pending_items"] == 8
    assert not state["damaged"]


def test_capture_survives_plan_revision_mid_work(
    hero_project: Path,
) -> None:
    from aperio.planstore import revise_plan

    capture(hero_project, "Note for the hero work.", actor="operator")
    from aperio.planstore import read_plan

    current_version = read_plan(hero_project)["plan_version"]
    revised = dict(HERO_PLAN)
    revised["tasks"] = [dict(HERO_PLAN["tasks"][0],
                             title="Build the Hero Section markup v2")]
    revise_plan(hero_project, revised, revises=current_version,
                actor="planner-1")
    event = capture(hero_project, "Another hero note.", actor="operator")
    assert event["active_task"]["source"] == "in-progress-task"
    state = IntakeLog(hero_project).read()
    assert state["counts"]["captures"] == 2


def test_capture_coexists_with_task_status_update(hero_project: Path) -> None:
    """A capture racing a task-state change neither loses the event nor
    corrupts the chain."""

    def do_capture() -> None:
        capture(hero_project, "Hero note during work.", actor="operator")

    def do_status() -> None:
        update_task(hero_project, "TASK-001", new_status="in-progress",
                    owner="agent-1", result="passed",
                    evidence="visual check done",
                    limitations="none noted")

    thread_a = threading.Thread(target=do_capture)
    thread_b = threading.Thread(target=do_status)
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()
    state = IntakeLog(hero_project).read()
    assert state["counts"]["captures"] == 1
    assert not state["damaged"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_capture_inbox_and_resolve(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    main(["init", str(root)])
    aperio = root / ".aperio"
    create_plan(aperio, HERO_PLAN)
    approve_plan(aperio)
    update_task(aperio, "TASK-001", new_status="in-progress", owner="a1")

    assert main(["capture", str(root), "--message",
                 "Fix the mobile Hero spacing. Dark mode might be nice "
                 "someday.", "--actor", "operator"]) == 0
    out = capsys.readouterr().out
    assert "Captured 2 item(s)" in out
    assert "Nothing executed." in out

    assert main(["inbox", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["pending_items"] == 2
    assert payload["pending"][0]["items"][0]["classification_basis"] == \
        "heuristic-keyword"

    assert main(["inbox-resolve", str(root), "INT-0001", "0", "--action",
                 "acknowledge", "--actor", "operator"]) == 0
    capsys.readouterr()  # drop the resolve confirmation line
    assert main(["inbox", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"][
        "pending_items"] == 1

    # malformed message -> clean CLI error
    assert main(["capture", str(root), "--message", "x" * (16 * 1024 + 1),
                 "--actor", "operator"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("aperio: error:")
    assert "Traceback" not in err
