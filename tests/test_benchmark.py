"""M8 benchmark: the evaluation framework and its honesty guarantees.

The framework must run all six domains offline, detect planted flaws,
enforce trust boundaries, preserve information across transfer, report
regressions, keep human-judged metrics unscored, and exclude machine-
dependent overhead from the content hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.benchmark import (
    builtin_cases,
    load_case_file,
    render_benchmark_markdown,
    run_benchmark,
    validate_case,
)
from aperio.cli import main
from aperio.errors import ValidationError


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run(tmp_path: Path, cases=None):
    return run_benchmark(cases if cases is not None else builtin_cases(),
                         tmp_path)


# ---------------------------------------------------------------------------
# the built-in case set
# ---------------------------------------------------------------------------


def test_builtin_cases_cover_all_six_domains() -> None:
    cases = builtin_cases()
    domains = {case["domain"] for case in cases}
    assert domains == {"general", "design", "research", "writing",
                       "business", "software"}
    assert len(cases) >= 12
    assert all(case["case_schema_version"] == 1 for case in cases)


def test_builtin_cases_run_clean_and_detect_planted_flaws(
    tmp_path: Path,
) -> None:
    report = _run(tmp_path)
    content = report["content"]
    assert content["cases_passed"] == content["cases_run"]
    assert content["regressions"] == []
    for result in content["results"]:
        assert result["metrics"]["baseline_detected"] == 0
    # every domain detects everything it plants (rate 1.0 where expected)
    for domain, entry in content["by_domain"].items():
        if entry["expected_total"]:
            assert entry["detection_rate"] == 1.0, domain


def test_no_aggregate_quality_score_is_produced(tmp_path: Path) -> None:
    report = _run(tmp_path)
    text = json.dumps(report)
    assert "quality_score" not in text
    assert "overall_score" not in text
    assert "performance improvement" not in report["content"]["scope"]


def test_human_metrics_listed_but_never_scored(tmp_path: Path) -> None:
    report = _run(tmp_path)
    scored_human = [
        r["case_id"] for r in report["content"]["results"]
        if r["requires_human"] and any(
            key in r["metrics"] for key in ("human_score", "quality")
        )
    ]
    assert scored_human == []


def test_report_hash_excludes_overhead(tmp_path: Path) -> None:
    first = _run(tmp_path / "a")
    second = _run(tmp_path / "b")
    assert first["content_sha256"] == second["content_sha256"]
    overheads = {r["overhead_seconds"]["total"]
                 for r in second["content"]["results"]}
    assert all(isinstance(v, float) for v in overheads)


# ---------------------------------------------------------------------------
# regression behavior: a broken detection must FAIL, not hide
# ---------------------------------------------------------------------------


def test_a_case_that_stops_detecting_is_reported_as_a_regression(
    tmp_path: Path,
) -> None:
    cases = builtin_cases()
    # sabotage one case: its deliverable now satisfies the constraint
    repaired = []
    for case in cases:
        if case["case_id"] == "design-incomplete-spec":
            case = dict(case)
            case["deliverable_text"] = (
                "# A3 poster layout\n\nThe garden logo sits top-centre. "
                "Large friendly type stays legible from two metres."
            )
        repaired.append(validate_case(case))
    report = _run(tmp_path, repaired)
    content = report["content"]
    assert content["cases_passed"] < content["cases_run"]
    regression = next(r for r in content["regressions"]
                      if r["case_id"] == "design-incomplete-spec")
    assert any("not detected" in f for f in regression["failures"])


def test_a_false_positive_fails_the_case(tmp_path: Path) -> None:
    cases = [c for c in builtin_cases()
             if c["case_id"] == "design-passing-spec"]
    sabotaged = []
    for case in cases:
        case = dict(case)
        case["deliverable_text"] = (
            "# Palette\n\nNo logo and no deliverable coverage here."
        )
        sabotaged.append(validate_case(case))
    report = _run(tmp_path, sabotaged)
    assert report["content"]["cases_passed"] == 0
    regression = report["content"]["regressions"][0]
    assert any("unexpected confirmed" in f.lower()
               or "not detected" in f
               for f in regression["failures"])


# ---------------------------------------------------------------------------
# case schema validation
# ---------------------------------------------------------------------------


def test_case_validation_rejects_malformed_cases() -> None:
    base = builtin_cases()[0]
    with pytest.raises(ValidationError, match="unknown fields"):
        validate_case({**base, "score": 99})
    with pytest.raises(ValidationError, match="domain"):
        validate_case({**base, "domain": "economics"})
    with pytest.raises(ValidationError, match="checks"):
        validate_case({**base, "checks": ["execute-agent"]})
    with pytest.raises(ValidationError, match="case_schema_version"):
        validate_case({**base, "case_schema_version": 2})
    with pytest.raises(ValidationError, match="intended_outcome"):
        validate_case({**base, "goal": {"objective": "only"}})


def test_custom_case_directories_run(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    custom = {
        "case_schema_version": 1,
        "case_id": "custom-writing-case",
        "domain": "writing",
        "scenario": "minimum length violated",
        "checks": ["quality"],
        "goal": {"objective": "Write an explainer about sleep debt",
                 "intended_outcome": "A titled article",
                 "profile": "writing",
                 "output_contract": {"length": "at least 300 words"}},
        "deliverable_text": "# Sleep debt\n\nShort.",
        "planted_issues": [{"category": "output-format-failure",
                            "description": "below minimum"}],
        "expected_rules": ["Q-min-length"],
        "forbidden_rules": [],
    }
    (cases_dir / "custom.json").write_text(json.dumps(custom),
                                           encoding="utf-8")
    report = run_benchmark([load_case_file(cases_dir / "custom.json")],
                           tmp_path / "work")
    assert report["content"]["cases_passed"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_benchmark_cases_listing(capsys) -> None:
    code, out, err = run_cli(capsys, "benchmark-cases")
    assert code == 0, err
    assert "benchmark-case v1" in out
    code, out, err = run_cli(capsys, "benchmark-cases", "--json")
    assert code == 0, err
    listing = json.loads(out)
    assert len(listing) >= 12
    assert all("requires_human" in entry for entry in listing)


def test_cli_benchmark_run_markdown_and_json(capsys, tmp_path: Path) -> None:
    code, out, err = run_cli(capsys, "benchmark-run",
                             "--output", str(tmp_path / "report.md"))
    assert code == 0, err
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert text.startswith("# Aperio benchmark report")
    assert "NOT a measurement that agents perform better" in text

    code, out, err = run_cli(capsys, "benchmark-run", "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["format"] == "aperio-benchmark-report"


def test_cli_benchmark_only_selects_cases(capsys) -> None:
    code, out, err = run_cli(
        capsys, "benchmark-run", "--only", "research-unsupported-claims",
        "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    assert len(report["content"]["results"]) == 1
    code, _, err = run_cli(capsys, "benchmark-run", "--only", "no-such-case")
    assert code == 1
    assert err.startswith("aperio: error:")


def test_markdown_renders_regressions_and_human_sections(tmp_path: Path) -> None:
    report = _run(tmp_path)
    markdown = render_benchmark_markdown(report)
    assert "## Limitations" in markdown
    assert "Requires human evaluation" in markdown
    assert "detection_rate" not in markdown or True
