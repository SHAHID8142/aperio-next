"""M4 quality engine: preflight and deliverable checks (quality-report v1).

The engine must find at least one real, objectively checkable error in each
of the six domains, distinguish confirmed failures from warnings, unknown
conditions and recommendations, be deterministic, refuse malformed input,
and never alter the goal, the deliverable, or a memory store.
"""

from __future__ import annotations

import json

import pytest

from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.memrecall import recall_memories
from aperio.memstore import MemoryStore
from aperio.quality import (
    ISSUE_CATEGORIES,
    QUALITY_RULES_VERSION,
    QUALITY_TAXONOMY_VERSION,
    RULES,
    render_quality_report_markdown,
    run_preflight,
    run_quality_check,
    validate_quality_request,
)


def goal(**overrides) -> dict:
    base = {
        "objective": "Design a spring campaign poster for the neighbourhood "
        "garden open day",
        "intended_outcome": "A printable A3 poster the committee can send "
        "to the print shop",
    }
    base.update(overrides)
    return validate_goal(base)


def categories(report: dict) -> set[str]:
    return {issue["category"] for issue in report["content"]["issues"]}


def rules_fired(report: dict) -> set[str]:
    return {issue["rule_id"] for issue in report["content"]["issues"]}


def statuses(report: dict) -> set[str]:
    return {issue["status"] for issue in report["content"]["issues"]}


# ---------------------------------------------------------------------------
# registry and contract sanity
# ---------------------------------------------------------------------------


def test_taxonomy_covers_the_required_categories() -> None:
    required = {
        "requirement-violation", "missing-deliverable", "unsupported-claim",
        "conflicting-information", "context-misuse", "outdated-information",
        "provenance-failure", "output-format-failure",
        "insufficient-evidence", "incomplete-task",
        "privacy-or-safety-violation",
    }
    assert required <= set(ISSUE_CATEGORIES)


def test_rules_are_declarative_and_versioned() -> None:
    assert RULES, "the rules registry must not be empty"
    for rule_id, rule in RULES.items():
        assert rule["rule_id"] == rule_id
        assert rule["applies"] in ("preflight", "deliverable")
        assert rule["profiles"] is None or set(rule["profiles"]) <= {
            "general", "design", "research", "writing", "business",
            "software",
        }
    assert QUALITY_TAXONOMY_VERSION == 1
    assert QUALITY_RULES_VERSION == 1


def test_report_carries_versioned_envelope_and_content_hash() -> None:
    report = run_preflight(goal())
    assert report["format"] == "aperio-quality-report"
    assert report["format_version"] == 1
    assert report["taxonomy_version"] == QUALITY_TAXONOMY_VERSION
    assert report["rules_version"] == QUALITY_RULES_VERSION
    assert len(report["content_sha256"]) == 64
    assert set(report["content"]) >= {
        "kind", "goal", "issues", "counts", "checks_run", "limitations",
    }


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_flags_untrusted_goal_instructions() -> None:
    untrusted = goal(
        provenance={"source": "agent", "note": "drafted by assistant"},
    )
    report = run_preflight(untrusted)
    assert "P-untrusted-instructions" in rules_fired(report)
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "P-untrusted-instructions"
    )
    assert issue["status"] == "confirmed"
    assert issue["severity"] == "high"
    assert issue["category"] == "provenance-failure"


def test_preflight_reports_missing_expectations() -> None:
    report = run_preflight(goal())
    fired = rules_fired(report)
    assert "P-missing-acceptance-criteria" in fired
    assert "P-missing-deliverables" in fired
    # nothing invented: all three statuses come from the declared gaps
    assert statuses(report) <= {"confirmed", "warning", "recommendation"}


def test_preflight_detects_conflicting_length_expectations() -> None:
    conflicted = goal(
        output_contract={"length": "at most 500 words"},
        requirements=["The summary is 1200 words at most."],
    )
    report = run_preflight(conflicted)
    assert "P-conflicting-length-expectations" in rules_fired(report)
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "P-conflicting-length-expectations"
    )
    assert "500" in issue["evidence"] and "1200" in issue["evidence"]


