"""M1: universal, read-only context-pack contracts and CLI."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from aperio.context_pack import (
    CONTEXT_PACK_FORMAT,
    CONTEXT_PACK_FORMAT_VERSION,
    BUDGET_SCOPE,
    ContextItem,
    attached_context_item,
    build_context_pack,
    canonical_content_bytes,
    context_content_sha256,
    read_utf8_text_file,
    render_context_pack_markdown,
)
from aperio.decisions import append_decision
from aperio.handoff import HandoffContent, write_handoff
from aperio.planstore import approve_plan, create_plan

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _plan() -> dict:
    return {
        "objective": "universal context baseline",
        "expected_outcome": "portable context",
        "scope": ["context"],
        "exclusions": [],
        "assumptions": [],
        "risks": [],
        "strategy": "assemble evidence",
        "acceptance_criteria": ["context is portable"],
        "verification_strategy": "tests",
        "tasks": [
            {
                "id": "TASK-001",
                "title": "prepare context",
                "objective": "prove context assembly",
                "deliverables": ["context"],
                "acceptance_criteria": ["portable"],
                "depends_on": [],
            }
        ],
    }


def _pack_json(run_cli, *args: str) -> dict:
    code, out, err = run_cli("context-pack", *args, "--json")
    assert code == 0, err
    return json.loads(out)


def _write_handoff(project: Path) -> None:
    content = HandoffContent(
        current_objective="Prepare a portable context package",
        completed_work="Collected the current brief and decision log",
        work_in_progress="Selecting context within the budget",
        next_recommended_action="Review the generated pack",
        blockers="None",
        verification_evidence="Context items have provenance labels",
        verification_limitations="No independent review yet",
    )
    write_handoff(project / ".aperio", content)


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def test_standalone_pack_without_any_aperio_project(run_cli, tmp_path):
    objective = (
        "Design a calm identity for a neighborhood garden, including a "
        "small poster and a social-media announcement."
    )
    pack = _pack_json(
        run_cli,
        "--objective", objective,
        "--constraint", "Use accessible contrast and large type.",
        "--quality-criterion", "The system is legible at small sizes.",
        "--expected-output", "A concise visual identity direction.",
        "--audience", "Neighborhood residents",
    )
    assert pack["format"] == CONTEXT_PACK_FORMAT
    assert pack["format_version"] == CONTEXT_PACK_FORMAT_VERSION
    assert pack["content"]["profile"] == {"id": "general", "version": 1}
    assert pack["content"]["objective"] == objective
    assert pack["content"]["objective_source"] == "operator"
    assert pack["content"]["constraints"] == [
        "Use accessible contrast and large type."
    ]
    assert pack["content"]["budget"]["scope"] == BUDGET_SCOPE
    assert pack["content"]["context_items"] == []
    assert not (tmp_path / ".aperio").exists()

    markdown = render_context_pack_markdown(pack)
    assert "Aperio Context Pack" in markdown
    assert objective in markdown
    assert "pytest" not in markdown
    assert "implementation diff" not in markdown
    assert "repository" not in markdown


@pytest.mark.parametrize(
    "objective",
    [
        "Create a visual identity and poster system for a community garden.",
        "What does the current evidence say about urban heat islands?",
        "Write a 700-word explainer for a general audience about sleep debt.",
        "Analyze whether opening a second store is financially viable.",
        "Explain the trade-offs between two scheduling approaches.",
    ],
)
def test_general_profile_domain_examples_work_without_a_software_plan(
    run_cli, objective
):
    pack = _pack_json(
        run_cli,
        "--objective", objective,
        "--constraint", "State assumptions explicitly.",
        "--quality-criterion", "Every claim is supported or labeled uncertain.",
        "--expected-output", "A structured response.",
    )
    assert pack["content"]["profile"]["id"] == "general"
    assert pack["content"]["objective"] == objective
    assert pack["content"]["completeness"]["constraints_provided"] is True
    assert "software" not in pack["content"]["profile"]["id"]


def test_objective_file_supports_unicode_and_multiline_text(run_cli, tmp_path):
    objective_file = tmp_path / "objective.txt"
    objective = "分析這個問題\n\n- 保留不確定性\n- 使用 🌱 標記"
    objective_file.write_text(objective, encoding="utf-8")
    pack = _pack_json(run_cli, "--objective-file", str(objective_file))
    assert pack["content"]["objective"] == objective.strip()
    markdown = render_context_pack_markdown(pack)
    assert "🌱" in markdown


def test_objective_argument_and_file_are_mutually_exclusive(run_cli, tmp_path):
    path = tmp_path / "objective.txt"
    path.write_text("objective", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        run_cli(
            "context-pack", "--objective", "one", "--objective-file", str(path)
        )
    assert excinfo.value.code == 2


def test_missing_objective_is_an_actionable_error(run_cli):
    code, out, err = run_cli("context-pack")
    assert code == 1
    assert out == ""
    assert "objective is required" in err
    assert "Traceback" not in err


def test_project_backed_pack_is_read_only_and_uses_expected_trust(
    run_cli, project_dir, tmp_path
):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    aperio = project_dir / ".aperio"
    _write_handoff(project_dir)
    append_decision(
        aperio,
        title="Choose the launch palette",
        context="The identity must remain accessible.",
        decision="Use verified contrast tokens.",
        consequences="All future assets reuse the tokens.",
    )
    create_plan(aperio, _plan())
    approve_plan(aperio, by="operator")
    local = aperio / "local"
    local.mkdir(exist_ok=True)
    secret = "TOP-SECRET-PROVIDER-TOKEN"
    (local / "provider.log").write_text(secret, encoding="utf-8")

    before = _snapshot(project_dir)
    pack = _pack_json(
        run_cli, str(project_dir),
        "--objective", "Prepare the next design review.",
        "--constraint", "Respect the approved palette.",
    )
    after = _snapshot(project_dir)
    assert before == after

    by_ref = {item["source_ref"]: item for item in pack["content"]["context_items"]}
    assert by_ref["brief.md"]["trust"] == "attested"
    assert by_ref["handoff.md"]["trust"] == "attested"
    assert by_ref["plan.json"]["trust"] == "authoritative"
    assert by_ref["DEC-0001"]["trust"] == "attested"
    assert pack["content"]["profile"]["id"] == "general"
    serialized = json.dumps(pack, ensure_ascii=False)
    assert secret not in serialized
    assert str(project_dir) not in serialized


def test_project_with_missing_optional_artifacts_still_works(
    run_cli, project_dir
):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    aperio = project_dir / ".aperio"
    (aperio / "decisions.md").unlink(missing_ok=True)
    (aperio / "plan.json").unlink(missing_ok=True)
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Write a short brief."
    )
    assert pack["content"]["objective"] == "Write a short brief."
    refs = {item["source_ref"] for item in pack["content"]["context_items"]}
    assert "plan.json" not in refs
    assert "handoff.md" not in refs


def test_malformed_optional_sources_are_skipped_not_fatal(
    run_cli, project_dir
):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    aperio = project_dir / ".aperio"
    (aperio / "plan.json").write_text("{not json", encoding="utf-8")
    (aperio / "decisions.md").write_bytes(b"\xff\xfe\x00")
    (aperio / "handoff.md").write_text("## Broken\n", encoding="utf-8")
    pack = _pack_json(
        run_cli, str(project_dir),
        "--objective", "Continue the task with the readable context.",
    )
    codes = {warning["code"] for warning in pack["content"]["warnings"]}
    assert "optional_source_skipped" in codes
    assert pack["content"]["objective"]
    assert str(project_dir) not in json.dumps(pack)


def test_objective_falls_back_to_an_existing_plan(run_cli, project_dir):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    create_plan(project_dir / ".aperio", _plan())
    pack = _pack_json(run_cli, str(project_dir))
    assert pack["content"]["objective"] == "universal context baseline"
    assert pack["content"]["objective_source"] == "plan"
    assert any(
        warning["code"] == "objective_from_workspace"
        for warning in pack["content"]["warnings"]
    )


def test_software_profile_is_opt_in_and_legacy_plan_is_preserved(
    run_cli, project_dir
):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    create_plan(project_dir / ".aperio", _plan())
    approve_plan(project_dir / ".aperio", by="operator")

    default_pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Review the plan."
    )
    software_pack = _pack_json(
        run_cli, str(project_dir),
        "--objective", "Review the plan.",
        "--profile", "software",
    )
    assert default_pack["content"]["profile"]["id"] == "general"
    assert software_pack["content"]["profile"]["id"] == "software"
    assert "software-development profile" in render_context_pack_markdown(
        software_pack
    )
    plan = next(
        item for item in software_pack["content"]["context_items"]
        if item["source_ref"] == "plan.json"
    )
    assert plan["trust"] == "authoritative"


def test_budget_is_deterministic_whole_item_and_reports_scope():
    items = [
        attached_context_item("A" * 20, trust="imported", ordinal=index)
        for index in range(1, 4)
    ]
    first = build_context_pack(
        "Use the background notes.",
        budget_bytes=40,
        imported_items=items,
    )
    second = build_context_pack(
        "Use the background notes.",
        budget_bytes=40,
        imported_items=items,
    )
    assert first["content"]["budget"] == {
        "scope": BUDGET_SCOPE,
        "limit_bytes": 40,
        "used_bytes": 40,
        "items_considered": 3,
        "items_included": 2,
        "items_excluded": 1,
    }
    assert first["content"]["excluded"]["by_reason"] == {"budget": 1}
    assert first["content"]["context_items"][0]["content"] == "A" * 20
    assert first["content_sha256"] == second["content_sha256"]
    assert first["content"] == second["content"]


def test_zero_budget_includes_no_context_but_keeps_requirements():
    item = attached_context_item("background", trust="imported", ordinal=1)
    pack = build_context_pack(
        "Do the task.",
        budget_bytes=0,
        constraints=["Keep it short."],
        imported_items=[item],
    )
    assert pack["content"]["context_items"] == []
    assert pack["content"]["constraints"] == ["Keep it short."]
    assert pack["content"]["budget"]["used_bytes"] == 0
    assert pack["content"]["excluded"]["by_reason"] == {"budget": 1}


def test_identical_inputs_have_identical_content_hash_and_honest_time():
    first = build_context_pack("Stable objective.", constraints=["Same."])
    second = build_context_pack("Stable objective.", constraints=["Same."])
    assert first["content"] == second["content"]
    assert first["content_sha256"] == second["content_sha256"]
    assert first["content_sha256"] == context_content_sha256(first["content"])
    assert TIMESTAMP_RE.match(first["generated_at"])
    assert "generated_at" not in first["content"]
    assert canonical_content_bytes(first["content"]) == canonical_content_bytes(
        second["content"]
    )


def test_different_objective_changes_the_content_hash():
    first = build_context_pack("Objective one.")
    second = build_context_pack("Objective two.")
    assert first["content_sha256"] != second["content_sha256"]


def test_missing_constraints_and_quality_are_explicit_not_invented():
    pack = build_context_pack("Produce a useful answer.")
    content = pack["content"]
    assert content["constraints"] == []
    assert content["quality_criteria"] == []
    assert content["completeness"] == {
        "constraints_provided": False,
        "assumptions_provided": False,
        "risks_provided": False,
        "quality_criteria_provided": False,
        "expected_output_provided": False,
        "audience_provided": False,
    }
    codes = {warning["code"] for warning in content["warnings"]}
    assert {
        "constraints_not_provided",
        "quality_criteria_not_provided",
        "expected_output_not_provided",
    } <= codes
    markdown = render_context_pack_markdown(pack)
    assert "## Requirements and constraints\n\nNot provided." in markdown
    assert "## Quality criteria\n\nNot provided." in markdown


def test_operator_missing_information_is_preserved():
    pack = build_context_pack(
        "Write the report.",
        missing_information=["The target launch date is unknown."],
    )
    assert pack["content"]["missing_information"] == [
        "The target launch date is unknown."
    ]
    assert "The target launch date is unknown." in render_context_pack_markdown(
        pack
    )


def test_imported_and_proposed_context_are_labeled_and_path_free(
    run_cli, tmp_path
):
    imported = tmp_path / "private-source.txt"
    imported.write_text("External note: cite the field study.", encoding="utf-8")
    proposed = tmp_path / "agent-output.txt"
    proposed.write_text("Agent claim: the result is probably correct.", encoding="utf-8")
    pack = _pack_json(
        run_cli,
        "--objective", "Write a short evidence note.",
        "--imported-context", str(imported),
        "--proposed-context", str(proposed),
    )
    items = pack["content"]["context_items"]
    assert [item["trust"] for item in items] == ["imported", "proposed"]
    assert items[0]["content"] == "External note: cite the field study."
    assert items[0]["source_ref"] == "attached:1"
    assert items[1]["source_ref"] == "attached:1"
    serialized = json.dumps(pack, ensure_ascii=False)
    assert "private-source.txt" not in serialized
    assert "agent-output.txt" not in serialized
    assert str(tmp_path) not in serialized
    codes = {warning["code"] for warning in pack["content"]["warnings"]}
    assert {"imported_context_included", "proposed_context_included"} <= codes
    markdown = render_context_pack_markdown(pack)
    assert "**imported**" in markdown
    assert "**proposed**" in markdown


def test_imported_context_is_not_promoted_by_repetition():
    item = attached_context_item(
        "Unverified claim: the metric improved by 50%.",
        trust="imported",
        ordinal=1,
    )
    pack = build_context_pack(
        "Assess the claim.", imported_items=[item, item]
    )
    assert len(pack["content"]["context_items"]) == 1
    assert pack["content"]["context_items"][0]["trust"] == "imported"
    assert pack["content"]["excluded"]["by_reason"] == {"duplicate": 1}
    assert pack["content"]["warnings"]
    assert "verified" not in {
        warning["source"] for warning in pack["content"]["warnings"]
    }


def test_markdown_and_json_are_rendered_from_the_same_payload():
    item = attached_context_item(
        "A quoted background note.", trust="imported", ordinal=1
    )
    pack = build_context_pack(
        "Use the background.",
        constraints=["Do not invent facts."],
        imported_items=[item],
    )
    markdown = render_context_pack_markdown(pack)
    assert pack["content_sha256"] in markdown
    for selected in pack["content"]["context_items"]:
        assert selected["id"] in markdown
        assert selected["trust"] in markdown
        assert selected["source_ref"] in markdown
    assert "Do not invent facts." in markdown
    assert "A quoted background note." in markdown


def test_markdown_renderer_refuses_tampered_content():
    pack = build_context_pack("Original objective.")
    pack["content"]["objective"] = "Tampered objective."
    with pytest.raises(Exception, match="hash does not match"):
        render_context_pack_markdown(pack)


def test_invalid_utf8_and_oversized_files_are_actionable_errors(
    run_cli, tmp_path
):
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff\xfe")
    code, out, err = run_cli("context-pack", "--objective-file", str(invalid))
    assert code == 1
    assert out == ""
    assert "valid UTF-8" in err
    assert "Traceback" not in err

    oversized = tmp_path / "large.txt"
    oversized.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    code, out, err = run_cli("context-pack", "--objective-file", str(oversized))
    assert code == 1
    assert out == ""
    assert "at most" in err
    assert "Traceback" not in err


def test_symlinked_input_file_is_refused(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(Exception, match="symlink"):
        read_utf8_text_file(link, label="objective file")


def test_negative_budget_is_rejected(run_cli):
    code, out, err = run_cli(
        "context-pack", "--objective", "Task.", "--budget", "-1"
    )
    assert code == 1
    assert out == ""
    assert "context budget" in err
    assert "Traceback" not in err


def test_empty_objective_is_rejected(run_cli):
    code, out, err = run_cli("context-pack", "--objective", "   ")
    assert code == 1
    assert out == ""
    assert "objective is required" in err


def test_verified_review_context_uses_verified_trust(
    run_cli, project_dir, tmp_path
):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    code, _, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "r:reviewer", "--assign", "r2:reviewer",
    )
    assert code == 0, err
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_plan()), encoding="utf-8")
    code, _, err = run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file),
        "--actor", "p",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "plan-approve", str(project_dir), "--actor", "r"
    )
    assert code == 0, err
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err
    code, out, err = run_cli(
        "review-submit", str(project_dir),
        "--task", "TASK-001", "--actor", "i",
        "--scope", "acceptance criteria",
        "--evidence", "context package inspected",
    )
    assert code == 0, err
    match = re.search(r"REV-\d{4,}", out)
    assert match is not None
    review_id = match.group(0)
    code, _, err = run_cli(
        "review-decide", str(project_dir), review_id,
        "--actor", "r", "--outcome", "approved",
        "--findings", "Scope met.",
        "--limitations", "Context only.",
    )
    assert code == 0, err

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Use the reviewed context."
    )
    review = next(
        item for item in pack["content"]["context_items"]
        if item["source_ref"] == review_id
    )
    assert review["trust"] == "verified"
    assert review["provenance"]["method"] == "independent-review"
    assert "context package inspected" in review["content"]


def test_cli_json_has_valid_identity_and_no_local_state(run_cli, project_dir):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    local = project_dir / ".aperio" / "local"
    local.mkdir(exist_ok=True)
    (local / "secret.txt").write_text("DO-NOT-EXPORT", encoding="utf-8")
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Prepare background."
    )
    assert pack["content_sha256"] == context_content_sha256(pack["content"])
    assert TIMESTAMP_RE.match(pack["generated_at"])
    assert pack["aperio_version"]
    assert "DO-NOT-EXPORT" not in json.dumps(pack)
    assert "local/secret.txt" not in json.dumps(pack)



def test_operator_brief_is_authoritative_and_separate_from_attested_context():
    pack = build_context_pack(
        "Prepare a decision brief.",
        brief="Prioritize clarity over completeness.",
        expected_output="One page.",
    )
    brief = next(
        item for item in pack["content"]["context_items"]
        if item["source"] == "operator"
    )
    assert brief["trust"] == "authoritative"
    assert brief["source_ref"] == "operator:brief"
    assert pack["content"]["operator_brief"] == "Prioritize clarity over completeness."
    assert pack["content"]["output_contract"]["expected_output"] == "One page."


def test_objective_falls_back_to_a_non_template_handoff(run_cli, project_dir):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    _write_handoff(project_dir)
    pack = _pack_json(run_cli, str(project_dir))
    assert pack["content"]["objective"] == "Prepare a portable context package"
    assert pack["content"]["objective_source"] == "handoff"


def test_every_supported_profile_has_guidance():
    from aperio.context_pack import PROFILE_VERSIONS, _PROFILE_GUIDANCE

    assert set(PROFILE_VERSIONS) == set(_PROFILE_GUIDANCE)


def test_local_only_item_is_excluded_and_counted():
    shared = attached_context_item("shareable note", trust="imported", ordinal=1)
    local = ContextItem(
        source="imported",
        source_ref="attached:2",
        kind="background",
        title="Local only",
        content="never share this",
        trust="imported",
        origin="external",
        method="operator-selected-import",
        sensitivity="local_only",
    )
    pack = build_context_pack(
        "Use the notes.", imported_items=[shared, local]
    )
    refs = [item["source_ref"] for item in pack["content"]["context_items"]]
    assert refs == ["attached:1"]
    assert pack["content"]["excluded"]["by_reason"] == {"sensitivity": 1}
    assert "never share this" not in json.dumps(pack, ensure_ascii=False)


# --- M1 trust-correctness: review applicability -------------------------------

REV_RE = re.compile(r"REV-\d{4,}")


def _two_task_plan() -> dict:
    plan = _plan()
    plan["tasks"].append(
        {
            "id": "TASK-002",
            "title": "second task",
            "objective": "second objective",
            "deliverables": ["notes"],
            "acceptance_criteria": ["noted"],
            "depends_on": [],
        }
    )
    return plan


def _governed(run_cli, project_dir, tmp_path, plan=None) -> Path:
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    code, _, err = run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "r:reviewer", "--assign", "r2:reviewer",
    )
    assert code == 0, err
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan or _plan()), encoding="utf-8")
    code, _, err = run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file),
        "--actor", "p",
    )
    assert code == 0, err
    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 0, err
    _claim(run_cli, project_dir)
    return project_dir / ".aperio"


def _claim(run_cli, project_dir, task="TASK-001", actor="i") -> None:
    code, _, err = run_cli(
        "task-status", str(project_dir), task, "--status", "in-progress",
        "--owner", actor, "--actor", actor,
    )
    assert code == 0, err


def _submit_and_approve(run_cli, project_dir, evidence="pytest: evidence A") -> str:
    code, out, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "acceptance criteria",
        "--evidence", evidence,
    )
    assert code == 0, err
    review_id = REV_RE.search(out).group(0)
    code, _, err = run_cli(
        "review-decide", str(project_dir), review_id, "--actor", "r",
        "--outcome", "approved", "--findings", "scope met",
    )
    assert code == 0, err
    return review_id


def _review_items(pack) -> list[dict]:
    return [
        item for item in pack["content"]["context_items"]
        if item["kind"] in ("review", "review-decision")
    ]


def test_current_approved_review_is_verified(run_cli, project_dir, tmp_path):
    _governed(run_cli, project_dir, tmp_path)
    review_id = _submit_and_approve(run_cli, project_dir)
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Prepare completion."
    )
    reviews = [item for item in _review_items(pack) if item["source_ref"] == review_id]
    assert len(reviews) == 1
    assert reviews[0]["trust"] == "verified"
    assert reviews[0]["kind"] == "review"


def test_reopened_task_prior_approval_is_not_verified(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    review_id = _submit_and_approve(run_cli, project_dir)
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "completed",
        "--evidence", "pytest: evidence A", "--limitations", "none",
        "--result", "passed", "--actor", "i",
    )
    assert code == 0, err
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "in-progress",
        "--reopen", "--owner", "i", "--actor", "i",
    )
    assert code == 0, err

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Continue the reopened task."
    )
    assert review_id not in {
        item["source_ref"] for item in _review_items(pack)
        if item["trust"] == "verified"
    }
    assert "previous-attempt" in pack["content"]["excluded"]["by_reason"]
    tasks = json.loads((aperio_dir / "plan.json").read_text())["tasks"][0]
    assert tasks["attempt"] == 2
    # Classification is read-only: the immutable review record is untouched.
    stored = json.loads((aperio_dir / "reviews.json").read_text())["reviews"]
    assert [review["review_id"] for review in stored] == [review_id]
    assert stored[0]["outcome"] == "approved"
    assert stored[0]["target"]["attempt"] == 1
    assert tasks["verification"]["result"] is None


def test_revised_evidence_invalidates_prior_approval(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    review_id = _submit_and_approve(run_cli, project_dir)
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "in-progress",
        "--evidence", "pytest: evidence B", "--limitations", "none",
        "--result", "passed", "--actor", "i",
    )
    assert code == 0, err

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Complete with new evidence."
    )
    verified = {
        item["source_ref"] for item in _review_items(pack)
        if item["trust"] == "verified"
    }
    assert review_id not in verified
    assert "evidence-changed" in pack["content"]["excluded"]["by_reason"]


def test_newer_rejected_review_prevents_older_approval_as_verified(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    approved_id = _submit_and_approve(run_cli, project_dir)
    code, out, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "acceptance criteria",
        "--evidence", "pytest: evidence A",
    )
    assert code == 0, err
    rejected_id = REV_RE.search(out).group(0)
    code, _, err = run_cli(
        "review-decide", str(project_dir), rejected_id, "--actor", "r",
        "--outcome", "rejected", "--findings", "evidence is insufficient",
    )
    assert code == 0, err

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Address the findings."
    )
    items = {item["source_ref"]: item for item in _review_items(pack)}
    assert approved_id not in items
    assert items[rejected_id]["trust"] == "attested"
    assert items[rejected_id]["kind"] == "review-decision"
    assert "superseded" in pack["content"]["excluded"]["by_reason"]
    assert not [
        item for item in items.values() if item["trust"] == "verified"
    ]


def test_newer_pending_review_prevents_older_approval_as_verified(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    approved_id = _submit_and_approve(run_cli, project_dir)
    code, out, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-001",
        "--actor", "i", "--scope", "acceptance criteria",
        "--evidence", "pytest: evidence A",
    )
    assert code == 0, err
    pending_id = REV_RE.search(out).group(0)

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Wait for the review."
    )
    items = {item["source_ref"]: item for item in _review_items(pack)}
    assert approved_id not in items
    assert items[pending_id]["trust"] == "attested"
    assert "pending" in items[pending_id]["content"]
    assert "superseded" in pack["content"]["excluded"]["by_reason"]


def test_review_from_a_previous_plan_revision_is_historical(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path, plan=_two_task_plan())
    review_id = _submit_and_approve(run_cli, project_dir)
    revised = _two_task_plan()
    revised["tasks"][1]["title"] = "second task, rescoped"
    revised_file = tmp_path / "plan-v2.json"
    revised_file.write_text(json.dumps(revised), encoding="utf-8")
    code, _, err = run_cli(
        "plan-revise", str(project_dir), "--file", str(revised_file),
        "--revises", "2", "--actor", "p",
    )
    assert code == 0, err

    draft_pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Draft plan context."
    )
    assert review_id not in {
        item["source_ref"] for item in _review_items(draft_pack)
    }
    assert "no-approved-plan" in draft_pack["content"]["excluded"]["by_reason"]

    code, _, err = run_cli("plan-approve", str(project_dir), "--actor", "r")
    assert code == 0, err
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Revised plan context."
    )
    assert review_id not in {
        item["source_ref"] for item in _review_items(pack)
    }
    assert "previous-plan-revision" in pack["content"]["excluded"]["by_reason"]
    reviews = json.loads((aperio_dir / "reviews.json").read_text())["reviews"]
    assert reviews[0]["review_id"] == review_id
    assert reviews[0]["outcome"] == "approved"


def test_legacy_review_without_attempt_attribution_is_not_verified(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _governed(run_cli, project_dir, tmp_path)
    (aperio_dir / "reviews.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviews": [
                    {
                        "review_id": "REV-0001",
                        "target": {
                            "kind": "task",
                            "task_id": "TASK-001",
                            "plan_revision": 1,
                            "run_id": None,
                        },
                        "submitted_by": "i",
                        "implemented_by": None,
                        "submitted_role": "implementer",
                        "scope": ["acceptance criteria"],
                        "evidence": ["pytest: evidence A"],
                        "outcome": "approved",
                        "reviewer": "r",
                        "findings": "legacy approval",
                        "limitations": None,
                        "created_at": "2025-01-01T00:00:00Z",
                        "decided_at": "2025-01-01T01:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Legacy review context."
    )
    assert not [
        item for item in _review_items(pack) if item["trust"] == "verified"
    ]
    assert "unattributed" in pack["content"]["excluded"]["by_reason"]


def test_standalone_pack_without_reviews_has_no_review_context(run_cli):
    pack = _pack_json(run_cli, "--objective", "One-off task.")
    assert pack["content"]["context_items"] == []
    assert pack["content"]["excluded"]["by_reason"] == {}


def test_review_context_is_deterministic_and_path_free(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    review_id = _submit_and_approve(run_cli, project_dir)
    first = _pack_json(
        run_cli, str(project_dir), "--objective", "Deterministic context."
    )
    second = _pack_json(
        run_cli, str(project_dir), "--objective", "Deterministic context."
    )
    assert first["content"] == second["content"]
    assert first["content_sha256"] == second["content_sha256"]
    review = next(
        item for item in _review_items(first) if item["source_ref"] == review_id
    )
    assert review["trust"] == "verified"
    assert str(project_dir) not in json.dumps(review, ensure_ascii=False)
    assert "/.aperio/" not in json.dumps(review, ensure_ascii=False)


def test_blocked_task_approval_is_not_shown_as_current(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path)
    review_id = _submit_and_approve(run_cli, project_dir)
    code, _, err = run_cli(
        "task-status", str(project_dir), "TASK-001", "--status", "blocked",
        "--reason", "waiting on client input", "--actor", "i",
    )
    assert code == 0, err
    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Unblock the task."
    )
    assert review_id not in {
        item["source_ref"] for item in _review_items(pack)
        if item["trust"] == "verified"
    }
    assert "task-not-active" in pack["content"]["excluded"]["by_reason"]


def test_review_applicability_is_evaluated_per_task(
    run_cli, project_dir, tmp_path
):
    _governed(run_cli, project_dir, tmp_path, plan=_two_task_plan())
    first = _submit_and_approve(run_cli, project_dir)
    _claim(run_cli, project_dir, task="TASK-002")
    code, out, err = run_cli(
        "review-submit", str(project_dir), "--task", "TASK-002",
        "--actor", "i", "--scope", "acceptance criteria",
        "--evidence", "pytest: evidence B",
    )
    assert code == 0, err
    second = REV_RE.search(out).group(0)
    code, _, err = run_cli(
        "review-decide", str(project_dir), second, "--actor", "r",
        "--outcome", "approved",
    )
    assert code == 0, err

    pack = _pack_json(
        run_cli, str(project_dir), "--objective", "Review both tasks."
    )
    verified = {
        item["source_ref"] for item in _review_items(pack)
        if item["trust"] == "verified"
    }
    assert verified == {first, second}
    assert "superseded" not in pack["content"]["excluded"]["by_reason"]
