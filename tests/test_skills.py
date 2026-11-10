"""Trust-aware portable skill packs: format, states, composition.

Trust rules under test: imported skills are untrusted until an explicit
review approves them; untrusted skills never compose into briefs;
declared conflicts are reported, never auto-resolved; operator
requirements outrank skills; private material never travels with an
export; builtin packs enter stores as untrusted like anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.skills import (
    SkillStore,
    builtin_skill_packs,
    compose_skill_section,
    export_approved_skills,
    import_skill_export,
    parse_skill_md,
    select_skills,
)

VALID_SKILL = """<!-- aperio:skill
name: testing-integration
version: 1.0.0
description: Functional and integration test planning with honest evidence recording.
scope: testing
depends_on:
conflicts:
provenance: imported
-->
Verify each acceptance criterion with a recorded check. State what was
not covered. Never present an agent's own claim of success as evidence.
"""


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def store_with(tmp_path: Path, *texts: str) -> SkillStore:
    store_dir = tmp_path / "skills"
    store = SkillStore(store_dir)
    for index, text in enumerate(texts):
        result = store.import_skill_md(text, source=f"test-{index}")
        assert result["imported"] or "identical" in result["reason"]
    return store


# ---------------------------------------------------------------------------
# format parsing
# ---------------------------------------------------------------------------


def test_valid_skill_parses() -> None:
    skill = parse_skill_md(VALID_SKILL, source_path="test")
    assert skill["name"] == "testing-integration"
    assert skill["scope"] == "testing"
    assert skill["version"] == "1.0.0"
    assert len(skill["content_sha256"]) == 64
    assert "recorded check" in skill["body"]


@pytest.mark.parametrize("mutation,match", [
    (VALID_SKILL.replace("name: testing-integration", "name: Bad Name"),
     "name"),
    (VALID_SKILL.replace("version: 1.0.0", "version: 1.0"),
     "semver"),
    (VALID_SKILL.replace("scope: testing", "scope: everything"),
     "scope must be one of"),
    (VALID_SKILL.replace("description: Functional and integration test "
                         "planning with honest evidence recording.",
                         "description: " + "x" * 400),
     "at most"),
    (VALID_SKILL.replace("<--", "<!--").replace("<!-- aperio:skill\n",
                                               ""), "header"),
])
def test_malformed_skills_are_refused(mutation: str, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        parse_skill_md(mutation, source_path="test")


def test_header_must_be_first_and_closed() -> None:
    with pytest.raises(ValidationError, match="first line"):
        parse_skill_md("prose first\n<!-- aperio:skill\nname: x-y\n-->",
                       source_path="t")
    with pytest.raises(ValidationError, match="closed"):
        parse_skill_md("<!-- aperio:skill\nname: x-y\n", source_path="t")


def test_unknown_and_duplicate_header_fields_refused() -> None:
    with pytest.raises(ValidationError, match="unknown header"):
        parse_skill_md(
            VALID_SKILL.replace("conflicts:", "executable: yes"),
            source_path="t",
        )
    with pytest.raises(ValidationError, match="duplicate"):
        parse_skill_md(
            VALID_SKILL.replace("conflicts:",
                                "conflicts:\nversion: 2.0.0"),
            source_path="t",
        )


# ---------------------------------------------------------------------------
# store states: import -> review
# ---------------------------------------------------------------------------


def test_import_enters_untrusted_and_review_approves(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    skill = store.read_skill("testing-integration")
    assert skill["state"]["state"] == "untrusted"

    store.review("testing-integration", action="approve", actor="operator")
    skill = store.read_skill("testing-integration")
    assert skill["state"]["state"] == "approved"
    assert skill["state"]["reviews"][-1]["actor"] == "operator"


def test_reimport_identical_is_noop_different_content_refused(
    tmp_path: Path,
) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    result = store.import_skill_md(VALID_SKILL, source="again")
    assert result["imported"] is False
    altered = VALID_SKILL.replace("Never present", "Never ever present")
    with pytest.raises(ValidationError, match="same version"):
        store.import_skill_md(altered, source="again")


def test_double_approve_refused(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    with pytest.raises(ValidationError, match="already approved"):
        store.review("testing-integration", action="approve", actor="op")


def test_builtin_packs_parse_and_enter_untrusted(tmp_path: Path) -> None:
    packs = builtin_skill_packs()
    assert len(packs) >= 11
    names = {p["name"] for p in packs}
    required = {
        "requirements-review", "ux-ia", "design-systems", "accessibility",
        "frontend-engineering", "backend-cms", "security-review",
        "testing-integration", "release-rollback", "technical-seo",
        "seo-content-reporting",
    }
    assert required <= names
    for pack in packs:
        assert pack["provenance"] == "aperio-builtin-v1"
        missing = [d for d in pack["depends_on"] if d not in names]
        assert not missing, (pack["name"], missing)
    store = tmp_path / "builtin-store"
    for pack in packs:
        from aperio.skills import render_skill_md

        result = SkillStore(store).import_skill_md(
            render_skill_md(pack), source=f"builtin:{pack['name']}"
        )
        assert result["imported"]
        assert result["skill"]["state"]["state"] == "untrusted"


# ---------------------------------------------------------------------------
# selection and composition
# ---------------------------------------------------------------------------


def test_selection_requires_approved_state(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    skills = store.list_skills()
    selection = select_skills(skills, scope="testing")
    assert selection["selected"] == []
    assert selection["rejected"][0]["reason"].startswith("skill state is")
    with pytest.raises(ValidationError, match="not approved"):
        select_skills(skills, scope="testing", required=["testing-integration"])


def test_conflicts_between_selected_skills_block_composition(
    tmp_path: Path,
) -> None:
    skill_a = VALID_SKILL.replace(
        "name: testing-integration", "name: strict-mode"
    ).replace("scope: testing", "scope: general")
    skill_b = VALID_SKILL.replace(
        "name: testing-integration", "name: lenient-mode"
    ).replace("conflicts:", "conflicts: strict-mode").replace(
        "scope: testing", "scope: general"
    )
    store = store_with(tmp_path, skill_a, skill_b)
    for name in ("strict-mode", "lenient-mode"):
        store.review(name, action="approve", actor="op")
    selection = select_skills(
        store.list_skills(), scope="general",
        required=["strict-mode", "lenient-mode"],
    )
    assert selection["conflicts"]
    with pytest.raises(ValidationError, match="resolved by the operator"):
        compose_skill_section(selection)


def test_version_change_resets_trust_and_supersedes(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    v2 = VALID_SKILL.replace("version: 1.0.0", "version: 2.0.0").replace(
        "Never present", "Never, under any circumstances, present"
    )
    result = store.import_skill_md(v2, source="upgrade")
    assert result["imported"] is True
    assert "state reset to untrusted" in result["reason"]
    skill = store.read_skill("testing-integration")
    assert skill["version"] == "2.0.0"
    assert skill["state"]["state"] == "untrusted", (
        "new content is a new judgment: approval never carries over"
    )
    mutated = VALID_SKILL.replace("version: 1.0.0", "version: 2.0.0").replace(
        "Verify each", "Now verify each"
    )
    with pytest.raises(ValidationError, match="same version"):
        store.import_skill_md(mutated, source="mutate")


def test_scope_mismatch_is_rejected_with_reason(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    selection = select_skills(
        store.list_skills(), scope="frontend"
    )
    assert selection["selected"] == []
    assert any("scope" in r["reason"] for r in selection["rejected"])


# ---------------------------------------------------------------------------
# export/import portability
# ---------------------------------------------------------------------------


def test_export_carries_approved_only_and_reimports_untrusted(
    tmp_path: Path,
) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    export = export_approved_skills(store.store_dir)
    assert export["skills"] == []
    store.review("testing-integration", action="approve", actor="op")
    export = export_approved_skills(store.store_dir)
    assert len(export["skills"]) == 1
    assert "recorded check" in export["skills"][0]["body"]

    other = tmp_path / "other-store"
    report = import_skill_export(other, export)
    assert report["imported"][0]["imported"] is True
    imported = SkillStore(other).read_skill("testing-integration")
    assert imported["state"]["state"] == "untrusted"


def test_import_refuses_altered_export(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    export = export_approved_skills(store.store_dir)
    export["skills"][0]["body"] = export["skills"][0]["body"].replace(
        "Never", "Always"
    )
    with pytest.raises(ValidationError, match="hash mismatch"):
        import_skill_export(tmp_path / "other", export)


def test_export_document_never_contains_machine_paths(tmp_path: Path) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    export = export_approved_skills(store.store_dir)
    assert str(tmp_path) not in json.dumps(export)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_skill_lifecycle(tmp_path: Path, capsys) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(VALID_SKILL, encoding="utf-8")
    store = str(tmp_path / "store")

    code, out, err = run_cli(capsys, "skill-import", store, str(skill_file))
    assert code == 0, err
    assert "untrusted" in out

    code, out, _ = run_cli(capsys, "skill-list", store, "--json")
    assert code == 0
    listing = json.loads(out)
    assert listing[0]["state"]["state"] == "untrusted"

    code, out, _ = run_cli(capsys, "skill-review", store,
                           "testing-integration", "--action", "approve",
                           "--actor", "operator", "--note", "reviewed ok")
    assert code == 0, err
    assert "approved" in out

    code, out, _ = run_cli(capsys, "skill-show", store,
                           "testing-integration")
    assert code == 0
    assert "reviewed ok" in out

    code, out, _ = run_cli(capsys, "skill-select", store,
                           str(_goal_file(tmp_path)), "--json",
                           "--require", "testing-integration")
    assert code == 0
    selection = json.loads(out)
    assert selection["selected"][0]["name"] == "testing-integration"

    export_file = tmp_path / "export.json"
    code, out, _ = run_cli(capsys, "skill-export", store, "--output",
                           str(export_file))
    assert code == 0

    # malformed skill -> clean error
    bad = tmp_path / "bad.md"
    bad.write_text("no header", encoding="utf-8")
    code, _, err = run_cli(capsys, "skill-import", str(tmp_path / "s2"),
                           str(bad))
    assert code == 1
    assert err.startswith("aperio: error:")
    assert "Traceback" not in err


def _goal_file(tmp_path: Path) -> Path:
    path = tmp_path / "goal.json"
    path.write_text(json.dumps({
        "objective": "Plan the test pass for the release",
        "intended_outcome": "A test plan",
        "profile": "general",
    }), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# brief composition
# ---------------------------------------------------------------------------


def test_agent_brief_composes_approved_skills_only(
    tmp_path: Path, capsys
) -> None:
    store = store_with(tmp_path, VALID_SKILL)
    store.review("testing-integration", action="approve", actor="op")
    goal_file = _goal_file(tmp_path)
    code, out, err = run_cli(
        capsys, "agent-brief", str(goal_file), "--skills-store",
        str(store.store_dir), "--skill", "testing-integration", "--json",
    )
    assert code == 0, err
    brief = json.loads(out)
    skills = brief["content"]["skills"]
    assert skills["skills"][0]["name"] == "testing-integration"
    assert "never gate approval or completion" in json.dumps(
        brief["content"]["limitations"]
    )

    code, out, _ = run_cli(
        capsys, "agent-brief", str(goal_file), "--skills-store",
        str(store.store_dir), "--skill", "testing-integration",
    )
    assert code == 0
    assert "Selected skills (quoted guidance)" in out