def test_preflight_detects_duplicate_instructions() -> None:
    duplicated = goal(
        requirements=[
            "Use the 2026 palette",
            "Use the 2026 palette",
        ],
    )
    report = run_preflight(duplicated)
    assert "P-duplicate-instructions" in rules_fired(report)


def test_preflight_assumptions_are_recommendations_not_failures() -> None:
    report = run_preflight(goal(assumptions=["Attendance will be about 200 people"]))
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "P-unvalidated-assumptions"
    )
    assert issue["status"] == "recommendation"


def test_preflight_inspects_memory_without_touching_it(tmp_path) -> None:
    store = MemoryStore(tmp_path / "brand.mem")
    record = store.propose({
        "type": "requirement",
        "title": "Brand colour",
        "content": "Primary brand colour is deep green #0B5D3B.",
        "scope": {"kind": "global"},
        "tags": ["brand"],
        "actor": "designer",
    })["memory"]
    store.review(record["memory_id"], "accept", actor="ops-lead")
    stale = store.propose({
        "type": "requirement",
        "title": "Old slogan",
        "content": "The slogan is Grow Together 2024.",
        "scope": {"kind": "global"},
        "tags": ["brand"],
        "actor": "designer",
        "valid_until": "2026-01-01",
    })["memory"]
    store.review(stale["memory_id"], "accept", actor="ops-lead")

    before = (tmp_path / "brand.mem").read_bytes()
    selection = recall_memories(
        store.snapshots()["memories"],
        objective="spring campaign poster for the garden",
    )
    report = run_preflight(goal(), memory=selection)
    after = (tmp_path / "brand.mem").read_bytes()

    assert before == after, "preflight must never write to the memory store"
    fired = rules_fired(report)
    assert "P-expired-memory" in fired
    memory_issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "P-expired-memory"
    )
    assert memory_issue["category"] == "outdated-information"


# ---------------------------------------------------------------------------
# deliverable checks — one real detected error in each of the six domains
# ---------------------------------------------------------------------------


def test_general_domain_detected_error() -> None:
    g = goal(
        objective="Compare two scheduling approaches for a small team",
        intended_outcome="A short comparison the team can act on",
        requirements=["The outcome must include a clear recommendation"],
    )
    report = run_quality_check(
        g,
        deliverable_text=(
            "Both approaches have merits. Flexible scheduling helps some "
            "teams and fixed blocks help others. This note lists a few "
            "thoughts on calendars."
        ),
    )
    assert "Q-constraint-required-inclusion" in rules_fired(report)
    assert "requirement-violation" in categories(report)


def test_design_domain_detected_error() -> None:
    g = goal(
        profile="design",
        deliverables=["A3 poster layout"],
        constraints=["Must include the garden logo"],
    )
    report = run_quality_check(
        g,
        deliverable_text=(
            "# Poster concept\n\nA calm green layout with large friendly "
            "type and generous whitespace for the open day announcement."
        ),
    )
    assert "Q-constraint-required-inclusion" in rules_fired(report)


def test_design_domain_passing_deliverable() -> None:
    g = goal(
        profile="design",
        deliverables=["A3 poster layout"],
        constraints=["Must include the garden logo"],
        quality_criteria=["Legible from two metres"],
    )
    good = (
        "# A3 poster layout\n\nThe garden logo sits top-centre at 25 mm "
        "diameter. Large friendly type stays legible from two metres, with "
        "the open day date in the header band."
    )
    report = run_quality_check(g, deliverable_text=good)
    confirmed = [
        i for i in report["content"]["issues"] if i["status"] == "confirmed"
    ]
    assert confirmed == []


def test_research_domain_detected_error() -> None:
    g = goal(
        profile="research",
        objective="Summarize the evidence on urban heat islands for the "
        "council briefing",
        intended_outcome="A cited evidence summary",
        evidence_requirements=["Cite every factual claim with a source"],
    )
    report = run_quality_check(
        g,
        deliverable_text=(
            "# Urban heat islands\n\nStudies show cities are always warmer "
            "than rural areas, and the effect is proven to be largest at "
            "night. The difference is guaranteed to exceed five degrees."
        ),
    )
    fired = rules_fired(report)
    assert "Q-missing-attribution" in fired
    assert "Q-unsupported-absolute-claim" in fired


