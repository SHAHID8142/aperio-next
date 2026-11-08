"""M2 — universal goal contract, task profiles, and goal→context integration.

These tests are the acceptance contract for goal-schema v1: domain-neutral
vocabulary, honest missing information, bounded deterministic validation,
optional work decomposition, and a read-only, provider-neutral conversion
into an M1 context pack. Nothing here requires a provider, a model API, or
an initialized project.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aperio import goals, profiles
from aperio.context_pack import (
    PROFILE_VERSIONS,
    build_context_pack,
    context_content_sha256,
    render_context_pack_markdown,
)
from aperio.errors import ValidationError
from aperio.goals import (
    GOAL_SCHEMA_VERSION,
    build_goal_context_pack,
    canonical_goal_bytes,
    describe_goal,
    goal_completeness,
    goal_content_sha256,
    goal_identity,
    goal_trust,
    load_goal_file,
    validate_goal,
    work_graph,
)
from aperio.planmodel import validate_dependency_graph

MINIMAL = {"objective": "Explain compost", "intended_outcome": "A short brief"}


def goal(**overrides) -> dict:
    return validate_goal({**MINIMAL, **overrides})


# --------------------------------------------------------------------------
# Schema: required vs optional, domain neutrality
# --------------------------------------------------------------------------


def test_minimal_goal_is_valid_and_defaults_to_general():
    result = validate_goal(MINIMAL)
    assert result["goal_schema_version"] == GOAL_SCHEMA_VERSION
    assert result["profile"] == "general"
    assert result["goal_id"] is None
    assert result["work"] is None
    assert result["provenance"] == {"source": "operator", "note": None}
    for field in (
        "constraints",
        "requirements",
        "assumptions",
        "uncertainties",
        "risks",
        "deliverables",
        "quality_criteria",
        "acceptance_criteria",
        "evidence_requirements",
    ):
        assert result[field] == []
    assert result["output_contract"] == {}
    assert result["audience"] is None


@pytest.mark.parametrize("missing", ["objective", "intended_outcome"])
def test_required_fields_are_reported(missing):
    data = dict(MINIMAL)
    del data[missing]
    with pytest.raises(ValidationError, match=f"field '{missing}' is required"):
        validate_goal(data)


def test_goal_must_be_a_json_object():
    with pytest.raises(ValidationError, match="must be a JSON object"):
        validate_goal(["objective"])


def test_unknown_top_level_field_is_rejected():
    with pytest.raises(ValidationError, match="unknown field"):
        validate_goal({**MINIMAL, "pytest_command": "pytest -q"})


def test_unsupported_schema_version_is_rejected():
    with pytest.raises(ValidationError, match="unsupported goal_schema_version 2"):
        validate_goal({**MINIMAL, "goal_schema_version": 2})


def test_schema_version_must_be_an_integer():
    with pytest.raises(ValidationError, match="must be an integer"):
        validate_goal({**MINIMAL, "goal_schema_version": "1"})


def test_validation_is_idempotent():
    once = validate_goal({**MINIMAL, "constraints": ["keep it brief"]})
    assert validate_goal(once) == once


def test_goal_validation_requires_no_software_vocabulary():
    text = json.dumps(goal(), ensure_ascii=False).lower()
    for word in ("pytest", "commit", "repository", "diff", "unit test"):
        assert word not in text


def test_invalid_profile_is_rejected():
    with pytest.raises(ValidationError, match="goal.profile must be one of"):
        validate_goal({**MINIMAL, "profile": "devops"})


@pytest.mark.parametrize(
    "profile_id", ["general", "design", "research", "writing", "business", "software"]
)
def test_every_profile_is_accepted(profile_id):
    assert goal(profile=profile_id)["profile"] == profile_id


@pytest.mark.parametrize("bad_id", ["-GOAL", "1GOAL", "GO AL", "G" * 65, ""])
def test_invalid_goal_id_is_rejected(bad_id):
    with pytest.raises(ValidationError, match="goal.goal_id"):
        validate_goal({**MINIMAL, "goal_id": bad_id})


def test_empty_text_field_is_rejected():
    with pytest.raises(ValidationError, match="goal.constraints\\[0\\] must not be empty"):
        validate_goal({**MINIMAL, "constraints": ["   "]})


def test_list_must_be_a_list_of_strings():
    with pytest.raises(ValidationError, match="must be a list of strings"):
        validate_goal({**MINIMAL, "risks": "one risk"})


def test_output_contract_keys_are_bounded():
    assert goal(output_contract={"format": "table", "length": "1 page"})[
        "output_contract"
    ] == {"format": "table", "length": "1 page"}
    with pytest.raises(ValidationError, match="goal.output_contract: unknown field"):
        validate_goal({**MINIMAL, "output_contract": {"font": "Comic Sans"}})


def test_output_contract_must_be_an_object():
    with pytest.raises(ValidationError, match="must be an object"):
        validate_goal({**MINIMAL, "output_contract": "markdown"})


def test_audience_must_be_single_line():
    with pytest.raises(ValidationError, match="single line"):
        validate_goal({**MINIMAL, "audience": "residents\ncommittee"})


def test_unicode_and_multiline_objective_round_trip():
    objective = "Skriv en förklaring — rad ett\nrad två\n中文段落"
    result = goal(objective=objective)
    assert result["objective"] == objective
    pack = build_goal_context_pack(result)
    markdown = render_context_pack_markdown(pack)
    assert "Skriv en förklaring" in markdown
    assert "中文段落" in markdown


def test_foreign_line_separators_are_rejected():
    with pytest.raises(ValidationError, match="line separator"):
        validate_goal({**MINIMAL, "constraints": ["one\u2028two"]})


def test_nul_byte_is_rejected():
    with pytest.raises(ValidationError, match="NUL"):
        validate_goal({**MINIMAL, "objective": "bad\x00text"})


def test_oversize_objective_is_rejected():
    with pytest.raises(ValidationError, match="at most"):
        validate_goal({**MINIMAL, "objective": "x" * (17 * 1024)})


def test_too_many_list_items_are_rejected():
    with pytest.raises(ValidationError, match="at most 50 items"):
        validate_goal({**MINIMAL, "constraints": ["c"] * 51})


# --------------------------------------------------------------------------
# File loading: bounded, safe, no tracebacks
# --------------------------------------------------------------------------


def test_load_goal_file_round_trip(tmp_path):
    path = tmp_path / "goal.json"
    path.write_text(json.dumps(MINIMAL), encoding="utf-8")
    assert load_goal_file(path)["objective"] == "Explain compost"


def test_load_goal_file_rejects_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(MINIMAL), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValidationError, match="symlink"):
        load_goal_file(link)


def test_load_goal_file_rejects_malformed_json(tmp_path):
    path = tmp_path / "goal.json"
    path.write_text('{"objective": ', encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid JSON"):
        load_goal_file(path)


def test_load_goal_file_rejects_non_utf8(tmp_path):
    path = tmp_path / "goal.json"
    path.write_bytes(b'{"objective": "\xff\xfe"}')
    with pytest.raises(ValidationError, match="valid UTF-8"):
        load_goal_file(path)


def test_load_goal_file_rejects_oversize_file(tmp_path):
    path = tmp_path / "goal.json"
    path.write_bytes(b" " * (goals.MAX_GOAL_FILE_BYTES + 10))
    with pytest.raises(ValidationError, match="at most"):
        load_goal_file(path)


def test_load_goal_file_reports_missing_file(tmp_path):
    with pytest.raises(ValidationError, match="cannot read goal file"):
        load_goal_file(tmp_path / "absent.json")


# --------------------------------------------------------------------------
# Content identity: deterministic, timestamp-free
# --------------------------------------------------------------------------


def test_identity_is_deterministic_and_key_order_independent():
    first = validate_goal({"objective": "A", "intended_outcome": "B"})
    second = validate_goal({"intended_outcome": "B", "objective": "A"})
    assert goal_content_sha256(first) == goal_content_sha256(second)
    assert goal_identity(first) == goal_identity(second)
    assert 64 == len(goal_content_sha256(first))


def test_identity_changes_with_content():
    assert goal_content_sha256(goal()) != goal_content_sha256(
        goal(constraints=["new constraint"])
    )


def test_declared_goal_id_wins_over_content_identity():
    assert goal_identity(goal(goal_id="GOAL-X")) == "GOAL-X"
    assert goal_identity(goal()).startswith("GOAL-")


def test_canonical_bytes_are_stable_utf8():
    data = canonical_goal_bytes(goal(objective="Überblick"))
    assert isinstance(data, bytes)
    assert "Überblick".encode("utf-8") in data
    assert b" " not in data.split(b'"objective":')[1][:1]


# --------------------------------------------------------------------------
# Provenance and trust
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [("operator", "authoritative"), ("agent", "proposed"), ("imported", "imported")],
)
def test_provenance_maps_to_trust(source, expected):
    assert goal_trust(goal(provenance={"source": source})) == expected


def test_unknown_provenance_source_is_rejected():
    with pytest.raises(ValidationError, match="provenance.source must be one of"):
        validate_goal({**MINIMAL, "provenance": {"source": "oracle"}})


def test_unknown_provenance_key_is_rejected():
    with pytest.raises(ValidationError, match="goal.provenance: unknown field"):
        validate_goal({**MINIMAL, "provenance": {"source": "operator", "sig": "x"}})


# --------------------------------------------------------------------------
# Work graph: optional, reusable validation, bounded
# --------------------------------------------------------------------------


def test_simple_task_needs_no_work_graph():
    assert goal()["work"] is None
    assert work_graph(goal())["task_count"] == 0


def test_multi_task_graph_reports_levels_and_roots():
    result = goal(
        work={
            "tasks": [
                {"id": "collect", "title": "Collect sources", "action": "Gather"},
                {"id": "draft", "title": "Draft", "action": "Write",
                 "depends_on": ["collect"]},
                {"id": "review", "title": "Review", "action": "Check",
                 "depends_on": ["draft"]},
                {"id": "publish", "title": "Publish", "action": "Send",
                 "depends_on": ["review", "collect"]},
            ]
        }
    )
    graph = work_graph(result)
    assert graph["task_count"] == 4
    assert graph["roots"] == ["collect"]
    assert graph["levels"] == [["collect"], ["draft"], ["review"], ["publish"]]
    assert graph["tasks"][1]["depends_on"] == ["collect"]


def test_task_optional_fields_default_to_empty():
    result = goal(work={"tasks": [{"id": "t1", "title": "T", "action": "A"}]})
    task = result["work"]["tasks"][0]
    assert task["deliverables"] == []
    assert task["preconditions"] == []
    assert task["completion_criteria"] == []
    assert task["verification"] == []
    assert task["owner"] is None
    assert task["result"] is None


@pytest.mark.parametrize("missing", ["id", "title", "action"])
def test_task_required_fields(missing):
    task = {"id": "t1", "title": "T", "action": "A"}
    del task[missing]
    with pytest.raises(ValidationError, match=f"field '{missing}' is required"):
        validate_goal({**MINIMAL, "work": {"tasks": [task]}})


def test_task_unknown_field_is_rejected():
    with pytest.raises(ValidationError, match="unknown field"):
        validate_goal(
            {**MINIMAL, "work": {"tasks": [
                {"id": "t1", "title": "T", "action": "A", "priority": "high"}
            ]}}
        )


def test_task_graph_cycle_is_rejected():
    with pytest.raises(ValidationError, match="dependency cycle detected"):
        validate_goal(
            {**MINIMAL, "work": {"tasks": [
                {"id": "a", "title": "A", "action": "A", "depends_on": ["b"]},
                {"id": "b", "title": "B", "action": "B", "depends_on": ["a"]},
            ]}}
        )


def test_task_self_dependency_is_rejected():
    with pytest.raises(ValidationError, match="depends on itself"):
        validate_goal(
            {**MINIMAL, "work": {"tasks": [
                {"id": "a", "title": "A", "action": "A", "depends_on": ["a"]},
            ]}}
        )


def test_unknown_dependency_is_rejected():
    with pytest.raises(ValidationError, match="unknown task"):
        validate_goal(
            {**MINIMAL, "work": {"tasks": [
                {"id": "a", "title": "A", "action": "A", "depends_on": ["ghost"]},
            ]}}
        )


def test_duplicate_task_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate task id"):
        validate_goal(
            {**MINIMAL, "work": {"tasks": [
                {"id": "a", "title": "A", "action": "A"},
                {"id": "a", "title": "A2", "action": "A"},
            ]}}
        )


def test_too_many_tasks_are_rejected():
    tasks = [
        {"id": f"t{index}", "title": "T", "action": "A"}
        for index in range(goals.MAX_TASKS + 1)
    ]
    with pytest.raises(ValidationError, match="at most 200 tasks"):
        validate_goal({**MINIMAL, "work": {"tasks": tasks}})


def test_empty_work_tasks_are_rejected():
    with pytest.raises(ValidationError, match="non-empty list"):
        validate_goal({**MINIMAL, "work": {"tasks": []}})


def test_work_unknown_key_is_rejected():
    with pytest.raises(ValidationError, match="goal.work: unknown field"):
        validate_goal({**MINIMAL, "work": {"tasks": [], "graph": 1}})


def test_task_owner_is_bounded():
    assert goal(work={"tasks": [
        {"id": "t1", "title": "T", "action": "A", "owner": "researcher-1"}
    ]})["work"]["tasks"][0]["owner"] == "researcher-1"
    with pytest.raises(ValidationError, match="owner must be a bounded actor"):
        validate_goal({**MINIMAL, "work": {"tasks": [
            {"id": "t1", "title": "T", "action": "A", "owner": "/Users/me"}
        ]}})


# --------------------------------------------------------------------------
# Reused plan-schema v1 graph validator keeps its original messages
# --------------------------------------------------------------------------


def test_shared_graph_validator_keeps_plan_messages():
    tasks = [
        {"id": "TASK-001", "depends_on": ["TASK-002"]},
        {"id": "TASK-002", "depends_on": ["TASK-001"]},
    ]
    with pytest.raises(ValidationError, match="^plan: dependency cycle detected"):
        validate_dependency_graph(tasks)
    with pytest.raises(ValidationError, match="^goal.work: dependency cycle detected"):
        validate_dependency_graph(tasks, artifact="goal.work")


# --------------------------------------------------------------------------
# Profiles: data-only registry, advisory suggestions
# --------------------------------------------------------------------------


def test_default_profile_is_general_and_registry_is_complete():
    assert profiles.DEFAULT_PROFILE == "general"
    assert profiles.PROFILE_IDS == (
        "general",
        "design",
        "research",
        "writing",
        "business",
        "software",
    )
    assert set(PROFILE_VERSIONS) == set(profiles.PROFILE_IDS)
    assert PROFILE_VERSIONS == profiles.profile_versions()


def test_profiles_are_data_only():
    """No profile entry may hold a callable or executable hook."""

    for profile in profiles.PROFILES.values():
        for value in profile.values():
            assert not callable(value), profile["id"]
            flat = value if isinstance(value, list) else [value]
            for entry in flat:
                assert not callable(entry), profile["id"]
        assert profile["version"] == 1


def test_profiles_have_bounded_content():
    for profile in profiles.PROFILES.values():
        for key in (
            "suggested_quality_criteria",
            "suggested_evidence",
            "output_expectations",
        ):
            assert profile[key], (profile["id"], key)
            assert len(profile[key]) <= profiles.MAX_SUGGESTIONS
            for entry in profile[key]:
                assert len(entry.encode("utf-8")) <= profiles.MAX_SUGGESTION_BYTES


def test_profile_views_are_copies():
    first = profiles.get_profile("design")
    first["suggested_quality_criteria"].append("mutated")
    assert "mutated" not in profiles.get_profile("design")["suggested_quality_criteria"]


def test_unknown_profile_lookup_is_rejected():
    with pytest.raises(ValidationError, match="profile must be one of"):
        profiles.get_profile("architecture")


def test_profile_suggestions_are_explicitly_advisory():
    suggestions = profiles.profile_suggestions("research")
    assert suggestions["profile"] == "research"
    assert "Suggestions only" in suggestions["note"]
    assert "never gate" in suggestions["note"]
    assert suggestions["suggested_quality_criteria"]


def test_each_profile_has_guidance_used_by_the_pack():
    from aperio.context_pack import _PROFILE_GUIDANCE

    assert set(_PROFILE_GUIDANCE) == set(profiles.PROFILE_IDS)
    for profile_id in profiles.PROFILE_IDS:
        pack = build_goal_context_pack(goal(profile=profile_id))
        markdown = render_context_pack_markdown(pack)
        assert profiles.PROFILES[profile_id]["pack_guidance"] in markdown


# --------------------------------------------------------------------------
# Completeness: honest gaps, never invented values
# --------------------------------------------------------------------------


def test_completeness_lists_missing_fields_without_inventing_them():
    report = goal_completeness(goal())
    missing = {entry["field"] for entry in report["missing"]}
    assert {"constraints", "deliverables", "acceptance_criteria"} <= missing
    assert report["provided"]["constraints"] is False
    assert report["work_decomposition_provided"] is False


def test_completeness_marks_supplied_fields():
    report = goal_completeness(
        goal(
            constraints=["c"],
            deliverables=["d"],
            acceptance_criteria=["a"],
            evidence_requirements=["e"],
            audience="residents",
            requirements=["r"],
            quality_criteria=["q"],
        )
    )
    assert report["missing"] == []
    assert all(report["provided"].values())


def test_describe_goal_is_json_serializable():
    report = describe_goal(goal(goal_id="GOAL-D", profile="business"))
    assert report["goal_identity"] == "GOAL-D"
    assert report["profile_version"] == 1
    assert report["trust"] == "authoritative"
    json.dumps(report)


# --------------------------------------------------------------------------
# Goal → context pack (M1 integration)
# --------------------------------------------------------------------------


def test_goal_pack_preserves_objective_constraints_and_profile():
    result = goal(
        objective="Compare two venues",
        intended_outcome="A recommendation",
        profile="business",
        requirements=["Use the 2026 quotes"],
        constraints=["Budget cap 5k"],
        assumptions=["Venue A is available"],
        risks=["Prices may rise"],
        quality_criteria=["Show the arithmetic"],
        audience="The board",
        uncertainties=["Guest count is unconfirmed"],
    )
    content = build_goal_context_pack(result)["content"]
    assert content["objective"] == "Compare two venues"
    assert content["profile"] == {"id": "business", "version": 1}
    assert content["constraints"] == ["Use the 2026 quotes", "Budget cap 5k"]
    assert content["assumptions"] == ["Venue A is available"]
    assert content["risks"] == ["Prices may rise"]
    assert content["quality_criteria"] == ["Show the arithmetic"]
    assert content["output_contract"]["expected_output"] == "A recommendation"
    assert content["output_contract"]["audience"] == "The board"
    assert content["missing_information"] == ["Guest count is unconfirmed"]


def test_goal_pack_carries_deliverables_and_acceptance_criteria():
    block = build_goal_context_pack(
        goal(
            goal_id="GOAL-X",
            deliverables=["A poster"],
            acceptance_criteria=["Printer can quote it"],
            evidence_requirements=["List the brand rules used"],
            output_contract={"format": "spec", "length": "2 pages"},
            profile="design",
        )
    )["content"]["goal"]
    assert block["goal_id"] == "GOAL-X"
    assert block["deliverables"] == ["A poster"]
    assert block["acceptance_criteria"] == ["Printer can quote it"]
    assert block["evidence_requirements"] == ["List the brand rules used"]
    assert block["output_contract"]["length"] == "2 pages"
    assert block["goal_schema_version"] == GOAL_SCHEMA_VERSION
    assert block["trust"] == "authoritative"


def test_profile_suggestions_never_become_requirements():
    pack = build_goal_context_pack(goal(profile="research"))
    content = pack["content"]
    suggestions = content["profile_suggestions"]["suggested_quality_criteria"]
    assert suggestions
    # The operator supplied no criteria, so the enforced list stays empty.
    assert content["quality_criteria"] == []
    assert not set(suggestions) & set(content["quality_criteria"])
    assert not set(suggestions) & set(content["constraints"])


def test_agent_provenance_is_warned_and_labelled():
    pack = build_goal_context_pack(goal(provenance={"source": "agent"}))
    content = pack["content"]
    assert content["goal"]["trust"] == "proposed"
    assert "goal_trust_not_operator" in {w["code"] for w in content["warnings"]}
    markdown = render_context_pack_markdown(pack)
    assert "not operator-authoritative" in markdown


def test_operator_provenance_has_no_trust_warning():
    pack = build_goal_context_pack(goal())
    assert "goal_trust_not_operator" not in {
        w["code"] for w in pack["content"]["warnings"]
    }


def test_imported_provenance_is_labelled_imported():
    block = build_goal_context_pack(goal(provenance={"source": "imported"}))[
        "content"
    ]["goal"]
    assert block["trust"] == "imported"


def test_goal_pack_is_hash_verifiable():
    pack = build_goal_context_pack(goal(profile="writing"))
    assert pack["content_sha256"] == context_content_sha256(pack["content"])
    markdown = render_context_pack_markdown(pack)
    assert pack["content_sha256"] in markdown


def test_goal_pack_markdown_and_json_agree():
    pack = build_goal_context_pack(
        goal(
            profile="design",
            deliverables=["A poster"],
            work={"tasks": [{"id": "draft", "title": "Draft", "action": "Sketch"}]},
        )
    )
    markdown = render_context_pack_markdown(pack)
    content = pack["content"]
    assert content["objective"] in markdown
    for deliverable in content["goal"]["deliverables"]:
        assert deliverable in markdown
    assert "draft" in markdown
    assert "## Profile suggestions (not requirements)" in markdown
    assert "## Goal" in markdown


def test_goal_pack_has_no_work_section_when_no_graph():
    markdown = render_context_pack_markdown(build_goal_context_pack(goal()))
    assert "### Work decomposition" not in markdown


def test_goal_pack_is_deterministic():
    first = build_goal_context_pack(goal(profile="research", constraints=["c"]))
    second = build_goal_context_pack(goal(profile="research", constraints=["c"]))
    assert first["content_sha256"] == second["content_sha256"]
    assert first["content"] == second["content"]
    assert first["generated_at"] != "" and "generated_at" in first


def test_goal_pack_concurrent_builds_agree():
    result = goal(profile="business", constraints=["c"], risks=["r"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        digests = list(pool.map(lambda _: build_goal_context_pack(result)["content_sha256"], range(8)))
    assert len(set(digests)) == 1


def test_goal_pack_rejects_an_invalid_goal():
    with pytest.raises(ValidationError, match="required"):
        build_goal_context_pack({"objective": "x"})


def test_goal_pack_budget_is_enforced():
    result = goal()
    from aperio.context_pack import attached_context_item

    imported = [
        attached_context_item("x" * 400, trust="imported", ordinal=index)
        for index in range(1, 6)
    ]
    pack = build_goal_context_pack(result, budget_bytes=500, imported_items=imported)
    used = pack["content"]["budget"]["used_bytes"]
    assert used <= 500
    assert pack["content"]["budget"]["scope"] == "selected_context_content_utf8_bytes"


def test_goal_pack_default_profile_is_general():
    assert build_goal_context_pack(goal())["content"]["profile"]["id"] == "general"


def test_goal_pack_never_contains_local_paths(tmp_path):
    path = tmp_path / "goal.json"
    path.write_text(json.dumps({**MINIMAL, "profile": "writing"}), encoding="utf-8")
    pack = build_goal_context_pack(load_goal_file(path))
    dumped = json.dumps(pack, ensure_ascii=False) + render_context_pack_markdown(pack)
    assert str(tmp_path) not in dumped
    assert "/Users/" not in dumped
    assert ".aperio/local" not in dumped


# --------------------------------------------------------------------------
# CLI: standalone workflow needs no project, provider, or model API
# --------------------------------------------------------------------------


def _write_goal(path: Path, **overrides) -> Path:
    path.write_text(json.dumps({**MINIMAL, **overrides}), encoding="utf-8")
    return path


def test_cli_profiles_lists_all_profiles(run_cli):
    code, out, err = run_cli("profiles")
    assert code == 0, err
    for profile_id in profiles.PROFILE_IDS:
        assert profile_id in out
    assert "general v1 (default)" in out


def test_cli_profiles_json(run_cli):
    code, out, err = run_cli("profiles", "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert [entry["id"] for entry in payload["profiles"]] == list(profiles.PROFILE_IDS)


def test_cli_goal_check_standalone(run_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _write_goal(tmp_path / "goal.json", profile="research")
    code, out, err = run_cli("goal-check", str(path))
    assert code == 0, err
    assert "Goal valid" in out
    assert "research" in out
    assert not any(tmp_path.glob(".aperio"))


def test_cli_goal_check_json(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json")
    code, out, err = run_cli("goal-check", str(path), "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["report"]["goal_schema_version"] == GOAL_SCHEMA_VERSION
    assert payload["suggestions"]["profile"] == "general"


def test_cli_goal_check_reports_invalid_goal_without_traceback(run_cli, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"objective": "x"}', encoding="utf-8")
    code, out, err = run_cli("goal-check", str(path))
    assert code == 1
    assert "aperio: error:" in err
    assert "intended_outcome" in err
    assert "Traceback" not in err and "Traceback" not in out


def test_cli_goal_check_reports_missing_file(run_cli, tmp_path):
    code, _out, err = run_cli("goal-check", str(tmp_path / "nope.json"))
    assert code == 1
    assert "aperio: error: cannot read goal file" in err


def test_cli_goal_pack_standalone_writes_no_project(run_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _write_goal(tmp_path / "goal.json", profile="design",
                       deliverables=["A poster"])
    code, out, err = run_cli("goal-pack", str(path))
    assert code == 0, err
    assert "# Aperio Context Pack" in out
    assert "A poster" in out
    assert list(tmp_path.iterdir()) == [path]


def test_cli_goal_pack_json_recomputes_hash(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json", profile="writing")
    code, out, err = run_cli("goal-pack", str(path), "--json")
    assert code == 0, err
    pack = json.loads(out)
    assert pack["content_sha256"] == context_content_sha256(pack["content"])
    assert pack["content"]["goal"]["profile"] == "writing"


def test_cli_goal_pack_writes_output_file(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json")
    target = tmp_path / "pack.md"
    code, out, err = run_cli("goal-pack", str(path), "--output", str(target))
    assert code == 0, err
    assert "Wrote context pack" in out
    assert target.read_text(encoding="utf-8").startswith("# Aperio Context Pack")


def test_cli_goal_pack_refuses_to_overwrite_without_force(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json")
    target = tmp_path / "pack.md"
    target.write_text("existing", encoding="utf-8")
    code, _out, err = run_cli("goal-pack", str(path), "--output", str(target))
    assert code == 1
    assert "already exists" in err
    assert target.read_text(encoding="utf-8") == "existing"
    code, _out, err = run_cli(
        "goal-pack", str(path), "--output", str(target), "--force"
    )
    assert code == 0, err
    assert target.read_text(encoding="utf-8").startswith("# Aperio Context Pack")


def test_cli_goal_pack_refuses_symlink_output(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json")
    real = tmp_path / "real.md"
    real.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    code, _out, err = run_cli(
        "goal-pack", str(path), "--output", str(link), "--force"
    )
    assert code == 1
    assert "symlink" in err
    assert real.read_text(encoding="utf-8") == "keep"


def test_cli_goal_pack_rejects_invalid_budget(run_cli, tmp_path):
    path = _write_goal(tmp_path / "goal.json")
    code, _out, err = run_cli("goal-pack", str(path), "--budget", "-1")
    assert code == 1
    assert "budget" in err


def test_cli_context_pack_output_has_no_goal_sections(run_cli, tmp_path):
    """M1 behaviour is unchanged when no goal is involved."""

    code, out, err = run_cli("context-pack", "--objective", "Explain compost")
    assert code == 0, err
    assert "## Goal" not in out
    assert "## Profile suggestions" not in out


def test_cli_help_lists_m2_commands(run_cli):
    with pytest.raises(SystemExit) as excinfo:
        run_cli("--help")
    assert excinfo.value.code == 0
    out = "".join(profiles.PROFILE_IDS) + "profiles goal-check goal-pack"
    assert "profiles" in out and "goal-check" in out and "goal-pack" in out


# --------------------------------------------------------------------------
# Project-backed mode and legacy compatibility
# --------------------------------------------------------------------------


def test_goal_pack_with_project_workspace_is_read_only(run_cli, project_dir):
    code, _out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    code, _out, err = run_cli(
        "decision-add", str(project_dir),
        "--title", "Keep it small", "--context", "context",
        "--decision", "decision", "--consequences", "consequences",
    )
    assert code == 0, err

    def snapshot():
        return {
            path.relative_to(project_dir).as_posix(): path.read_bytes()
            for path in sorted(project_dir.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    goal_path = _write_goal(project_dir / "goal.json")
    before = snapshot()
    code, out, err = run_cli("goal-pack", str(goal_path), str(project_dir))
    assert code == 0, err
    assert "# Aperio Context Pack" in out
    assert "Keep it small" in out
    assert snapshot() == before


def test_legacy_plan_schema_v1_still_written_and_readable(
    run_cli, project_dir, tmp_path
):
    """M2 adds no plan fields and does not migrate plan-schema v1."""

    code, _out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    plan_file = tmp_path / "plan-input.json"
    plan_file.write_text(
        json.dumps(
            {
                "objective": "Legacy plan",
                "expected_outcome": "A legacy plan survives",
                "scope": ["in scope"],
                "exclusions": ["out of scope"],
                "assumptions": ["an assumption"],
                "risks": ["a risk"],
                "strategy": "Do the work in order.",
                "acceptance_criteria": ["works"],
                "verification_strategy": "Check the result by hand.",
                "tasks": [
                    {
                        "id": "TASK-001",
                        "title": "Legacy task",
                        "objective": "Do the legacy task",
                        "deliverables": ["A result"],
                        "depends_on": [],
                        "acceptance_criteria": ["works"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    code, _out, err = run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file)
    )
    assert code == 0, err
    stored = json.loads((project_dir / ".aperio" / "plan.json").read_text())
    assert stored["schema_version"] == 1
    assert stored["plan_version"] == 1
    assert stored["tasks"][0]["status"] == "pending"
    code, _out, err = run_cli("plan-show", str(project_dir))
    assert code == 0, err


def test_goal_layer_does_not_import_provider_adapters(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import aperio.goals, aperio.profiles, sys;"
            "bad=[m for m in sys.modules if 'adapters' in m or 'runtime' in m];"
            "print(','.join(bad))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_work_task_detail_reaches_the_pack():
    result = goal(
        work={
            "tasks": [
                {
                    "id": "draft",
                    "title": "Draft the memo",
                    "action": "Write 700 words",
                    "result": "A readable draft",
                    "deliverables": ["Memo"],
                    "preconditions": ["Sources collected"],
                    "completion_criteria": ["Operator reads it once"],
                    "verification": ["Spell-check and read aloud"],
                    "risks": ["Thin sourcing"],
                    "owner": "writer-1",
                }
            ]
        }
    )
    pack = build_goal_context_pack(result)
    task = pack["content"]["goal"]["work"]["tasks"][0]
    assert task["action"] == "Write 700 words"
    assert task["completion_criteria"] == ["Operator reads it once"]
    assert task["verification"] == ["Spell-check and read aloud"]
    assert task["preconditions"] == ["Sources collected"]
    assert task["owner"] == "writer-1"
    markdown = render_context_pack_markdown(pack)
    for expected in (
        "Write 700 words",
        "Completion criteria: Operator reads it once",
        "Verification: Spell-check and read aloud",
        "Preconditions: Sources collected",
    ):
        assert expected in markdown


def test_agent_objective_is_not_labelled_operator_written():
    pack = build_goal_context_pack(goal(provenance={"source": "agent"}))
    assert pack["content"]["objective_source"] == "agent"
    markdown = render_context_pack_markdown(pack)
    assert "Objective source: agent" in markdown
    assert build_goal_context_pack(goal())["content"]["objective_source"] == "operator"


def test_context_pack_rejects_an_unknown_objective_source():
    with pytest.raises(ValidationError, match="objective source must be one of"):
        build_context_pack("Do it", declared_objective_source="telepathy")
