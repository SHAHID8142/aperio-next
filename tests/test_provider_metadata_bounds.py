"""Feature 07 gate closure: one shared provider-metadata contract.

Defect (documented in the roadmap since M1): the adapter accepted provider
metadata up to ``10**12`` while the runtime persistence validator rejected
values above ``1_000_000_000`` — a structurally accepted provider result
could fail (or poison) persistence and strand the run after the child
exited. The fix is one shared set of constants used by both sides; these
tests pin the boundary at the adapter parse, the runtime validator, and
the real dispatch path.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from aperio.adapters import (
    CODEX_CLI_ADAPTER_ID,
    _bounded_usage,
    _parse_claude_stdout,
    _parse_codex_stdout,
    MAX_PROVIDER_COUNT_VALUE,
    MAX_PROVIDER_COST_USD,
    MAX_PROVIDER_METADATA_VALUE,
)
from aperio import runtime
from aperio.errors import StateError
from aperio.planstore import approve_plan, create_plan
from aperio.runtime import dispatch_task, read_executions

PLAN_INPUT = {
    "objective": "BUILD-SECRET-OBJECTIVE",
    "expected_outcome": "working change",
    "scope": ["provider"], "exclusions": [], "assumptions": [],
    "risks": [], "strategy": "small",
    "acceptance_criteria": ["tests pass"],
    "verification_strategy": "pytest",
    "tasks": [{
        "id": "TASK-001",
        "title": "Implement provider bridge",
        "objective": "BUILD-SECRET-OBJECTIVE",
        "deliverables": ["src/change.py"],
        "acceptance_criteria": ["tests pass"],
        "depends_on": [],
    }],
}

HELP_TEXT = """fake provider
Usage: fake [OPTIONS]

Options:
  --json
  --sandbox <MODE>
  -C, --cd <DIR>
  -c, --config <KEY=VALUE>
  --skip-git-repo-check
  --ephemeral
  -m, --model <MODEL>
  -p, --print
  --permission-mode <MODE> (choices: plan, acceptEdits)
  --permission-prompts <POLICY>
  --tools <TOOLS>
  --allowedTools <TOOLS>
  --output-format <FORMAT>
  --max-turns <N>
  --no-session-persistence
  --max-budget-usd <N>
"""

PRELUDE = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("fake-provider 1.2.3")
    raise SystemExit(0)
if "--help" in args:
    print(HELP_TEXT)
    raise SystemExit(0)
"""