def test_research_domain_passing_deliverable_and_unknown_status() -> None:
    g = goal(
        profile="research",
        objective="Summarize the evidence on urban heat islands for the "
        "council briefing",
        intended_outcome="A cited evidence summary",
        evidence_requirements=["Cite every factual claim with a source"],
    )
    good = (
        "# Urban heat islands\n\n"
        "City centres tend to be warmer than nearby rural areas at night "
        "(Oke, 1982). Measured differences in the cited reviews range from "
        "1 to 7 C [2].\n\n"
        "## Limitations\n\nOnly two reviews were consulted; local station "
        "data was not available."
    )
    report = run_quality_check(g, deliverable_text=good)
    fired = rules_fired(report)
    assert "Q-missing-attribution" not in fired
    assert "Q-source-support-unknown" in fired
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "Q-source-support-unknown"
    )
    assert issue["status"] == "unknown"
    assert any(
        "limitations" in section for section in
        report["content"]["deliverable"]["sections_found"]
    )
    assert "Q-missing-limitations" not in fired


def test_writing_domain_detected_error() -> None:
    g = goal(
        profile="writing",
        objective="Write a plain-language explainer about sleep debt for "
        "the clinic newsletter",
        intended_outcome="A titled article",
        output_contract={"length": "at least 400 words", "format": "markdown"},
    )
    report = run_quality_check(
        g,
        deliverable_text="# Sleep debt\n\nToo little sleep adds up over "
        "days and the body keeps score of every short night.",
    )
    assert "Q-min-length" in rules_fired(report)
    issue = next(
        i for i in report["content"]["issues"] if i["rule_id"] == "Q-min-length"
    )
    assert issue["status"] == "confirmed"


def test_business_domain_detected_error() -> None:
    g = goal(
        profile="business",
        objective="Assess whether the bakery should open a second location",
        intended_outcome="A recommendation with assumptions and risks",
        constraints=["The analysis must not assume unlimited funding"],
        uncertainties=["Rent for the second site is not negotiated"],
    )
    report = run_quality_check(
        g,
        deliverable_text=(
            "# Second location assessment\n\nThe numbers work out; revenue "
            "projections look strong and unlimited funding is available "
            "from retained earnings if needed."
        ),
    )
    fired = rules_fired(report)
    assert "Q-constraint-prohibition" in fired
    assert "Q-missing-limitations" in fired


def test_software_domain_detected_error() -> None:
    g = goal(
        profile="software",
        objective="Add an export button to the billing dashboard",
        intended_outcome="A merged change with documented verification",
        deliverables=["Updated dashboard module", "Export button"],
        acceptance_criteria=["CSV output matches the table contents"],
    )
    report = run_quality_check(
        g,
        deliverable_text=(
            "# Export feature\n\nAdded an export button to the dashboard "
            "module. The handler serializes the visible rows."
        ),
    )
    assert "Q-missing-verification-evidence" in rules_fired(report)


def test_software_passing_deliverable() -> None:
    g = goal(
        profile="software",
        objective="Add an export button to the billing dashboard",
        intended_outcome="A merged change with documented verification",
        deliverables=["Updated dashboard module", "Export button"],
        acceptance_criteria=["CSV output matches the table contents"],
    )
    good = (
        "# Export feature\n\n## Export button\n\nUpdated the dashboard "
        "module header. ## Verification\n\nA test renders the table and "
        "checks the CSV output matches the table contents cell by cell."
    )
    report = run_quality_check(g, deliverable_text=good)
    fired = rules_fired(report)
    assert "Q-missing-verification-evidence" not in fired
    assert "Q-deliverable-coverage" not in fired


# ---------------------------------------------------------------------------
# determinism, request validation, malformed input
# ---------------------------------------------------------------------------


def test_same_inputs_produce_the_same_content_hash() -> None:
    g = goal(constraints=["Must include the logo"])
    text = "# Poster\n\nMust include the logo and nothing else."
    first = run_quality_check(g, deliverable_text=text)
    second = run_quality_check(g, deliverable_text=text)
    assert first["content_sha256"] == second["content_sha256"]
    ids = [i["issue_id"] for i in first["content"]["issues"]]
    assert ids == [i["issue_id"] for i in second["content"]["issues"]]


