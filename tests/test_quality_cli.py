"""CLI tests for the M4 commands: ``preflight`` and ``quality-check``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.memstore import MemoryStore


def _goal_file(tmp_path: Path, data: dict, name: str = "goal.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


GOOD_GOAL = {
    "objective": "Write a plain-language explainer about sleep debt for "
    "the clinic newsletter",
    "intended_outcome": "A titled article the editor can publish",
    "deliverables": ["Titled article"],
    "acceptance_criteria": ["A reader can act on the advice"],
    "output_contract": {"length": "at least 400 words"},
}


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_reports_findings_as_markdown(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    code, out, err = run_cli("preflight", str(goal_path))
    assert code == 0
    assert err == ""
    assert out.startswith("# Preflight report")
    assert "## Checks run" in out


def test_preflight_json_is_the_canonical_payload(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    code, out, _ = run_cli("preflight", str(goal_path), "--json")
    assert code == 0
    report = json.loads(out)
    assert report["format"] == "aperio-quality-report"
    assert report["content"]["kind"] == "preflight"
    assert report["content"]["goal"]["profile"] == "general"


def test_preflight_with_memory_store_is_read_only(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    store_path = tmp_path / "notes.mem"
    store = MemoryStore(store_path)
    record = store.propose({
        "type": "requirement",
        "title": "Clinic tone",
        "content": "The clinic tone is warm and plain.",
        "scope": {"kind": "global"},
        "tags": ["tone"],
        "actor": "editor",
        "valid_until": "2026-01-01",
    })["memory"]
    store.review(record["memory_id"], "accept", actor="ops-lead")
    before = store_path.read_bytes()

    code, out, _ = run_cli(
        "preflight", str(goal_path), "--memory-store", str(store_path)
    )
    assert code == 0
    assert store_path.read_bytes() == before
    assert "outdated-information" in out


def test_preflight_rejects_malformed_goal(run_cli, tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.json", "{not json")
    code, out, err = run_cli("preflight", str(bad))
    assert code == 1
    assert out == ""
    assert err.startswith("aperio: error:")
    assert "Traceback" not in err


def test_preflight_output_writes_report_file(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    output = tmp_path / "report.md"
    code, out, _ = run_cli(
        "preflight", str(goal_path), "--output", str(output)
    )
    assert code == 0
    assert output.exists()
    assert output.read_text(encoding="utf-8").startswith("# Preflight report")
    assert "Wrote quality report" in out


def test_preflight_refuses_to_overwrite_output_without_force(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    output = _write(tmp_path, "report.md", "previous content")
    code, _, err = run_cli(
        "preflight", str(goal_path), "--output", str(output)
    )
    assert code == 1
    assert err.startswith("aperio: error:")
    assert output.read_text(encoding="utf-8") == "previous content"


# ---------------------------------------------------------------------------
# quality-check
# ---------------------------------------------------------------------------


def test_quality_check_detects_failing_deliverable(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(tmp_path, "draft.md", "# Sleep debt\n\nShort note.")
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable)
    )
    assert code == 0, "findings are a report, not a command failure"
    assert "Q-min-length" in out
    assert "confirmed failure" in out


def test_quality_check_passing_deliverable_has_no_confirmed_findings(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    body = (
        "# Sleep debt\n\nSleep debt builds when nights run short and the "
        "body keeps score of every missed hour. " * 8
        + "\n\n## Acting on it\n\nA reader can act on the advice by "
        "scheduling an earlier bedtime for two weeks and tracking energy."
    )
    deliverable = _write(tmp_path, "draft.md", body)
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable)
    )
    assert code == 0
    assert "No findings" in out or "confirmed" not in out.split("finding")[0]


def test_quality_check_json_report(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(tmp_path, "draft.md", "# Sleep debt\n\nShort.")
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable),
        "--json",
    )
    assert code == 0
    report = json.loads(out)
    assert report["content"]["kind"] == "deliverable"
    assert report["content"]["deliverable"]["words"] >= 1


def test_quality_check_request_flags_and_file_agree(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(tmp_path, "draft.md", "No sections here at all.")
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable),
        "--require-section", "Results", "--json",
    )
    assert code == 0
    fired = {
        issue["rule_id"] for issue in json.loads(out)["content"]["issues"]
    }
    assert "Q-required-section" in fired

    request_path = _write(
        tmp_path,
        "request.json",
        json.dumps({"required_sections": ["Results"]}),
    )
    deliverable2 = _write(tmp_path, "draft2.md", "## Results\n\nAll good.")
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable2),
        "--request", str(request_path), "--json",
    )
    assert code == 0
    fired = {
        issue["rule_id"] for issue in json.loads(out)["content"]["issues"]
    }
    assert "Q-required-section" not in fired


def test_quality_check_rejects_missing_and_bad_deliverable(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    code, _, err = run_cli(
        "quality-check", str(goal_path),
        "--deliverable", str(tmp_path / "missing.md"),
    )
    assert code == 1
    assert err.startswith("aperio: error:")

    binary = tmp_path / "binary.md"
    binary.write_bytes(b"\xff\xfe\x00bad")
    code, _, err = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(binary)
    )
    assert code == 1
    assert "UTF-8" in err


def test_quality_check_rejects_malformed_request_file(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(tmp_path, "draft.md", "Some text.")
    request = _write(tmp_path, "request.json", '{"min_words": -1}')
    code, _, err = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable),
        "--request", str(request),
    )
    assert code == 1
    assert err.startswith("aperio: error:")
    assert "min_words" in err


def test_quality_check_forbid_text_flag(run_cli, tmp_path: Path) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(
        tmp_path, "draft.md", "The draft mentions PRIVATE-KEY-123 once."
    )
    code, out, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable),
        "--forbid-text", "PRIVATE-KEY-123",
    )
    assert code == 0
    assert "privacy-or-safety-violation" in out


def test_quality_check_does_not_write_anything_without_output(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    deliverable = _write(tmp_path, "draft.md", "# Sleep debt\n\nText.")
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _, _ = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable)
    )
    assert code == 0
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


# ---------------------------------------------------------------------------
# six-domain CLI demonstration (acceptance: one real error per domain)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal_data,deliverable_text,expected_rule",
    [
        (
            {  # general: required outcome text missing
                "objective": "Compare two scheduling approaches for a small team",
                "intended_outcome": "A comparison the team can act on",
                "requirements": ["The outcome must include a clear recommendation"],
            },
            "Both approaches have merits and this note lists some thoughts "
            "on calendars and meeting blocks for the team to consider.",
            "Q-constraint-required-inclusion",
        ),
        (
            {  # design: required logo never included
                "objective": "Design a spring campaign poster for the "
                "neighbourhood garden open day",
                "intended_outcome": "A printable A3 poster",
                "profile": "design",
                "constraints": ["Must include the garden logo"],
            },
            "# Poster\n\nA calm green layout with large friendly type.",
            "Q-constraint-required-inclusion",
        ),
        (
            {  # research: evidence required, no attribution
                "objective": "Summarize the evidence on urban heat islands",
                "intended_outcome": "A cited evidence summary",
                "profile": "research",
                "evidence_requirements": ["Cite every factual claim"],
            },
            "# Heat islands\n\nStudies show cities are always warmer and "
            "the effect is proven largest at night.",
            "Q-missing-attribution",
        ),
        (
            {  # writing: below the stated minimum length
                "objective": "Write a plain-language explainer about sleep debt",
                "intended_outcome": "A titled article",
                "profile": "writing",
                "output_contract": {"length": "at least 400 words"},
            },
            "# Sleep debt\n\nToo little sleep adds up over days.",
            "Q-min-length",
        ),
        (
            {  # business: explicit prohibition violated
                "objective": "Assess whether the bakery should open a "
                "second location",
                "intended_outcome": "A recommendation with assumptions",
                "profile": "business",
                "constraints": ["The analysis must not assume unlimited funding"],
            },
            "# Assessment\n\nRevenue projections look strong and unlimited "
            "funding is available if needed.",
            "Q-constraint-prohibition",
        ),
        (
            {  # software: acceptance criteria never verified
                "objective": "Add an export button to the billing dashboard",
                "intended_outcome": "A merged change with verification",
                "profile": "software",
                "acceptance_criteria": ["CSV output matches the table contents"],
            },
            "# Export feature\n\nAdded an export button that serializes "
            "the visible rows.",
            "Q-missing-verification-evidence",
        ),
    ],
)
def test_six_domains_real_errors_via_cli(
    run_cli, tmp_path: Path, goal_data, deliverable_text, expected_rule
) -> None:
    goal_path = _goal_file(tmp_path, goal_data)
    deliverable = _write(tmp_path, "deliverable.md", deliverable_text)
    code, out, err = run_cli(
        "quality-check", str(goal_path), "--deliverable", str(deliverable)
    )
    assert code == 0
    assert expected_rule in out


# ---------------------------------------------------------------------------
# module entry point
# ---------------------------------------------------------------------------


def test_python_dash_m_entry_serves_quality_commands(
    run_cli, tmp_path: Path
) -> None:
    goal_path = _goal_file(tmp_path, GOOD_GOAL)
    code, out, err = run_cli("preflight", str(goal_path), "--json")
    assert code == 0
    assert json.loads(out)["content"]["kind"] == "preflight"
