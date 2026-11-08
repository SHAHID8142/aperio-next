"""Feature 07: provider-neutral non-interactive AI CLI adapters.

Uses executable fake CLI scripts so the tests exercise the real process
boundary (argv, stdin, cwd, bounded log, exit status) without network or
provider credentials.
"""

from __future__ import annotations

import json
import os
import stat
import sys
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

FAKE_CLI = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

payload = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "secret_visible": "APERIO_TEST_SECRET" in os.environ,
}
Path(__file__).with_suffix(".capture.json").write_text(
    json.dumps(payload), encoding="utf-8"
)
print("fake-provider-stdout")
print("fake-provider-stderr", file=sys.stderr)
sys.exit(EXIT_CODE)
'''


def _project(run_cli, project_dir):
    assert run_cli("init", str(project_dir))[0] == 0
    aperio_dir = project_dir / ".aperio"
    create_plan(aperio_dir, PLAN_INPUT)
    approve_plan(aperio_dir)
    return aperio_dir


def _fake_cli(tmp_path, name="fake-provider", exit_code=0) -> Path:
    path = tmp_path / name
    path.write_text(
        FAKE_CLI.replace("EXIT_CODE", str(exit_code)), encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


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


def test_codex_cli_invocation_uses_documented_exec_and_stdin(
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
    assert record["adapter_id"] == CODEX_CLI_ADAPTER_ID
    capture = _capture(fake)
    assert capture["argv"][:1] == ["exec"]
    assert "--sandbox" in capture["argv"]
    assert "workspace-write" in capture["argv"]
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
    log = (aperio_dir / "local/runs/RUN-0001.log").read_text()
    assert "fake-provider-stdout" in log
    assert "fake-provider-stderr" in log
    task = read_plan(aperio_dir)["tasks"][0]
    assert task["status"] == "in-progress"  # exit 0 is not verification
    serialized = (aperio_dir / "executions.json").read_text()
    assert str(fake) not in serialized
    assert "BUILD-SECRET-OBJECTIVE" not in serialized


def test_claude_cli_invocation_uses_print_mode_and_bounded_flags(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path, "fake-claude")
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CLAUDE_CLI_ADAPTER_ID,
        config=_config(
            fake, project_dir, model="claude-test", max_turns=7,
            allowed_tools=["Read", "Edit", "Bash"],
            max_budget_usd=2.5,
        ),
        authorize_exec=True,
    )
    assert record["status"] == "completed"
    argv = _capture(fake)["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit,Bash"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert argv[argv.index("--model") + 1] == "claude-test"
    assert "BUILD-SECRET-OBJECTIVE" not in argv
    assert "BUILD-SECRET-OBJECTIVE" in _capture(fake)["stdin"]


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
    assert not fake.with_suffix(".capture.json").exists()
    assert read_executions(aperio_dir) is None
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "pending"
    # The same project can still be executed after explicit authorization.
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir), authorize_exec=True,
    )
    assert record["status"] == "completed"


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
    assert read_plan(aperio_dir)["tasks"][0]["status"] == "in-progress"


def test_provider_timeout_terminates_process(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = tmp_path / "fake-slow"
    fake.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id=CODEX_CLI_ADAPTER_ID,
        config=_config(fake, project_dir, timeout_seconds=1),
        authorize_exec=True,
    )
    assert record["status"] == "failed"
    assert record.get("timed_out") is True or "timed out" in record["outcome"]


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


def test_provider_config_validation_is_strict(project_dir, run_cli):
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
            {"executable": "claude", "allowed_tools": []}, aperio_dir
        )
    with pytest.raises(ValidationError, match="not flags"):
        claude.validate_config(
            {"executable": "claude", "allowed_tools": ["--yolo"]}, aperio_dir
        )
    with pytest.raises(ValidationError, match="working_dir"):
        codex.validate_config(
            {"executable": "codex", "working_dir": str(project_dir.parent)},
            aperio_dir,
        )
    with pytest.raises(ValidationError, match="unknown field"):
        codex.validate_config(
            {"executable": "codex", "raw_flags": ["--yolo"]}, aperio_dir
        )
    with pytest.raises(ValidationError, match="timeout_seconds"):
        codex.validate_config(
            {"executable": "codex", "timeout_seconds": True}, aperio_dir
        )
    # A missing executable selects the documented default binary.
    assert codex.validate_config({}, aperio_dir)["executable"] == "codex"
    assert claude.validate_config({}, aperio_dir)["executable"] == "claude"
    prefix = codex.validate_config(
        {"executable": "codex", "args": ["--version"]}, aperio_dir
    )
    assert prefix["args"] == ["--version"]


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
    assert get_adapter(CODEX_CLI_ADAPTER_ID).id == CODEX_CLI_ADAPTER_ID
    assert get_adapter(CLAUDE_CLI_ADAPTER_ID).id == CLAUDE_CLI_ADAPTER_ID


def test_provider_preview_does_not_spawn_or_need_authorization(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    preview = get_adapter(CLAUDE_CLI_ADAPTER_ID).preview(
        task=read_plan(aperio_dir)["tasks"][0],
        config=_config(fake, project_dir),
        aperio_dir=aperio_dir,
    )
    assert preview["command"]["prompt_sha256"]
    assert preview["command"]["prompt_bytes"] > 0
    assert not fake.with_suffix(".capture.json").exists()
    assert "not a sandbox" in " ".join(preview["risk_notes"])


def test_scheduler_previews_then_dispatches_provider(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _project(run_cli, project_dir)
    fake = _fake_cli(tmp_path)
    from aperio.runtime import runs_dispatch

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
    assert run["local_log_ref"] is None
    assert run["pid"] is None