def _custom_cli(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    prelude = PRELUDE.replace("HELP_TEXT", repr(HELP_TEXT))
    path.write_text(prelude + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
               | stat.S_IXOTH)
    return path


def _project(run_cli, project_dir) -> Path:
    assert run_cli("init", str(project_dir))[0] == 0
    aperio_dir = project_dir / ".aperio"
    create_plan(aperio_dir, PLAN_INPUT)
    approve_plan(aperio_dir)
    return aperio_dir


def _config(fake: Path, project_dir: Path) -> dict:
    return {
        "executable": str(fake),
        "working_dir": str(project_dir),
        "timeout_seconds": 20,
        "env_policy": "minimal",
    }


# ---------------------------------------------------------------------------
# the contract itself
# ---------------------------------------------------------------------------


def test_one_shared_contract_on_both_sides() -> None:
    """The runtime must validate with the very constants the adapter uses."""

    assert runtime.MAX_PROVIDER_METADATA_VALUE is MAX_PROVIDER_METADATA_VALUE
    assert runtime.MAX_PROVIDER_COUNT_VALUE is MAX_PROVIDER_COUNT_VALUE
    assert runtime.MAX_PROVIDER_COST_USD is MAX_PROVIDER_COST_USD
    assert MAX_PROVIDER_METADATA_VALUE == 1_000_000_000


def test_bounded_usage_rejects_everything_the_contract_refuses() -> None:
    accepted = MAX_PROVIDER_METADATA_VALUE
    for value, expected in (
        (accepted, accepted),
        (accepted + 1, None),
        (10**12, None),
        (-1, None),
        (True, None),
        (1.5, None),
        ("100", None),
        (None, None),
    ):
        assert _bounded_usage(value) == expected, value


# ---------------------------------------------------------------------------
# adapter parse side
# ---------------------------------------------------------------------------


def _codex_record(usage: dict) -> dict:
    lines = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed",
         "item": {"id": "i", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": usage},
    ]
    return _parse_codex_stdout(
        "\n".join(json.dumps(line) for line in lines), truncated=False
    )


def test_codex_usage_above_the_shared_bound_is_dropped_not_stored() -> None:
    record = _codex_record({
        "input_tokens": MAX_PROVIDER_METADATA_VALUE + 1,
        "output_tokens": 5,
    })
    assert record["parse_status"] == "ok"
    assert record["usage"] in (None, {"output_tokens": 5}), \
        "an out-of-contract magnitude must never enter the record"

    boundary = _codex_record({"input_tokens": MAX_PROVIDER_METADATA_VALUE})
    assert boundary["usage"]["input_tokens"] == MAX_PROVIDER_METADATA_VALUE


def test_claude_turns_duration_and_cost_respect_the_shared_bound() -> None:
    event = {
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": MAX_PROVIDER_METADATA_VALUE + 1,
        "duration_ms": MAX_PROVIDER_METADATA_VALUE + 1,
        "total_cost_usd": float(MAX_PROVIDER_COST_USD) + 1.0,
        "usage": {"input_tokens": MAX_PROVIDER_METADATA_VALUE + 1},
        "permission_denials": [],
    }
    record = _parse_claude_stdout(json.dumps(event), truncated=False)
    assert record["parse_status"] == "ok"
    assert record["num_turns"] is None
    assert record["duration_ms"] is None
    assert record["total_cost_usd"] is None
    assert record["usage"] is None

    boundary_event = dict(event,
                          num_turns=MAX_PROVIDER_METADATA_VALUE,
                          duration_ms=MAX_PROVIDER_METADATA_VALUE,
                          total_cost_usd=float(MAX_PROVIDER_COST_USD),
                          usage={"input_tokens": MAX_PROVIDER_METADATA_VALUE})
    ok = _parse_claude_stdout(json.dumps(boundary_event), truncated=False)
    assert ok["num_turns"] == MAX_PROVIDER_METADATA_VALUE
    assert ok["duration_ms"] == MAX_PROVIDER_METADATA_VALUE
    assert ok["total_cost_usd"] == float(MAX_PROVIDER_COST_USD)
    assert ok["usage"]["input_tokens"] == MAX_PROVIDER_METADATA_VALUE


def test_non_finite_and_boolean_costs_are_dropped() -> None:
    for bad in (float("inf"), float("nan"), True):
        event = {"type": "result", "subtype": "success",
                 "total_cost_usd": bad}
        record = _parse_claude_stdout(json.dumps(event), truncated=False)
        assert record["total_cost_usd"] is None, bad


# ---------------------------------------------------------------------------
# runtime validator side
# ---------------------------------------------------------------------------


def _validated(provider_result: dict) -> None:
    runtime._validate_provider_result(provider_result,
                                      Path("executions.json"), 0)


def test_runtime_validator_boundary_matches_the_adapter() -> None:
    ok = {
        "provider": "claude-cli", "format": "json", "parse_status": "ok",
        "model_reported": "success",
        "event_count": MAX_PROVIDER_COUNT_VALUE,
        "error_events": MAX_PROVIDER_COUNT_VALUE,
        "unknown_events": MAX_PROVIDER_COUNT_VALUE,
        "num_turns": MAX_PROVIDER_METADATA_VALUE,
        "duration_ms": MAX_PROVIDER_METADATA_VALUE,
        "total_cost_usd": float(MAX_PROVIDER_COST_USD),
        "usage": {"input_tokens": MAX_PROVIDER_METADATA_VALUE},
    }
    _validated(ok)
    for key, bad in (
        ("num_turns", MAX_PROVIDER_METADATA_VALUE + 1),
        ("duration_ms", MAX_PROVIDER_METADATA_VALUE + 1),
        ("event_count", MAX_PROVIDER_COUNT_VALUE + 1),
        ("total_cost_usd", float(MAX_PROVIDER_COST_USD) + 1.0),
    ):
        tampered = dict(ok)
        tampered[key] = bad
        with pytest.raises(StateError):
            _validated(tampered)


def test_negative_malformed_and_boolean_values_are_rejected() -> None:
    base = {
        "provider": "codex-cli", "format": "jsonl", "parse_status": "ok",
        "model_reported": "success", "event_count": 1,
        "turn_completed": True, "turn_failed": False,
        "usage": {"input_tokens": -1},
    }
    with pytest.raises(StateError):
        _validated(base)
    boolean = dict(base, usage={"input_tokens": True})
    with pytest.raises(StateError):
        _validated(boolean)


# ---------------------------------------------------------------------------
# end to end: an extreme provider result must never strand a run
# ---------------------------------------------------------------------------

_HUGE_BODY = """
payload = {"argv": args, "cwd": os.getcwd(), "stdin": sys.stdin.read(),
           "secret_visible": "APERIO_TEST_SECRET" in os.environ}
Path(__file__).with_suffix(".capture.json").write_text(
    json.dumps(payload), encoding="utf-8")
if "exec" in args:
    print(json.dumps({"type": "thread.started", "thread_id": "t"}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 2000000000, "output_tokens": 5,
        "cached_input_tokens": 900000000000}}))
else:
    print(json.dumps({"type": "result", "subtype": "success",
                      "is_error": False, "num_turns": 9000000000000,
                      "duration_ms": 9000000000000, "total_cost_usd": 1e12,
                      "usage": {"input_tokens": 900000000000},
                      "permission_denials": [], "result": "fine"}))
raise SystemExit(0)
"""


def test_codex_result_beyond_the_bound_finalizes_and_reads_back(
    tmp_path, run_cli, project_dir
) -> None:
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(tmp_path, "huge-codex", _HUGE_BODY)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["status"] == "completed"
    assert record["provider_result"]["parse_status"] == "ok"
    # out-of-contract magnitudes are dropped; the in-bound value survives
    assert record["provider_result"]["usage"] == {"output_tokens": 5}

    # The executions file must remain readable — the property the pre-fix
    # mismatch broke: an adapter-accepted result can never fail persistence
    # on magnitude grounds.
    state = read_executions(aperio_dir)
    stored = state["runs"][0]["provider_result"]
    assert stored["usage"] == {"output_tokens": 5}
    assert stored["model_reported"] == "success"


def test_claude_result_beyond_the_bound_finalizes_and_reads_back(
    tmp_path, run_cli, project_dir
) -> None:
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(tmp_path, "huge-claude", _HUGE_BODY)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="claude-cli",
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["status"] == "completed"
    provider = record["provider_result"]
    assert provider["num_turns"] is None
    assert provider["duration_ms"] is None
    assert provider["total_cost_usd"] is None
    assert provider["usage"] is None

    state = read_executions(aperio_dir)
    stored = state["runs"][0]["provider_result"]
    assert stored["num_turns"] is None
    assert stored["model_reported"] == "success"
