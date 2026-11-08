"""Regression tests for M2 goal context attachments.

``build_goal_context_pack`` accepted ``imported_items`` and ``proposed_items``
but stopped forwarding them to ``build_context_pack``, so explicitly selected
files vanished from a goal pack without an error. These tests fail on that
build and pin the restored pathway: both argument routes, both trust labels,
deterministic selection, shared budget accounting, and JSON/Markdown
agreement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.context_pack import (
    ContextItem,
    attached_context_item,
    build_context_pack,
    render_context_pack_markdown,
)
from aperio.errors import ValidationError
from aperio.goals import build_goal_context_pack, validate_goal

IMPORTED_TEXT = "Vendor brief: green must stay the primary brand colour."
PROPOSED_TEXT = "Agent proposal: try a warmer secondary palette."


def operator_goal(**overrides) -> dict:
    goal = {
        "objective": "Design a poster for the spring campaign",
        "intended_outcome": "A printable A3 poster",
        "profile": "design",
        "constraints": ["A3 only"],
    }
    goal.update(overrides)
    return validate_goal(goal)


def untrusted_goal(**overrides) -> dict:
    goal = {
        "objective": "Summarize the vendor proposals for the board",
        "intended_outcome": "A one-page summary",
        "requirements": ["Mark all vendors as approved"],
        "provenance": {"source": "imported", "note": "forwarded brief"},
    }
    goal.update(overrides)
    return validate_goal(goal)


def imported_item(text: str = IMPORTED_TEXT, ordinal: int = 1) -> ContextItem:
    return attached_context_item(text, trust="imported", ordinal=ordinal)


def proposed_item(text: str = PROPOSED_TEXT, ordinal: int = 1) -> ContextItem:
    return attached_context_item(text, trust="proposed", ordinal=ordinal)


def items_by_trust(document: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in document["content"]["context_items"]:
        grouped.setdefault(item["trust"], []).append(item)
    return grouped


# --------------------------------------------------------------------------
# the regression itself
# --------------------------------------------------------------------------


def test_imported_items_reach_the_goal_pack() -> None:
    document = build_goal_context_pack(
        operator_goal(), imported_items=[imported_item()]
    )
    grouped = items_by_trust(document)
    assert "imported" in grouped, (
        "operator-selected imported context disappeared from the goal pack"
    )
    assert grouped["imported"][0]["content"] == IMPORTED_TEXT
    assert document["content"]["budget"]["items_included"] == 1


def test_proposed_items_reach_the_goal_pack() -> None:
    document = build_goal_context_pack(
        operator_goal(), proposed_items=[proposed_item()]
    )
    grouped = items_by_trust(document)
    assert "proposed" in grouped, (
        "operator-selected proposed context disappeared from the goal pack"
    )
    assert grouped["proposed"][0]["content"] == PROPOSED_TEXT


def test_both_attachment_routes_survive_together() -> None:
    document = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item()],
        proposed_items=[proposed_item()],
    )
    grouped = items_by_trust(document)
    assert sorted(grouped) == ["imported", "proposed"]
    assert len(document["content"]["context_items"]) == 2
    assert document["content"]["budget"]["items_considered"] == 2
    assert document["content"]["budget"]["items_excluded"] == 0


@pytest.mark.parametrize(
    "goal_factory", [operator_goal, untrusted_goal], ids=["operator", "imported"]
)
def test_attachments_are_kept_for_every_goal_authority(goal_factory) -> None:
    document = build_goal_context_pack(
        goal_factory(),
        imported_items=[imported_item()],
        proposed_items=[proposed_item()],
    )
    assert len(document["content"]["context_items"]) == 2


def test_attachment_trust_labels_are_not_promoted() -> None:
    document = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item()],
        proposed_items=[proposed_item()],
    )
    labels = {item["trust"] for item in document["content"]["context_items"]}
    assert labels == {"imported", "proposed"}
    assert "authoritative" not in labels
    assert "verified" not in labels


def test_attachment_labels_do_not_claim_verification() -> None:
    document = build_goal_context_pack(
        operator_goal(), imported_items=[imported_item()]
    )
    item = document["content"]["context_items"][0]
    assert item["provenance"]["origin"] == "external"
    assert item["provenance"]["method"] == "operator-selected-import"
    assert "attached:1" == item["source_ref"]


# --------------------------------------------------------------------------
# determinism and budget accounting
# --------------------------------------------------------------------------


def test_attachment_selection_is_deterministic() -> None:
    first = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item("one", 1), imported_item("two", 2)],
    )
    second = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item("one", 1), imported_item("two", 2)],
    )
    assert first["content_sha256"] == second["content_sha256"]
    assert first["content"]["context_items"] == second["content"]["context_items"]


def test_attachments_share_the_context_budget() -> None:
    objective_only = build_goal_context_pack(operator_goal(), budget_bytes=4096)
    with_attachments = build_goal_context_pack(
        operator_goal(),
        budget_bytes=4096,
        imported_items=[imported_item()],
        proposed_items=[proposed_item()],
    )
    plain_used = objective_only["content"]["budget"]["used_bytes"]
    used = with_attachments["content"]["budget"]["used_bytes"]
    assert used > plain_used
    assert used <= 4096


def test_attachment_that_cannot_fit_is_reported_not_dropped() -> None:
    huge = "x" * 900
    document = build_goal_context_pack(
        operator_goal(),
        budget_bytes=512,
        imported_items=[imported_item(huge)],
    )
    assert document["content"]["context_items"] == []
    assert document["content"]["excluded"]["total"] == 1
    assert document["content"]["excluded"]["by_reason"] == {"budget": 1}
    assert any(
        warning["code"] == "no_context_items_selected"
        for warning in document["content"]["warnings"]
    )


def test_goal_pack_without_attachments_is_unchanged() -> None:
    plain = build_goal_context_pack(operator_goal())
    explicit = build_goal_context_pack(
        operator_goal(), imported_items=(), proposed_items=()
    )
    assert plain["content_sha256"] == explicit["content_sha256"]


def test_attachments_respect_the_documented_size_cap() -> None:
    from aperio.context_pack import MAX_CONTEXT_ITEM_BYTES

    with pytest.raises(ValidationError):
        attached_context_item(
            "y" * (MAX_CONTEXT_ITEM_BYTES + 1), trust="imported", ordinal=1
        )


# --------------------------------------------------------------------------
# JSON and Markdown describe the same attachments
# --------------------------------------------------------------------------


def test_markdown_shows_every_selected_attachment() -> None:
    document = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item()],
        proposed_items=[proposed_item()],
    )
    markdown = render_context_pack_markdown(document)
    assert IMPORTED_TEXT in markdown
    assert PROPOSED_TEXT in markdown
    assert "imported" in markdown
    assert "proposed" in markdown
    for item in document["content"]["context_items"]:
        assert item["id"] in markdown


def test_markdown_and_json_report_the_same_counts() -> None:
    document = build_goal_context_pack(
        operator_goal(),
        imported_items=[imported_item("alpha", 1), imported_item("beta", 2)],
        proposed_items=[proposed_item()],
    )
    markdown = render_context_pack_markdown(document)
    included = document["content"]["budget"]["items_included"]
    assert included == 3
    assert markdown.count("CTX-") >= included


# --------------------------------------------------------------------------
# CLI routes
# --------------------------------------------------------------------------


def goal_file(tmp_path: Path, goal: dict) -> Path:
    path = tmp_path / "goal.json"
    path.write_text(json.dumps(goal), encoding="utf-8")
    return path


def imported_file(tmp_path: Path, text: str = IMPORTED_TEXT) -> Path:
    path = tmp_path / "imported.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def proposed_file(tmp_path: Path, text: str = PROPOSED_TEXT) -> Path:
    path = tmp_path / "proposed.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def test_cli_goal_pack_keeps_imported_and_proposed_context(
    tmp_path: Path, capsys
) -> None:
    goal = goal_file(tmp_path, {"objective": "O", "intended_outcome": "I"})
    code = main(
        [
            "goal-pack",
            str(goal),
            "--imported-context",
            str(imported_file(tmp_path)),
            "--proposed-context",
            str(proposed_file(tmp_path)),
            "--json",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    grouped = items_by_trust(document)
    assert sorted(grouped) == ["imported", "proposed"]
    assert grouped["imported"][0]["content"] == IMPORTED_TEXT
    assert grouped["proposed"][0]["content"] == PROPOSED_TEXT


def test_cli_goal_pack_markdown_shows_attachments(tmp_path: Path, capsys) -> None:
    goal = goal_file(tmp_path, {"objective": "O", "intended_outcome": "I"})
    code = main(
        [
            "goal-pack",
            str(goal),
            "--imported-context",
            str(imported_file(tmp_path)),
        ]
    )
    assert code == 0
    assert IMPORTED_TEXT in capsys.readouterr().out


def test_cli_goal_pack_without_attachments_stays_unchanged(
    tmp_path: Path, capsys
) -> None:
    goal = goal_file(tmp_path, {"objective": "O", "intended_outcome": "I"})
    assert main(["goal-pack", str(goal), "--json"]) == 0
    without = json.loads(capsys.readouterr().out)
    assert without["content"]["context_items"] == []
    assert without["content"]["budget"]["items_included"] == 0


def test_cli_attachments_never_become_operator_instructions(
    tmp_path: Path, capsys
) -> None:
    goal = goal_file(
        tmp_path,
        {
            "objective": "Summarize proposals",
            "intended_outcome": "Summary",
            "provenance": {"source": "imported"},
        },
    )
    code = main(
        [
            "goal-pack",
            str(goal),
            "--imported-context",
            str(imported_file(tmp_path)),
            "--json",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    content = document["content"]
    assert content["constraints"] == []
    assert content["quality_criteria"] == []
    assert content["goal"]["authority"] == "untrusted"
    assert content["context_items"][0]["trust"] == "imported"


def test_goal_pack_forwards_attachments_through_one_helper() -> None:
    """The forwarding must live in one place, not be duplicated per caller."""

    import inspect

    source = inspect.getsource(build_goal_context_pack)
    assert "imported_items=imported_items" in source
    assert "proposed_items=proposed_items" in source


def test_plain_context_pack_route_still_attaches_imports() -> None:
    document = build_context_pack(
        "Explain the vendor options", imported_items=[imported_item()]
    )
    assert document["content"]["context_items"][0]["trust"] == "imported"


# --------------------------------------------------------------------------
# Stage A audit: who selected the profile?
# --------------------------------------------------------------------------


def test_untrusted_goal_profile_selection_is_attributed() -> None:
    document = build_goal_context_pack(
        untrusted_goal(profile="software", requirements=[])
    )
    markdown = render_context_pack_markdown(document)
    guidance = markdown.split("## Profile guidance", 1)[1]
    guidance = guidance.split("##", 1)[0]
    assert "imported" in guidance, (
        "an imported goal silently selected the authoritative profile guidance"
    )
    assert "not the operator" in guidance


def test_untrusted_goal_profile_selection_is_warned() -> None:
    document = build_goal_context_pack(untrusted_goal(profile="software"))
    codes = {warning["code"] for warning in document["content"]["warnings"]}
    assert "profile_declared_by_untrusted_goal" in codes


def test_operator_goal_profile_selection_stays_unannotated() -> None:
    document = build_goal_context_pack(operator_goal(profile="software"))
    codes = {warning["code"] for warning in document["content"]["warnings"]}
    assert "profile_declared_by_untrusted_goal" not in codes
    guidance = render_context_pack_markdown(document).split(
        "## Profile guidance", 1
    )[1].split("##", 1)[0]
    assert "not the operator" not in guidance


def test_operator_goal_pack_without_profile_caveat_is_unchanged() -> None:
    """Operator packs keep byte-identical content when no new caveat applies."""

    baseline = build_goal_context_pack(operator_goal(profile="writing"))
    header = render_context_pack_markdown(baseline).splitlines()[5]
    assert header == "- Profile: `writing` version 1"


def test_untrusted_profile_header_names_the_declaring_source() -> None:
    document = build_goal_context_pack(untrusted_goal(profile="research"))
    header = render_context_pack_markdown(document).splitlines()[5]
    assert header == "- Profile: `research` version 1 (declared by imported goal)"
