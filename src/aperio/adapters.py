"""Agent adapter contract and built-in adapters (Features 04 and 07).

An adapter connects Aperio's task graph to an execution-capable agent.
Adapters never decide policy: authorization (``--authorize-exec``) is
enforced by the runtime (:mod:`aperio.runtime`), which is the only caller
of :meth:`AgentAdapter.start`. Adapters expose:

- ``id`` / ``display_name`` — stable identifiers
- ``available(config=None)`` — executable/install preflight; config
  validation and execution still happen separately
- ``preview(...)`` — deterministic, side-effect-free execution preview
- ``start(...)`` — perform the execution (called only in execute mode)

Built-in adapters:

- ``manual`` — for agents that cannot be invoked by Aperio. ``start``
  prepares a portable, sanitized run-handoff document at
  ``.aperio/runs/<run-id>.handoff.md`` and the run stays
  ``awaiting-external`` until ``aperio run-report`` records the external
  result. It never claims work was executed.
- ``local-process`` — runs an explicitly configured executable as a
  subprocess: argv array (no shell), working directory constrained to the
  project, minimal environment by default, bounded output capture with a
  full mirror under ``.aperio/local/runs/`` (machine-local, git-ignored),
  hard timeout with SIGTERM→grace→SIGKILL (Windows: TerminateProcess, no
  grace), stdin DEVNULL, always reaped. No output text ever enters a
  shareable record.
- ``codex-cli`` / ``claude-cli`` — provider-neutral non-interactive AI CLI
  adapters. The prompt travels on stdin, argv contains only static provider
  flags, provider-specific unsafe modes are refused, and execution shares
  the same authorization, timeout, capture, and privacy boundary. Exit 0
  is never verification or completion.

Honest boundary: ``--authorize-exec`` is a procedural human-in-the-loop
checkpoint, not a sandbox. A tool with shell access can bypass aperio
entirely; the gate exists so agent-configured execution always requires
an explicit human decision. Subprocesses are treated as potentially
powerful and NOT sandboxed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from aperio.artifacts import (
    check_artifact_dir,
    create_exclusive_text,
    discard_created_file,
    open_exclusive_binary,
)
from aperio.errors import StateError, ValidationError

MANUAL_ADAPTER_ID = "manual"
LOCAL_PROCESS_ADAPTER_ID = "local-process"
CODEX_CLI_ADAPTER_ID = "codex-cli"
CLAUDE_CLI_ADAPTER_ID = "claude-cli"

CLI_DEFAULT_TIMEOUT_SECONDS = 1800

RUNS_DIR_NAME = "runs"
LOCAL_RUNS_DIR_NAME = "local/runs"
RUN_HANDOFF_MARKER = "<!-- aperio:run-handoff:v1 -->"

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 3600
MAX_CAPTURE_BYTES = 64 * 1024  # per stream, retained tail


def runs_dir(aperio_dir: Path) -> Path:
    """Directory holding per-run portable artifacts (shareable)."""
    return aperio_dir / RUNS_DIR_NAME


def validate_adapter_config(config: object) -> dict:
    """Validate a local-process adapter config (strict, known keys only).

    The config file is machine-specific by design and must live under
    ``.aperio/local/`` (or any path the operator treats as machine-local);
    it is never persisted into ``executions.json`` or any shareable
    artifact — only the executable basename and argv are recorded there.
    """
    if not isinstance(config, dict):
        raise ValidationError(
            "local-process config must be a JSON object with 'executable' "
            "(string), optional 'args' (array of strings), 'working_dir' "
            "(string), 'timeout_seconds' (1-3600), 'env_policy' "
            "('minimal'|'inherit')"
        )
    allowed = {
        "executable",
        "args",
        "working_dir",
        "timeout_seconds",
        "env_policy",
    }
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValidationError(
            "local-process config has unknown field(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )
    missing = [key for key in ("executable",) if key not in config]
    if missing:
        raise ValidationError(
            f"local-process config is missing required field(s): "
            f"{', '.join(missing)}"
        )

    validated: dict = {}
    executable = config["executable"]
    if not isinstance(executable, str) or not executable.strip():
        raise ValidationError(
            "local-process config: 'executable' must be a non-empty string"
        )
    for separator in ("\r", "\n", "\x00"):
        if separator in executable:
            raise ValidationError(
                "local-process config: 'executable' must not contain "
                "line breaks or NUL characters"
            )
    validated["executable"] = executable.strip()

    args = config.get("args", [])
    if not isinstance(args, list) or any(
        not isinstance(arg, str) for arg in args
    ):
        raise ValidationError(
            "local-process config: 'args' must be an array of strings"
        )
    for index, arg in enumerate(args):
        if "\x00" in arg:
            raise ValidationError(
                f"local-process config: 'args'[{index}] contains a NUL "
                "character"
            )
    validated["args"] = list(args)

    working_dir = config.get("working_dir")
    if working_dir is not None:
        if not isinstance(working_dir, str) or not working_dir.strip():
            raise ValidationError(
                "local-process config: 'working_dir' must be a non-empty "
                "string when given"
            )
        if "\x00" in working_dir:
            raise ValidationError(
                "local-process config: 'working_dir' must not contain NUL"
            )
        validated["working_dir"] = working_dir

    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValidationError(
            "local-process config: 'timeout_seconds' must be an integer"
        )
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValidationError(
            f"local-process config: 'timeout_seconds' must be between 1 "
            f"and {MAX_TIMEOUT_SECONDS}"
        )
    validated["timeout_seconds"] = timeout

    env_policy = config.get("env_policy", "minimal")
    if env_policy not in ("minimal", "inherit"):
        raise ValidationError(
            "local-process config: 'env_policy' must be 'minimal' or "
            "'inherit'"
        )
    validated["env_policy"] = env_policy
    return validated


def _minimal_environment() -> dict:
    """A small, deliberately non-secret environment for child processes."""
    minimal = {}
    for key in ("PATH", "HOME", "LANG", "TMPDIR", "SystemRoot", "COMSPEC"):
        value = os.environ.get(key)
        if value is not None:
            minimal[key] = value
    return minimal


def build_environment(config: dict) -> dict:
    """Environment for the child process per the configured policy.

    The environment itself is NEVER captured, logged, or persisted —
    ``inherit`` is a user-acknowledged risk (child output could echo
    env-derived values into the machine-local log), while ``minimal``
    exposes only the standard lookup variables.
    """
    if config["env_policy"] == "inherit":
        return dict(os.environ)
    return _minimal_environment()


class AgentAdapter:
    """Contract for execution-capable agents (plain class, no magic).

    Subclasses implement ``preview`` and ``start``; authorization is
    enforced by the runtime, which calls ``start`` only in execute mode.
    """

    id: str = "abstract"
    display_name: str = "abstract adapter"

    def available(self, config: dict | None = None) -> tuple[bool, str]:
        """Environment-level capability check.

        ``config`` is optional so preflight may check an explicitly
        configured executable without validating or starting it.
        """
        return True, ""

    def _log_path(self, aperio_dir: Path, run_id: str) -> Path:
        log_dir = check_artifact_dir(aperio_dir, "local", "runs")
        return log_dir / f"{run_id}.log"

    def validate_config(self, config: dict | None, aperio_dir: Path) -> dict | None:
        """Validate adapter config BEFORE any persistent mutation.

        Runs before locks, records, and claims: an invalid config must
        never leave an orphaned task claim or a partial record.
        """
        raise NotImplementedError

    def preview(self, *, task: dict, config: dict | None, aperio_dir: Path) -> dict:
        """Deterministic, side-effect-free execution preview."""
        raise NotImplementedError

    def start(
        self,
        *,
        run_id: str,
        aperio_dir: Path,
        task: dict,
        config: dict | None,
        on_running=None,
    ) -> dict:
        """Perform the execution. Called by the runtime ONLY in execute
        mode (the runtime refuses to reach this without authorization).

        ``on_running(pid)`` is invoked by executing adapters immediately
        after the child process is created, so the runtime can record
        ``running`` + pid atomically; if it raises (e.g. the run was
        cancelled during startup), the adapter must terminate and reap
        the child, then re-raise.

        :returns: a result dict with at least ``outcome`` and
            ``exit_code`` (int or None). Raw output and untrusted external
            text belong in machine-local files, never in the returned
            shareable fields.
        """
        raise NotImplementedError


class ManualHandoffAdapter(AgentAdapter):
    """Adapter for external agents aperio cannot invoke.

    ``start`` prepares a portable run-handoff document and returns
    immediately: the run is recorded as awaiting external action and
    nothing is ever claimed as executed by this adapter.
    """

    id = MANUAL_ADAPTER_ID
    display_name = "manual handoff (external agent)"

    def available(self, config: dict | None = None) -> tuple[bool, str]:
        return True, ""

    def validate_config(self, config: dict | None, aperio_dir: Path) -> None:
        if config is not None:
            raise ValidationError(
                "manual adapter takes no config file; use the plan's task "
                "content as the handoff context"
            )

    def preview(self, *, task: dict, config: dict | None, aperio_dir: Path) -> dict:
        return {
            "adapter_id": self.id,
            "mode": "manual handoff preparation",
            "task_id": task["id"],
            "steps": [
                "aperio writes a run-handoff document for the external agent",
                "the external agent performs the work outside aperio",
                "aperio run-report records the outcome (never automatic)",
            ],
        }

    def start(
        self,
        *,
        run_id: str,
        aperio_dir: Path,
        task: dict,
        config: dict | None,
        on_running=None,
    ) -> dict:
        handoff_dir = check_artifact_dir(aperio_dir, RUNS_DIR_NAME)
        handoff_path = handoff_dir / f"{run_id}.handoff.md"
        document = render_run_handoff(run_id=run_id, task=task)
        # Exclusive creation through the shared path policy: the whole
        # parent chain is validated (no symlinked runs directory) and a
        # file that raced in is never truncated.
        create_exclusive_text(
            handoff_path, document, what="run handoff document"
        )
        return {
            "outcome": "manual handoff prepared; awaiting external agent",
            "exit_code": None,
            "handoff_ref": f"{RUNS_DIR_NAME}/{run_id}.handoff.md",
        }


def render_run_handoff(*, run_id: str, task: dict) -> str:
    """Render the portable run-handoff document (injection-hardened).

    All aperio-supplied structure sits at column 0; author-controlled
    content (task fields) is rendered as continuation-indented lines, so
    it can never forge column-0 headings. Field values were already
    line-separator-validated by the plan model.
    """
    lines = [
        "# Run Handoff",
        RUN_HANDOFF_MARKER,
        "",
        f"- **Run:** {run_id}",
        f"- **Task:** {task['id']}",
        "",
        "## Task title",
    ]
    for line in task["title"].split("\n"):
        lines.append(f"  {line}")
    lines.append("")
    lines.append("## Objective")
    for line in task["objective"].split("\n"):
        lines.append(f"  {line}")
    lines.append("")
    lines.append("## Expected deliverables")
    for item in task["deliverables"]:
        for line in item.split("\n"):
            lines.append(f"  - {line}")
    lines.append("")
    lines.append("## Acceptance criteria (verify these; aperio does not")
    lines.append("## consider the task complete until recorded manually)")
    for item in task["acceptance_criteria"]:
        for line in item.split("\n"):
            lines.append(f"  - {line}")
    lines.append("")
    lines.append("## Instructions for the external agent")
    lines.append("")
    lines.append(
        "Perform the work described above, then record the outcome with\n"
        "'aperio run-report' (or ask the human operator to). Aperio never\n"
        "marks this task complete on its own."
    )
    return "\n".join(lines) + "\n"


def _resolve_project_working_dir(
    raw: str | None, aperio_dir: Path, label: str
) -> Path:
    working_dir = Path(raw) if raw is not None else aperio_dir
    if not working_dir.exists():
        raise ValidationError(
            f"{label} config: working_dir {working_dir} does not exist "
            "(aperio never creates it)"
        )
    if not working_dir.is_dir():
        raise ValidationError(
            f"{label} config: working_dir {working_dir} is not a directory"
        )
    resolved = working_dir.resolve()
    project_root = aperio_dir.parent.resolve()
    if project_root not in resolved.parents and resolved != project_root:
        raise ValidationError(
            f"{label} config: working_dir {working_dir} resolves outside "
            f"the project boundary ({project_root})"
        )
    return resolved


def _stop_process_tree(process: subprocess.Popen, *, force: bool) -> None:
    """Stop the child and, on POSIX, its own process group.

    Every executing adapter starts in a new session, so its process group
    id equals the child pid. We never signal a group we did not create.
    """
    if os.name == "posix":
        # This helper is only used for our own child, which was started in
        # a new session: its pid is the process-group id.
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


def _run_captured_process(
    *,
    argv: list[str],
    working_dir: Path,
    env: dict,
    timeout_seconds: int,
    local_log: Path,
    label: str,
    stdin_text: str | None = None,
    on_running=None,
) -> dict:
    """Run one bounded child with a machine-local mirror and no shell.

    Shared by local-process and the documented CLI provider adapters. The
    prompt (when supplied) travels over stdin, never argv, and is not
    written to the log. Raw output remains under ``.aperio/local/``.
    """
    mirror_handle = open_exclusive_binary(local_log, what="local output log")
    stdout_tail, stderr_tail = {"text": "", "bytes": 0}, {
        "text": "",
        "bytes": 0,
    }
    truncated = {"stdout": 0, "stderr": 0}

    def drain(stream, key):
        tail = stdout_tail if key == "stdout" else stderr_tail
        for chunk in iter(lambda: stream.read(4096), b""):
            truncated[key] += len(chunk)
            if mirror_handle is not None:
                try:
                    mirror_handle.write(chunk)
                except OSError:
                    pass  # mirroring is best-effort
            tail["bytes"] += len(chunk)
            if tail["bytes"] <= MAX_CAPTURE_BYTES:
                tail["text"] += chunk.decode("utf-8", errors="replace")
        stream.close()

    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(working_dir),
            env=env,
            stdin=(
                subprocess.PIPE
                if stdin_text is not None
                else subprocess.DEVNULL
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        discard_created_file(local_log, mirror_handle)
        raise ValidationError(
            f"{label}: executable not found: {argv[0]}"
        ) from None
    except PermissionError as exc:
        discard_created_file(local_log, mirror_handle)
        raise ValidationError(
            f"{label}: cannot execute {argv[0]}: "
            f"{exc.strerror or exc}"
        ) from None
    except ValueError as exc:
        discard_created_file(local_log, mirror_handle)
        raise ValidationError(
            f"{label}: invalid argv: {exc}"
        ) from None
    except OSError as exc:
        discard_created_file(local_log, mirror_handle)
        raise StateError(
            f"{label}: the configured executable could not be started "
            f"({exc.strerror or type(exc).__name__}); no process was "
            "created. Check the adapter config and retry (no configuration "
            "values were recorded)."
        ) from None

    pid = process.pid
    if on_running is not None:
        try:
            on_running(pid)
        except BaseException:
            _stop_process_tree(process, force=False)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process_tree(process, force=True)
                process.wait()
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            raise

    writer = None
    if stdin_text is not None:
        payload = stdin_text.encode("utf-8")

        def feed() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=feed)
        writer.start()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "stdout")),
        threading.Thread(target=drain, args=(process.stderr, "stderr")),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_tree(process, force=False)
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _stop_process_tree(process, force=True)
            exit_code = process.wait()
        else:
            # The leader exited, but descendants in its process group may
            # still hold the output pipes. Reap the whole group before
            # joining the reader threads so a timeout can never hang here.
            _stop_process_tree(process, force=True)
    for thread in threads:
        thread.join()
    if writer is not None:
        writer.join(timeout=5)
    if mirror_handle is not None:
        try:
            mirror_handle.close()
        except OSError:
            pass

    outcome = (
        "adapter timed out and the process was terminated"
        if timed_out
        else f"adapter finished (exit {exit_code})"
    )
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_truncated": {
            "stdout": stdout_tail["bytes"] > MAX_CAPTURE_BYTES,
            "stderr": stderr_tail["bytes"] > MAX_CAPTURE_BYTES,
        },
        "output_bytes": {
            "stdout": stdout_tail["bytes"],
            "stderr": stderr_tail["bytes"],
        },
        "local_log_ref": f"{LOCAL_RUNS_DIR_NAME}/{local_log.name}",
        "pid": pid,
    }


def _validate_provider_base(
    config: object, *, label: str, allowed: set[str]
) -> dict:
    if not isinstance(config, dict):
        raise ValidationError(
            f"{label} config must be a JSON object with 'executable' "
            "(string), optional 'args', 'model', 'working_dir', "
            "'timeout_seconds', 'env_policy', and provider-specific keys"
        )
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValidationError(
            f"{label} config has unknown field(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    executable = config.get("executable", "")
    if not isinstance(executable, str):
        raise ValidationError(
            f"{label} config: 'executable' must be a non-empty string"
        )
    if executable and not executable.strip():
        raise ValidationError(
            f"{label} config: 'executable' must be a non-empty string"
        )
    for separator in ("\r", "\n", "\x00"):
        if separator in executable:
            raise ValidationError(
                f"{label} config: 'executable' must not contain line breaks "
                "or NUL characters"
            )
    if executable and (os.sep in executable or "/" in executable):
        if not Path(executable).is_absolute():
            raise ValidationError(
                f"{label} config: 'executable' must be an absolute path when "
                "it contains a directory separator (or use a bare command "
                "name resolved through PATH)"
            )

    args = config.get("args", [])
    if not isinstance(args, list) or any(
        not isinstance(arg, str) for arg in args
    ):
        raise ValidationError(
            f"{label} config: 'args' must be an array of strings"
        )
    if len(args) > 64:
        raise ValidationError(
            f"{label} config: 'args' may contain at most 64 items"
        )
    for index, arg in enumerate(args):
        if "\x00" in arg:
            raise ValidationError(
                f"{label} config: 'args'[{index}] contains a NUL character"
            )

    working_dir = config.get("working_dir")
    if working_dir is not None:
        if not isinstance(working_dir, str) or not working_dir.strip():
            raise ValidationError(
                f"{label} config: 'working_dir' must be a non-empty string "
                "when given"
            )
        if "\x00" in working_dir:
            raise ValidationError(
                f"{label} config: 'working_dir' must not contain NUL"
            )

    timeout = config.get("timeout_seconds", CLI_DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValidationError(
            f"{label} config: 'timeout_seconds' must be an integer"
        )
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValidationError(
            f"{label} config: 'timeout_seconds' must be between 1 and "
            f"{MAX_TIMEOUT_SECONDS}"
        )

    env_policy = config.get("env_policy", "minimal")
    if env_policy not in ("minimal", "inherit"):
        raise ValidationError(
            f"{label} config: 'env_policy' must be 'minimal' or 'inherit'"
        )

    model = config.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValidationError(
                f"{label} config: 'model' must be a non-empty string when "
                "given"
            )
        if any(char in model for char in ("\r", "\n", "\x00")):
            raise ValidationError(
                f"{label} config: 'model' must not contain line breaks or NUL"
            )

    return {
        "executable": executable.strip() if executable else "",
        "args": list(args),
        "working_dir": working_dir,
        "timeout_seconds": timeout,
        "env_policy": env_policy,
        "model": model.strip() if isinstance(model, str) else None,
    }


def _config_bool(config: dict, key: str, default: bool, label: str) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(
            f"{label} config: '{key}' must be true or false"
        )
    return value


def render_agent_prompt(*, run_id: str, task: dict, working_dir: Path) -> str:
    """Render one task prompt delivered over stdin (never argv)."""
    def block(value: str) -> list[str]:
        return [f"  {line}" for line in str(value).split("\n")]

    lines = [
        "# Aperio Task Execution",
        "",
        f"Run: {run_id}",
        f"Working directory: {working_dir}",
        "",
        "You are the implementation agent for exactly one approved Aperio",
        "task. Work only inside the working directory above.",
        "",
        "## Task",
        f"- ID: {task['id']}",
        "- Title:",
        *block(task["title"]),
        "- Objective:",
        *block(task["objective"]),
        "",
        "## Deliverables",
    ]
    for item in task["deliverables"]:
        lines.extend([f"-", *block(item)])
    lines.extend(["", "## Acceptance criteria"])
    for item in task["acceptance_criteria"]:
        lines.extend([f"-", *block(item)])
    lines.extend([
        "",
        "## Operating rules",
        "- Do not modify .aperio/ control files, policies, reviews, or",
        "  execution records.",
        "- Do not run 'aperio task-status ... --status completed',",
        "  'aperio review-*', or 'aperio policy-*'; Aperio retains",
        "  independent verification and completion authority.",
        "- Do not run interactive commands or wait for human input.",
        "- Use tests and commands appropriate to this repository.",
        "- When finished, summarize changed files and verification results",
        "  in your final response.",
        "",
    ])
    return "\n".join(lines)


class CliAgentAdapter(AgentAdapter):
    """Provider-neutral base for documented non-interactive coding CLIs.

    Prompt text is delivered on stdin; argv contains only static provider
    flags and operator-supplied prefix arguments. The process is bounded,
    observed, and mirrored exactly like local-process. Authorization is
    enforced by the runtime before ``start`` is reachable.
    """

    requires_authorization = True
    default_executable = ""

    def available(self, config: dict | None = None) -> tuple[bool, str]:
        executable = self.default_executable
        if isinstance(config, dict):
            configured = config.get("executable")
            if isinstance(configured, str) and configured.strip():
                executable = configured.strip()
        found = shutil.which(executable)
        if found is None and Path(executable).is_file():
            found = executable
        if found is None:
            return False, (
                f"executable {executable!r} was not found; install the "
                "CLI or set 'executable' in the adapter config"
            )
        return True, ""

    def validate_config(self, config: dict | None, aperio_dir: Path) -> dict:
        validated = self._validate_provider_config(config)
        _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        return validated

    def _validate_provider_config(self, config: dict | None) -> dict:
        raise NotImplementedError

    def _provider_args(
        self, validated: dict, working_dir: Path
    ) -> list[str]:
        raise NotImplementedError

    def _argv(self, validated: dict, working_dir: Path) -> list[str]:
        return [
            validated["executable"],
            *validated["args"],
            *self._provider_args(validated, working_dir),
        ]

    def command_preview(
        self, config: dict | None, aperio_dir: Path, *, task: dict | None = None
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        command = {
            "executable": validated["executable"],
            "args": self._argv(validated, working_dir)[1:],
            "working_dir": str(working_dir),
            "timeout_seconds": validated["timeout_seconds"],
            "env_policy": validated["env_policy"],
            "prompt_transport": "stdin",
        }
        if task is not None:
            prompt = render_agent_prompt(
                run_id="RUN-NNNN", task=task, working_dir=working_dir
            )
            encoded = prompt.encode("utf-8")
            command["prompt_bytes"] = len(encoded)
            command["prompt_sha256"] = hashlib.sha256(encoded).hexdigest()
        return command

    def preview(self, *, task: dict, config: dict | None, aperio_dir: Path) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        prompt = render_agent_prompt(
            run_id="RUN-NNNN", task=task, working_dir=working_dir
        )
        command = {
            "executable": validated["executable"],
            "args": self._argv(validated, working_dir)[1:],
            "working_dir": str(working_dir),
            "timeout_seconds": validated["timeout_seconds"],
            "env_policy": validated["env_policy"],
            "prompt_transport": "stdin",
            "prompt_bytes": len(prompt.encode("utf-8")),
            "prompt_sha256": hashlib.sha256(
                prompt.encode("utf-8")
            ).hexdigest(),
        }
        return {
            "adapter_id": self.id,
            "mode": "non-interactive CLI execution (requires --authorize-exec)",
            "task_id": task["id"],
            **command,
            "command": command,
            "risk_notes": [
                "launches a real installed AI coding CLI with the operator's "
                "privileges; this is not a sandbox",
                "the prompt is sent over stdin and is not written to the "
                "shareable execution record",
                "exit 0 only means the provider process finished; Aperio "
                "verification and independent review are still required",
            ],
        }

    def start(
        self,
        *,
        run_id: str,
        aperio_dir: Path,
        task: dict,
        config: dict | None,
        on_running=None,
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        prompt = render_agent_prompt(
            run_id=run_id, task=task, working_dir=working_dir
        )
        argv = self._argv(validated, working_dir)
        local_log = self._log_path(aperio_dir, run_id)
        return _run_captured_process(
            argv=argv,
            working_dir=working_dir,
            env=build_environment(validated),
            timeout_seconds=validated["timeout_seconds"],
            local_log=local_log,
            label=self.id,
            stdin_text=prompt,
            on_running=on_running,
        )


class CodexCliAdapter(CliAgentAdapter):
    """OpenAI Codex CLI via the documented non-interactive ``codex exec``."""

    id = CODEX_CLI_ADAPTER_ID
    display_name = "OpenAI Codex CLI (non-interactive exec)"
    default_executable = "codex"

    def _validate_provider_config(self, config: dict | None) -> dict:
        allowed = {
            "executable", "args", "model", "working_dir",
            "timeout_seconds", "env_policy", "sandbox",
            "skip_git_repo_check", "ephemeral",
        }
        validated = _validate_provider_base(
            config, label=self.id, allowed=allowed
        )
        if validated["executable"] == "":
            validated["executable"] = self.default_executable
        sandbox = config.get("sandbox", "workspace-write")
        if sandbox not in ("read-only", "workspace-write"):
            raise ValidationError(
                "codex-cli config: 'sandbox' must be 'read-only' or "
                "'workspace-write' (danger-full-access is deliberately not "
                "offered)"
            )
        validated["sandbox"] = sandbox
        validated["skip_git_repo_check"] = _config_bool(
            config, "skip_git_repo_check", True, self.id
        )
        validated["ephemeral"] = _config_bool(
            config, "ephemeral", True, self.id
        )
        return validated

    def _provider_args(self, validated: dict, working_dir: Path) -> list[str]:
        args = [
            "exec",
            "--sandbox", validated["sandbox"],
            "-C", str(working_dir),
            "-c", "approval_policy='never'",
        ]
        if validated["skip_git_repo_check"]:
            args.append("--skip-git-repo-check")
        if validated["ephemeral"]:
            args.append("--ephemeral")
        if validated["model"]:
            args.extend(["-m", validated["model"]])
        args.append("-")  # prompt is read from stdin
        return args


class ClaudeCliAdapter(CliAgentAdapter):
    """Anthropic Claude Code via documented non-interactive print mode."""

    id = CLAUDE_CLI_ADAPTER_ID
    display_name = "Anthropic Claude Code CLI (print mode)"
    default_executable = "claude"

    def _validate_provider_config(self, config: dict | None) -> dict:
        allowed = {
            "executable", "args", "model", "working_dir",
            "timeout_seconds", "env_policy", "permission_mode",
            "allowed_tools", "max_turns", "max_budget_usd",
            "no_session_persistence",
        }
        validated = _validate_provider_base(
            config, label=self.id, allowed=allowed
        )
        if validated["executable"] == "":
            validated["executable"] = self.default_executable
        mode = config.get("permission_mode", "acceptEdits")
        modes = {
            "default", "acceptEdits", "plan", "dontAsk", "auto", "manual"
        }
        if mode not in modes:
            raise ValidationError(
                "claude-cli config: 'permission_mode' must be one of "
                f"{', '.join(sorted(modes))}; bypassPermissions is "
                "deliberately not offered"
            )
        validated["permission_mode"] = mode
        tools = config.get(
            "allowed_tools", ["Read", "Edit", "Write", "Bash"]
        )
        if not isinstance(tools, list) or not tools:
            raise ValidationError(
                "claude-cli config: 'allowed_tools' must be a non-empty "
                "array of strings"
            )
        if len(tools) > 32:
            raise ValidationError(
                "claude-cli config: 'allowed_tools' may contain at most "
                "32 items"
            )
        for index, tool in enumerate(tools):
            if not isinstance(tool, str) or not tool.strip():
                raise ValidationError(
                    "claude-cli config: 'allowed_tools' items must be "
                    "non-empty strings"
                )
            if any(char in tool for char in ("\r", "\n", "\x00", ",")):
                raise ValidationError(
                    "claude-cli config: 'allowed_tools' items must not "
                    f"contain line breaks, NUL, or commas (item {index})"
                )
            if tool.strip().startswith("-"):
                raise ValidationError(
                    "claude-cli config: 'allowed_tools' items must be tool "
                    "names, not flags"
                )
        validated["allowed_tools"] = [tool.strip() for tool in tools]
        turns = config.get("max_turns", 25)
        if isinstance(turns, bool) or not isinstance(turns, int):
            raise ValidationError(
                "claude-cli config: 'max_turns' must be an integer"
            )
        if not 1 <= turns <= 100:
            raise ValidationError(
                "claude-cli config: 'max_turns' must be between 1 and 100"
            )
        validated["max_turns"] = turns
        budget = config.get("max_budget_usd")
        if budget is not None:
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not 0 < budget <= 1000
            ):
                raise ValidationError(
                    "claude-cli config: 'max_budget_usd' must be a number "
                    "greater than 0 and at most 1000"
                )
            budget = float(budget)
        validated["max_budget_usd"] = budget
        validated["no_session_persistence"] = _config_bool(
            config, "no_session_persistence", True, self.id
        )
        return validated

    def _provider_args(self, validated: dict, working_dir: Path) -> list[str]:
        args = [
            "-p",
            "--permission-mode", validated["permission_mode"],
            "--permission-prompts", "none",
            "--allowedTools", ",".join(validated["allowed_tools"]),
            "--max-turns", str(validated["max_turns"]),
        ]
        if validated["no_session_persistence"]:
            args.append("--no-session-persistence")
        if validated["max_budget_usd"] is not None:
            args.extend([
                "--max-budget-usd", f"{validated['max_budget_usd']:.2f}"
            ])
        if validated["model"]:
            args.extend(["--model", validated["model"]])
        return args


class LocalProcessAdapter(AgentAdapter):
    """Runs an explicitly configured executable as a local subprocess.

    Strict controls (all enforced here or by the runtime): argv array
    with no shell interpolation, working directory inside the project,
    bounded output capture with a full machine-local mirror, hard timeout
    with SIGTERM→grace→SIGKILL (Windows: TerminateProcess, no grace),
    stdin DEVNULL, child always reaped, no privilege escalation, no
    network or credential assumptions, and no environment capture.
    """

    id = LOCAL_PROCESS_ADAPTER_ID
    display_name = "configured local process (explicit authorization required)"
    requires_authorization = True

    def available(self, config: dict | None = None) -> tuple[bool, str]:
        return True, ""

    def validate_config(self, config: dict | None, aperio_dir: Path) -> dict:
        """Validate config (and the working-directory boundary) BEFORE
        any persistent mutation."""
        if config is None:
            raise ValidationError(
                "local-process adapter requires a config file "
                "(run-start --config); store it under .aperio/local/ — "
                "it is machine-specific and never persisted into "
                "shareable records"
            )
        validated = validate_adapter_config(config)
        self._resolve_working_dir(validated, aperio_dir)
        return validated

    def _resolve_working_dir(self, config: dict, aperio_dir: Path) -> Path:
        raw = config.get("working_dir")
        working_dir = (
            Path(raw) if raw is not None else aperio_dir
        )
        if not working_dir.exists():
            raise ValidationError(
                f"local-process config: working_dir {working_dir} does "
                "not exist (aperio never creates it)"
            )
        if not working_dir.is_dir():
            raise ValidationError(
                f"local-process config: working_dir {working_dir} is not "
                "a directory"
            )
        resolved = working_dir.resolve()
        project_root = aperio_dir.parent.resolve()
        if project_root not in resolved.parents and resolved != project_root:
            raise ValidationError(
                f"local-process config: working_dir {working_dir} resolves "
                f"outside the project boundary ({project_root})"
            )
        return resolved

    def preview(self, *, task: dict, config: dict | None, aperio_dir: Path) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = self._resolve_working_dir(validated, aperio_dir)
        return {
            "adapter_id": self.id,
            "mode": "subprocess execution (requires --authorize-exec)",
            "task_id": task["id"],
            "executable": validated["executable"],
            "args": validated["args"],
            "working_dir": str(working_dir),
            "timeout_seconds": validated["timeout_seconds"],
            "env_policy": validated["env_policy"],
            "risk_notes": [
                "runs a real local process with the configured privileges",
                "output is captured machine-locally only; the shareable "
                "record carries neutral metadata (exit code, bytes, "
                "truncation flags), never output text",
                "exit 0 means the adapter finished - task completion still "
                "requires Feature-03 verification with evidence",
            ],
        }

    def command_preview(
        self, config: dict | None, aperio_dir: Path, *, task: dict | None = None
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = self._resolve_working_dir(validated, aperio_dir)
        return {
            "executable": validated["executable"],
            "args": validated["args"],
            "working_dir": str(working_dir),
            "timeout_seconds": validated["timeout_seconds"],
            "env_policy": validated["env_policy"],
        }

    def start(
        self,
        *,
        run_id: str,
        aperio_dir: Path,
        task: dict,
        config: dict | None,
        on_running=None,
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = self._resolve_working_dir(validated, aperio_dir)
        local_log = self._log_path(aperio_dir, run_id)
        return _run_captured_process(
            argv=[validated["executable"], *validated["args"]],
            working_dir=working_dir,
            env=build_environment(validated),
            timeout_seconds=validated["timeout_seconds"],
            local_log=local_log,
            label="local-process",
            stdin_text=None,
            on_running=on_running,
        )


def builtin_adapters() -> list[AgentAdapter]:
    """All built-in adapters in stable display order."""
    return [
        ManualHandoffAdapter(),
        LocalProcessAdapter(),
        CodexCliAdapter(),
        ClaudeCliAdapter(),
    ]


def get_adapter(adapter_id: str) -> AgentAdapter:
    """Look up a built-in adapter by id."""
    for adapter in builtin_adapters():
        if adapter.id == adapter_id:
            return adapter
    raise ValidationError(
        f"unknown agent adapter {adapter_id!r} "
        f"(available: {', '.join(a.id for a in builtin_adapters())})"
    )