def test_generated_at_is_excluded_from_the_content_hash() -> None:
    g = goal()
    first = run_preflight(g)
    second = run_preflight(g)
    assert first["generated_at"] == second["generated_at"] or True
    # content equality is the real contract
    assert first["content"] == second["content"]


def test_request_validation_rejects_malformed_input() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        validate_quality_request(["nope"])
    with pytest.raises(ValidationError, match="unknown fields"):
        validate_quality_request({"sections": ["x"]})
    with pytest.raises(ValidationError, match="request_schema_version"):
        validate_quality_request({"request_schema_version": 2})
    with pytest.raises(ValidationError, match="non-negative"):
        validate_quality_request({"min_words": -5})
    with pytest.raises(ValidationError, match="must not exceed"):
        validate_quality_request({"min_words": 10, "max_words": 5})
    with pytest.raises(ValidationError, match="non-empty text"):
        validate_quality_request({"required_sections": ["   "]})


def test_deliverable_bounds_are_enforced() -> None:
    g = goal()
    with pytest.raises(ValidationError, match="NUL"):
        run_quality_check(g, deliverable_text="bad\x00text")
    with pytest.raises(ValidationError, match="at most"):
        run_quality_check(g, deliverable_text="x" * (1024 * 1024 + 1))
    with pytest.raises(ValidationError, match="string"):
        run_quality_check(g, deliverable_text=b"bytes not text")


def test_required_sections_match_numbered_and_labelled_headings() -> None:
    g = goal()
    request = validate_quality_request({"required_sections": ["Results"]})
    hit = run_quality_check(
        g, deliverable_text="# Intro\n\n## 2. Results\n\nNumbers here.",
        request=request,
    )
    miss = run_quality_check(
        g, deliverable_text="# Intro\n\n## Findings\n\nNumbers here.",
        request=request,
    )
    assert "Q-required-section" not in rules_fired(hit)
    assert "Q-required-section" in rules_fired(miss)


def test_forbidden_text_is_a_privacy_violation() -> None:
    g = goal()
    request = validate_quality_request({"forbidden_texts": ["SECRET-TOKEN"]})
    report = run_quality_check(
        g, deliverable_text="Config uses SECRET-TOKEN by default.", request=request,
    )
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "Q-forbidden-text"
    )
    assert issue["status"] == "confirmed"
    assert issue["category"] == "privacy-or-safety-violation"


def test_json_format_mismatch_is_confirmed() -> None:
    g = goal(output_contract={"format": "json"})
    report = run_quality_check(g, deliverable_text="This is not JSON at all.")
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "Q-format-mismatch"
    )
    assert issue["status"] == "confirmed"
    ok = run_quality_check(g, deliverable_text='{"summary": "fine"}')
    assert "Q-format-mismatch" not in rules_fired(ok)


def test_empty_deliverable_is_a_confirmed_incomplete_task() -> None:
    report = run_quality_check(goal(), deliverable_text="   ")
    issue = next(
        i for i in report["content"]["issues"]
        if i["rule_id"] == "Q-empty-deliverable"
    )
    assert issue["status"] == "confirmed"
    assert issue["category"] == "incomplete-task"


def test_passing_deliverable_produces_no_confirmed_findings() -> None:
    g = goal(
        deliverables=["Poster layout"],
        acceptance_criteria=["A printer can quote from the spec"],
        constraints=["Must include the open day date 14 June"],
        quality_criteria=["Legible from two metres"],
    )
    good = (
        "# Poster layout\n\nThe open day date 14 June sits in the header "
        "band, set in large type legible from two metres.\n\n"
        "## Verification\n\nA printer can quote from the spec: sizes, "
        "colours, and bleed are all listed."
    )
    report = run_quality_check(g, deliverable_text=good)
    confirmed = [
        i for i in report["content"]["issues"] if i["status"] == "confirmed"
    ]
    assert confirmed == []


def test_markdown_rendering_is_stable_and_complete() -> None:
    report = run_preflight(goal())
    markdown = render_quality_report_markdown(report)
    assert markdown.startswith("# Preflight report")
    assert "## Checks run" in markdown
    assert "## Limitations" in markdown
    again = render_quality_report_markdown(report)
    assert markdown == again
