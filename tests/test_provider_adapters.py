"""Feature 07: provider-neutral non-interactive AI CLI adapters.

Uses executable fake CLI scripts so the tests exercise the real process
boundary (argv, stdin, cwd, bounded log, structured output, exit status)
without network or provider credentials. Capability probes read only
``--version``/``--help``; no model request is made.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aperio.adapters import (
    CLAUDE_CLI_ADAPTER_ID,
    CODEX_CLI_ADAPTER_ID,
    ClaudeCliAdapter,
    CodexCliAdapter,
    get_adapter,
)
from aperio.errors import StateError, ValidationError
from aperio.planstore import approve_plan, create_plan, read_plan
from aperio.runtime import dispatch_task, read_executions, runs_dispatch

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

PRELUDE = '''#!/usr/bin/env python3
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
'''


def _project(run_cli, project_dir):
    assert run_cli("init", str(project_dir))[0] == 0
    aperio_dir = project_dir / ".aperio"
    create_plan(aperio_dir, PLAN_INPUT)
    approve_plan(aperio_dir)
    return aperio_dir


def _custom_cli(tmp_path, name, body, *, help_text=HELP_TEXT) -> Path:
    path = tmp_path / name
    prelude = PRELUDE.replace("HELP_TEXT", repr(help_text))
    path.write_text(prelude + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_cli(tmp_path, name="fake-provider", exit_code=0) -> Path:
    body = f'''
payload = {{
    "argv": args,
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "secret_visible": "APERIO_TEST_SECRET" in os.environ,
}}
Path(__file__).with_suffix(".capture.json").write_text(
    json.dumps(payload), encoding="utf-8"
)
if "exec" in args:
    print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}))
    print(json.dumps({{"type": "item.completed", "item": {{
        "id": "i1", "type": "agent_message", "text": "CODEX-MODEL-SECRET"
    }}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{
        "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2
    }}}}))
else:
    print(json.dumps({{
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0.01,
        "usage": {{"input_tokens": 10, "output_tokens": 5}},
        "permission_denials": [],
        "result": "CLAUDE-MODEL-SECRET",
    }}))
print("fake-provider-stderr", file=sys.stderr)
raise SystemExit({exit_code})
'''
    return _custom_cli(tmp_path, name, body)


def _capture(fake: Path) -> dict:
    return json.loads(fake.with_suffix(".capture.json").read_text())


def _config(fake, project_dir, **overrides):
    config = {
        "executable": str(fake),
        "working_dir": str(project_dir),
        "timeout_seconds": 20,
        "env_policy": "minimal",
    }
    config.update(overrides)
    return config


def test_codex_default_is_read_only_and_parses_structured_events(
    run_cli, project_dir, tmp_path, monkeypatch
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    monkeypatch.setenv("APERIO_TEST_SECRET", "do-not-leak")
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir, model="gpt-test"),
        authorize_exec=True,
    )
    assert record["status"] == "completed"
    capture = _capture(fake)
    assert capture["argv"][:2] == ["exec", "--json"]
    assert capture["argv"][capture["argv"].index("--sandbox") + 1] == "read-only"
    assert "-C" in capture["argv"]
    assert str(project_dir) in capture["argv"]
    assert "approval_policy='never'" in capture["argv"]
    assert "--skip-git-repo-check" in capture["argv"]
    assert "--ephemeral" in capture["argv"]
    assert capture["argv"][-3:] == ["-m", "gpt-test", "-"]
    assert "BUILD-SECRET-OBJECTIVE" not in capture["argv"]
    assert "BUILD-SECRET-OBJECTIVE" in capture["stdin"]
    assert capture["cwd"] == str(project_dir)
    assert capture["secret_visible"] is False
    assert record["provider_result"]["parse_status"] == "ok"
    assert record["provider_result"]["model_reported"] == "success"
    assert record["provider_result"]["usage"] == {
        "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2
    }
    log = (aperio_dir / "local/runs/RUN-0001.log").read_text()
    assert "fake-provider-stderr" in log
    task = read_plan(aperio_dir)["tasks"][0]
    assert task["status"] == "in-progress"  # exit 0 is not verification
    serialized = (aperio_dir / "executions.json").read_text()
    assert str(fake) not in serialized
    assert "BUILD-SECRET-OBJECTIVE" not in serialized
    assert "CODEX-MODEL-SECRET" not in serialized


def test_codex_workspace_write_requires_separate_authorization(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    config = _config(fake, project_dir, sandbox="workspace-write")
    with pytest.raises(StateError, match="authorize-write"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
            config=config, authorize_exec=True,
        )
    assert not fake.with_suffix(".capture.json").exists()
    assert read_executions(aperio_dir) is None
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"

    with pytest.raises(StateError, match="authorize-write"):
        runs_dispatch(
            aperio_dir, adapter_id=CODEX_CLI_ADAPTER_ID,
            config=config, authorize_exec=True,
        )
    assert not fake.with_suffix(".capture.json").exists()

    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=config, authorize_exec=True, authorize_workspace_write=True,
    )
    assert record["status"] == "completed"
    argv = _capture(fake)["argv"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_claude_read_only_uses_plan_mode_and_restrictive_tools(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path, "fake-claude")
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(
            fake, project_dir, model="claude-test", max_turns=7,
            max_budget_usd=2.5,
        ),
        authorize_exec=True,
    )
    assert record["status"] == "completed"
    argv = _capture(fake)["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert "Bash" not in argv
    assert "Edit" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert argv[argv.index("--model") + 1] == "claude-test"
    assert "BUILD-SECRET-OBJECTIVE" not in argv
    assert "BUILD-SECRET-OBJECTIVE" in _capture(fake)["stdin"]
    assert record["provider_result"]["model_reported"] == "success"
    serialized = (aperio_dir / "executions.json").read_text()
    assert "CLAUDE-MODEL-SECRET" not in serialized


def test_claude_write_requires_authorization_and_shell_is_opt_in(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path, "fake-claude")
    shell_config = _config(fake, project_dir, allow_shell=True)
    with pytest.raises(StateError, match="authorize-write"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
            config=shell_config, authorize_exec=True,
        )
    assert not fake.with_suffix(".capture.json").exists()
    assert read_executions(aperio_dir) is None

    # Write consent alone enables Edit/Write, never broad Bash.
    write_record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
        authorize_workspace_write=True,
    )
    assert write_record["status"] == "completed"
    argv = _capture(fake)["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write"
    assert argv[argv.index("--allowedTools") + 1] == (
        "Read,Grep,Glob,Edit,Write"
    )
    assert "Bash" not in argv


@pytest.mark.parametrize(
    ("adapter_id", "override"),
    [
        (CODEX_CLI_ADAPTER_ID, {"args": [
            "--sandbox", "danger-full-access"
        ]}),
        (CODEX_CLI_ADAPTER_ID, {"args": [
            "--sandbox=danger-full-access"
        ]}),
        (CODEX_CLI_ADAPTER_ID, {"args": [
            "--config", "sandbox_mode=\"danger-full-access\""
        ]}),
        (CLAUDE_CLI_ADAPTER_ID, {"args": [
            "--permission-mode", "bypassPermissions"
        ]}),
        (CLAUDE_CLI_ADAPTER_ID, {"args": [
            "--permission-mode=bypassPermissions"
        ]}),
        (CLAUDE_CLI_ADAPTER_ID, {"args": ["--dangerously-skip-permissions"]}),
        (CLAUDE_CLI_ADAPTER_ID, {"args": ["--tools=Bash,Edit,Write"]}),
        (CLAUDE_CLI_ADAPTER_ID, {"args": ["--allowedTools=Bash"]}),
    ],
)
def test_raw_provider_argument_overrides_never_reach_the_executable(
    run_cli, project_dir, tmp_path, adapter_id, override
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    with pytest.raises(ValidationError, match="not accepted"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=adapter_id,
            config=_config(fake, project_dir, **override),
            authorize_exec=True,
        )
    assert not fake.with_suffix(".capture.json").exists()
    assert read_executions(aperio_dir) is None
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"


def test_direct_permission_and_sandbox_overrides_are_rejected(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    codex = CodexCliAdapter()
    claude = ClaudeCliAdapter()
    with pytest.raises(ValidationError, match="danger-full-access"):
        codex.validate_config(
            {"executable": "codex", "sandbox": "danger-full-access"},
            aperio_dir,
        )
    with pytest.raises(ValidationError, match="bypassPermissions"):
        claude.validate_config(
            {"executable": "claude", "permission_mode": "bypassPermissions"},
            aperio_dir,
        )
    with pytest.raises(ValidationError, match="allowed_tools"):
        claude.validate_config(
            {"executable": "claude", "allowed_tools": ["Bash"]}, aperio_dir
        )
    with pytest.raises(ValidationError, match="unknown field"):
        codex.validate_config(
            {"executable": "codex", "raw_flags": ["--yolo"]}, aperio_dir
        )
    with pytest.raises(ValidationError, match="not accepted"):
        codex.validate_config(
            {"executable": "codex", "args": ["--version"]}, aperio_dir
        )
    with pytest.raises(ValidationError, match="working_dir"):
        codex.validate_config(
            {"executable": "codex", "working_dir": str(project_dir.parent)},
            aperio_dir,
        )
    with pytest.raises(ValidationError, match="timeout_seconds"):
        codex.validate_config(
            {"executable": "codex", "timeout_seconds": True}, aperio_dir
        )
    assert codex.validate_config({}, aperio_dir)["executable"] == "codex"
    assert codex.validate_config({}, aperio_dir)["sandbox"] == "read-only"
    assert claude.validate_config({}, aperio_dir)["executable"] == "claude"


def test_capability_probe_refuses_unsupported_cli_before_mutation(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    limited_help = HELP_TEXT.replace("  --json\n", "")
    fake = _custom_cli(
        tmp_path, "limited-codex",
        '\nprint("should-not-run")\n',
        help_text=limited_help,
    )
    with pytest.raises(StateError, match="does not advertise required flag"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
            config=_config(fake, project_dir), authorize_exec=True,
        )
    assert read_executions(aperio_dir) is None
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"
    assert not fake.with_suffix(".capture.json").exists()


def test_agent_doctor_reports_capabilities_without_model_request(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    fake = _fake_cli(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config(fake, project_dir)))
    code, out, err = run_cli(
        "agent-doctor", str(project_dir), "--agent", CODEX_CLI_ADAPTER_ID,
        "--config", str(config_path), "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    assert report["installed"] is True
    assert report["version"] == "fake-provider 1.2.3"
    assert report["configuration"]["valid"] is True
    assert report["missing_flags"] == []
    assert report["read_only_execution"]["supported"] is True
    assert report["workspace_write_execution"]["supported"] is True
    assert report["authentication"]["checked"] is False
    assert report["live_integration"]["status"] == "unverified"
    assert not fake.with_suffix(".capture.json").exists()


def test_agent_doctor_reports_missing_and_invalid_config(
    run_cli, project_dir, tmp_path
):
    _project(run_cli, project_dir)
    missing = tmp_path / "missing-provider"
    missing_config = tmp_path / "missing.json"
    missing_config.write_text(json.dumps(_config(missing, project_dir)))
    code, out, err = run_cli(
        "agent-doctor", str(project_dir), "--agent", CLAUDE_CLI_ADAPTER_ID,
        "--config", str(missing_config), "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    assert report["installed"] is False
    assert report["configuration"]["valid"] is True
    assert "was not found" in report["installation_error"]

    fake = _fake_cli(tmp_path)
    unsafe_config = tmp_path / "unsafe.json"
    unsafe_config.write_text(json.dumps(_config(
        fake, project_dir,
        args=["--permission-mode", "bypassPermissions"],
    )))
    code, out, err = run_cli(
        "agent-doctor", str(project_dir), "--agent", CLAUDE_CLI_ADAPTER_ID,
        "--config", str(unsafe_config), "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    assert report["configuration"]["valid"] is False
    assert "not accepted" in report["configuration"]["error"]


def test_unauthorized_provider_is_read_only_preview(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    preview = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=False,
    )
    assert preview["status"] == "preview"
    assert preview["command"]["prompt_transport"] == "stdin"
    assert preview["command"]["prompt_sha256"]
    assert preview["command"]["executable"] == str(fake)
    assert preview["command"]["effective_permissions"]["workspace_write"] is False
    assert not fake.with_suffix(".capture.json").exists()
    assert read_executions(aperio_dir) is None
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["status"] == "completed"


def test_preview_displays_write_request_and_effective_read_only(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    task = read_plan(aperio_dir)["tasks"][0]
    adapter = get_adapter(CODEX_CLI_ADAPTER_ID)
    preview = adapter.preview(
        task=task,
        config=_config(fake, project_dir, sandbox="workspace-write"),
        aperio_dir=aperio_dir,
    )
    assert preview["command"]["requested_workspace_write"] is True
    assert preview["command"]["effective_permissions"]["workspace_write"] is False
    assert any("not authorized" in note for note in preview["risk_notes"])
    assert not fake.with_suffix(".capture.json").exists()

    authorized = adapter.preview(
        task=task,
        config=_config(fake, project_dir, sandbox="workspace-write"),
        aperio_dir=aperio_dir,
        authorize_workspace_write=True,
    )
    assert authorized["command"]["effective_permissions"]["workspace_write"] is True
    assert authorized["command"]["args"][
        authorized["command"]["args"].index("--sandbox") + 1
    ] == "workspace-write"


def test_claude_preview_shows_restrictive_tool_contract(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    preview = get_adapter(CLAUDE_CLI_ADAPTER_ID).preview(
        task=read_plan(aperio_dir)["tasks"][0],
        config=_config(fake, project_dir),
        aperio_dir=aperio_dir,
    )
    permissions = preview["command"]["effective_permissions"]
    assert permissions["permission_mode"] == "plan"
    assert permissions["tools"] == ["Read", "Grep", "Glob"]
    assert permissions["shell"] is False
    assert "not an OS sandbox" in " ".join(preview["risk_notes"])
    assert not fake.with_suffix(".capture.json").exists()


def test_provider_nonzero_exit_fails_without_losing_claim(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path, "fake-fail", exit_code=7)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["status"] == "failed"
    assert record["exit_code"] == 7
    assert record["provider_result"]["model_reported"] == "success"
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "in-progress"


def test_provider_timeout_terminates_process(run_cli, project_dir, tmp_path):
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(
        tmp_path, "fake-slow",
        "\nimport time\ntime.sleep(30)\n",
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir, timeout_seconds=1),
        authorize_exec=True,
    )
    assert record["status"] == "failed"
    assert "timed out" in record["outcome"]


@pytest.mark.skipif(
    os.name != "posix",
    reason="Windows descendant cleanup is documented as best-effort",
)
def test_parent_exit_with_descendant_holding_pipes_is_bounded(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(
        tmp_path, "fake-descendant",
        '''
child = subprocess.Popen([
    sys.executable, "-c",
    "import time; time.sleep(60)",
], stdout=sys.stdout, stderr=sys.stderr)
Path(__file__).with_suffix(".childpid").write_text(str(child.pid))
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
time.sleep(0.2)
raise SystemExit(0)
''',
    )
    # The body uses subprocess/time without importing them in the prelude.
    text = fake.read_text().replace(
        "child = subprocess.Popen", "import subprocess\nimport time\nchild = subprocess.Popen"
    )
    fake.write_text(text)

    started = time.monotonic()
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir, timeout_seconds=30),
        authorize_exec=True,
    )
    elapsed = time.monotonic() - started
    assert record["status"] == "completed"
    assert record["exit_code"] == 0
    assert elapsed < 10, f"dispatch hung for {elapsed:.1f}s"
    child_pid = int(fake.with_suffix(".childpid").read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant {child_pid} survived cleanup")


def test_structured_incomplete_failed_malformed_and_oversized(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)

    incomplete = _custom_cli(
        tmp_path, "incomplete",
        '\nprint(json.dumps({"type": "thread.started", "thread_id": "t"}))\n',
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(incomplete, project_dir), authorize_exec=True,
    )
    assert record["provider_result"]["model_reported"] == "incomplete"

    failed = _custom_cli(
        tmp_path, "model-failed",
        '\nprint(json.dumps({"type": "turn.failed", "error": '
        '{"message": "MODEL-SECRET"}}))\n',
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(failed, project_dir), authorize_exec=True,
    )
    assert record["status"] == "completed"  # process exit is distinct
    assert record["provider_result"]["model_reported"] == "failed"
    serialized = (aperio_dir / "executions.json").read_text()
    assert "MODEL-SECRET" not in serialized

    malformed = _custom_cli(
        tmp_path, "malformed",
        '\nprint("not-json-MODEL-SECRET")\n',
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(malformed, project_dir), authorize_exec=True,
    )
    assert record["provider_result"]["parse_status"] == "malformed"
    assert record["provider_result"]["model_reported"] == "unknown"

    oversized = _custom_cli(
        tmp_path, "oversized",
        '\nprint(json.dumps({"type": "item.completed", "item": '
        '{"type": "agent_message", "text": "x" * 300000}}))\n',
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(oversized, project_dir), authorize_exec=True,
    )
    assert record["provider_result"]["parse_status"] == "oversized"
    assert record["provider_result"]["model_reported"] == "unknown"


def test_claude_malformed_structured_output_is_safe(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(
        tmp_path, "claude-malformed",
        '\nprint("CLAUDE-RAW-SECRET")\n',
    )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["provider_result"]["parse_status"] == "malformed"
    serialized = (aperio_dir / "executions.json").read_text()
    assert "CLAUDE-RAW-SECRET" not in serialized


def test_provider_governance_requires_declared_implementer(
    run_cli, project_dir, tmp_path
):
    assert run_cli("init", str(project_dir))[0] == 0
    assert run_cli(
        "policy-init", str(project_dir),
        "--assign", "p:planner", "--assign", "i:implementer",
        "--assign", "i2:implementer", "--assign", "r:reviewer",
    )[0] == 0
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN_INPUT))
    assert run_cli(
        "plan-create", str(project_dir), "--file", str(plan_file),
        "--actor", "p",
    )[0] == 0
    assert run_cli(
        "plan-approve", str(project_dir), "--actor", "r"
    )[0] == 0
    aperio_dir = project_dir / ".aperio"
    fake = _fake_cli(tmp_path)
    with pytest.raises(StateError, match="not permitted"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
            config=_config(fake, project_dir), authorize_exec=True,
            actor="r",
        )
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
        actor="i",
    )
    assert record["status"] == "completed"
    task = read_plan(aperio_dir)["tasks"][0]
    assert task["implementer"] == "i"
    assert task["owner"] == "codex-cli:RUN-0001"


def test_agent_list_exposes_provider_adapters(run_cli):
    code, out, err = run_cli("agent-list")
    assert code == 0, err
    assert "codex-cli" in out
    assert "claude-cli" in out
    assert "--authorize-write" in out
    assert get_adapter(CODEX_CLI_ADAPTER_ID).id == CODEX_CLI_ADAPTER_ID
    assert get_adapter(CLAUDE_CLI_ADAPTER_ID).id == CLAUDE_CLI_ADAPTER_ID


def test_scheduler_previews_then_dispatches_provider(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    preview = runs_dispatch(
        aperio_dir, adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=False,
    )
    assert len(preview) == 1 and preview[0]["status"] == "preview"
    assert not fake.with_suffix(".capture.json").exists()
    records = runs_dispatch(
        aperio_dir, adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert len(records) == 1 and records[0]["status"] == "completed"


def test_provider_execution_metadata_survives_portability(
    run_cli, project_dir, tmp_path
):
    from aperio.portable import export_project, import_archive

    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    assert dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )["status"] == "completed"
    archive = tmp_path / "provider.zip"
    export_project(str(project_dir), output=str(archive))
    destination = tmp_path / "imported"
    import_archive(str(destination), str(archive))
    run = read_executions(destination / ".aperio")["runs"][0]
    assert run["adapter_id"] == CODEX_CLI_ADAPTER_ID
    assert run["status"] == "completed"
    assert run["provider_result"]["parse_status"] == "ok"
    assert run["local_log_ref"] is None
    assert run["pid"] is None
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        archive_bytes = b"".join(
            bundle.read(name) for name in bundle.namelist()
        )
    assert b"# Aperio Task Execution" not in archive_bytes
    assert b"CODEX-MODEL-SECRET" not in archive_bytes
    assert str(fake).encode() not in archive_bytes
    assert b"provider_result" in archive_bytes


def test_missing_provider_executable_is_rejected_before_mutation(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    with pytest.raises(StateError, match="not available"):
        dispatch_task(
            aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
            config=_config(tmp_path / "missing", project_dir),
            authorize_exec=True,
        )
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"
    assert read_executions(aperio_dir) is None


def test_provider_cancellation_terminates_owned_process(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _custom_cli(
        tmp_path, "fake-cancel",
        "\nimport time\ntime.sleep(30)\n",
    )
    import threading
    from aperio.runtime import cancel_run

    outcome = {}

    def run():
        try:
            outcome["record"] = dispatch_task(
                aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
                config=_config(fake, project_dir, timeout_seconds=30),
                authorize_exec=True,
            )
        except BaseException as exc:  # pragma: no cover - test diagnostic
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    run_id = None
    while time.monotonic() < deadline:
        document = read_executions(aperio_dir)
        if document and document["runs"]:
            run = document["runs"][0]
            if run.get("status") == "running" and run.get("pid"):
                run_id = run["run_id"]
                break
        time.sleep(0.05)
    assert run_id is not None, "provider never reached running state"
    cancelled = cancel_run(aperio_dir, run_id, reason="test cancellation")
    thread.join(timeout=10)
    assert not thread.is_alive(), "dispatch did not return after cancellation"
    assert "error" not in outcome
    assert cancelled["status"] == "cancelled"
    final = read_executions(aperio_dir)["runs"][0]
    assert final["status"] == "cancelled"


def test_cli_write_consent_is_separate_and_actionable(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    config_path = tmp_path / "codex.json"
    config_path.write_text(json.dumps(
        _config(fake, project_dir, sandbox="workspace-write")
    ))
    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", CODEX_CLI_ADAPTER_ID, "--config", str(config_path),
        "--authorize-exec",
    )
    assert code == 1
    assert "authorize-write" in err
    assert "Traceback" not in err
    assert read_executions(aperio_dir) is None
    assert not fake.with_suffix(".capture.json").exists()

    code, out, err = run_cli(
        "run-start", str(project_dir), "TASK-001",
        "--agent", CODEX_CLI_ADAPTER_ID, "--config", str(config_path),
        "--authorize-exec", "--authorize-write",
    )
    assert code == 0, err
    assert "completed" in out
    assert _capture(fake)["argv"][
        _capture(fake)["argv"].index("--sandbox") + 1
    ] == "workspace-write"

@pytest.mark.skipif(
    os.name != "posix", reason="POSIX signal/process-group semantics"
)
def test_cli_interrupt_is_actionable_and_terminates_owned_child(
    run_cli, project_dir, tmp_path
):
    _project(run_cli, project_dir)
    fake = _custom_cli(
        tmp_path,
        "sleepy-provider",
        """
import time
Path(__file__).with_suffix(".started").write_text("started")
time.sleep(60)
""",
    )
    config_path = tmp_path / "sleepy.json"
    config_path.write_text(json.dumps(_config(fake, project_dir)))
    cli = subprocess.Popen(
        [
            sys.executable, "-m", "aperio", "run-start",
            str(project_dir), "TASK-001",
            "--agent", CODEX_CLI_ADAPTER_ID,
            "--config", str(config_path), "--authorize-exec",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = fake.with_suffix(".started")
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists(), "provider child never started"
    os.kill(cli.pid, signal.SIGINT)
    out, err = cli.communicate(timeout=10)
    assert cli.returncode == 130
    assert "interrupted" in err
    assert "Traceback" not in err
    code, out, err = run_cli(
        "run-status", str(project_dir), "RUN-0001", "--json"
    )
    assert code == 0, err
    assert json.loads(out)["status"] == "interrupted"
