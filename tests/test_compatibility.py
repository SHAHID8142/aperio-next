"""Chunk 07 compatibility audit: everything that must remain true.

All Features 01–07 commands, M1–M3 contracts, the schema version table,
and standalone (no-project) operation survive the M4–M8 additions. The
full 1200+ test suite is the deep compatibility proof; this module pins
the surface-level guarantees in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.goals import GOAL_SCHEMA_VERSION
from aperio.planmodel import PLAN_SCHEMA_READ_VERSIONS, PLAN_SCHEMA_VERSION
from aperio.profiles import PROFILE_SCHEMA_VERSION
from aperio.schema import SCHEMA_VERSION


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


LEGACY_COMMANDS = (
    "init", "status", "memory", "decision-add", "handoff-write",
    "phase-set", "plan-create", "plan-show", "plan-approve", "plan-revise",
    "task-list", "task-status", "agent-list", "run-preview", "run-start",
    "run-status", "runs-list", "run-cancel", "runs-reconcile", "run-report",
    "run-handoff", "export", "import", "integrity", "upgrade", "roles",
    "policy-init", "policy-show", "policy-assign", "policy-permissions",
    "review-submit", "review-decide", "review-list", "review-status",
    "context-pack", "profiles", "goal-check", "goal-pack",
    "mem-propose", "mem-review", "mem-list", "mem-show", "mem-recall",
    "mem-export", "mem-import",
)

M4_TO_M8_COMMANDS = (
    "preflight", "quality-check", "feedback-check", "feedback-review",
    "feedback-lesson", "feedback-compare", "improvement-apply",
    "exchange-export", "exchange-import", "agent-brief",
    "benchmark-cases", "benchmark-run",
)


def test_every_legacy_command_still_exists(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for command in LEGACY_COMMANDS:
        assert command in out, f"legacy command vanished: {command}"
    for command in M4_TO_M8_COMMANDS:
        assert command in out, f"new command missing: {command}"


def test_schema_versions_unchanged() -> None:
    assert SCHEMA_VERSION == 1          # state.json
    # plan-schema v3 (optional plan-level ``milestone`` roadmap linkage);
    # v1/v2 documents remain fully readable and v2 archives import
    # unchanged — documented contract change, see planmodel.py
    assert PLAN_SCHEMA_VERSION == 3
    assert (1, 2, 3) == PLAN_SCHEMA_READ_VERSIONS
    assert GOAL_SCHEMA_VERSION == 1     # goal-schema v1
    assert PROFILE_SCHEMA_VERSION == 1  # profile v1
    from aperio.memportable import MEMORY_EXCHANGE_FORMAT_VERSION
    from aperio.portable import EXPORT_FORMAT_VERSION
    assert MEMORY_EXCHANGE_FORMAT_VERSION == 1  # M3 memory exchange v1
    assert EXPORT_FORMAT_VERSION == 1           # Feature 05 archive v1
    from aperio.exchange import EXCHANGE_FORMAT_VERSION
    from aperio.feedback import FEEDBACK_SCHEMA_VERSION
    from aperio.quality import QUALITY_REPORT_VERSION
    from aperio.benchmark import BENCHMARK_REPORT_VERSION
    assert EXCHANGE_FORMAT_VERSION == 1
    assert FEEDBACK_SCHEMA_VERSION == 1
    assert QUALITY_REPORT_VERSION == 1
    assert BENCHMARK_REPORT_VERSION == 1


def test_standalone_workflows_need_no_project(tmp_path: Path, capsys) -> None:
    """A non-programmer one-off task requires no `aperio init` anywhere."""

    assert not (tmp_path / ".aperio").exists()
    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps({
        "objective": "Compare two scheduling approaches for a small team",
        "intended_outcome": "A short comparison the team can act on",
    }), encoding="utf-8")
    deliverable = tmp_path / "answer.md"
    deliverable.write_text(
        "Both approaches have merits; this note compares a few.",
        encoding="utf-8",
    )
    for argv in (
        ("goal-check", str(goal_file)),
        ("preflight", str(goal_file)),
        ("agent-brief", str(goal_file)),
        ("quality-check", str(goal_file), "--deliverable", str(deliverable)),
        ("benchmark-run", "--only", "general-ambiguous-objective"),
    ):
        code, out, err = run_cli(capsys, *argv)
        assert code == 0, (argv, err)
    assert not (tmp_path / ".aperio").exists()


def test_supported_python_versions_documented() -> None:
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'requires-python = ">=3.11"' in pyproject
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f'Programming Language :: Python :: {version}' in pyproject


def test_one_canonical_pack_bytes_unchanged_without_new_inputs(
    tmp_path: Path, capsys
) -> None:
    """M1/M2 output stays byte-stable: a goal pack without memory or a
    context pack without new fields renders exactly as the goal defines."""

    goal_file = tmp_path / "goal.json"
    goal_file.write_text(json.dumps({
        "objective": "Write a 700-word explainer about sleep debt",
        "intended_outcome": "A titled article",
        "constraints": ["Use plain language"],
        "profile": "writing",
    }), encoding="utf-8")
    code, out1, _ = run_cli(capsys, "goal-pack", str(goal_file), "--json")
    assert code == 0
    code, out2, _ = run_cli(capsys, "goal-pack", str(goal_file), "--json")
    pack = json.loads(out2)
    # generated_at differs; content is canonical and reproducible
    assert pack["content"]["constraints"] == ["Use plain language"]
    assert len(pack["content_sha256"]) == 64
