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
import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from aperio.artifacts import (
    check_artifact_dir,
    create_exclusive_text,
    discard_created_file,
    open_exclusive_binary,
)
from aperio.errors import AperioError, StateError, ValidationError

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
MAX_CAPTURE_BYTES = 64 * 1024  # retained tail for ordinary adapters
MAX_PROVIDER_STDOUT_BYTES = 1024 * 1024  # bounded structured-output capture
MAX_PROVIDER_EVENT_BYTES = 256 * 1024  # one JSON/JSONL event
MAX_PROVIDER_EVENTS = 4096
MAX_PROVIDER_PROBE_BYTES = 512 * 1024
MAX_PROVIDER_USAGE_VALUE = 10**12
PIPE_JOIN_GRACE_SECONDS = 2.0  # bounded drain after the leader exits
PROBE_TIMEOUT_SECONDS = 15

CLAUDE_READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
CLAUDE_WRITE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")
CLAUDE_SHELL_TOOL = "Bash"


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

    def authorization_error(
        self, config: dict | None, *, authorize_workspace_write: bool
    ) -> str | None:
        """Return an actionable refusal when execution would exceed consent."""
        return None

    def check_capabilities(self, config: dict | None) -> None:
        """Refuse execution when the installed tool lacks required controls."""
        return None

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

    def preview(
        self, *, task: dict, config: dict | None, aperio_dir: Path,
        authorize_workspace_write: bool = False,
    ) -> dict:
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
        workspace_write_authorized: bool = False,
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


def _close_quietly(stream) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _join_pipe_threads(
    threads: list[threading.Thread],
    *,
    process: subprocess.Popen,
    writer: threading.Thread | None,
) -> None:
    """Join capture threads with a hard bound, then clean the owned group.

    A provider can exit while a descendant still holds the inherited
    stdout/stderr pipes. An unbounded ``join`` would then hang forever.
    The child was started in its own POSIX session, so killing its group
    cannot signal unrelated processes. Closing the local pipe handles is
    the final unblock (also the best available Windows bound: descendants
    are not killed there; see the documented limitation).
    """
    deadline = time.monotonic() + PIPE_JOIN_GRACE_SECONDS
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        _stop_process_tree(process, force=True)
        deadline = time.monotonic() + PIPE_JOIN_GRACE_SECONDS
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        _close_quietly(process.stdout)
        _close_quietly(process.stderr)
        for thread in threads:
            thread.join(timeout=1.0)
    if writer is not None:
        writer.join(timeout=1.0)
        if writer.is_alive():
            _close_quietly(process.stdin)
            writer.join(timeout=1.0)


def _run_captured_process(
    *,
    argv: list[str],
    working_dir: Path,
    env: dict,
    timeout_seconds: int,
    local_log: Path | None,
    label: str,
    stdin_text: str | None = None,
    capture_output_limit: int | None = None,
    on_running=None,
) -> dict:
    """Run one bounded child with an optional machine-local mirror.

    Shared by local-process, the documented CLI provider adapters, and
    passive capability probes. The prompt (when supplied) travels over
    stdin, never argv, and is not written to the log. Raw output remains
    in memory only when a caller explicitly requests a bounded capture;
    shareable records never receive it.
    """
    mirror_handle = (
        open_exclusive_binary(local_log, what="local output log")
        if local_log is not None
        else None
    )
    counts = {"stdout": 0, "stderr": 0}
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(stream, key):
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                counts[key] += len(chunk)
                if mirror_handle is not None:
                    try:
                        mirror_handle.write(chunk)
                    except (OSError, ValueError):
                        pass  # mirroring is best-effort
                if capture_output_limit is not None:
                    buffer = captured[key]
                    room = capture_output_limit - len(buffer)
                    if room > 0:
                        buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated[key] = True
        finally:
            _close_quietly(stream)

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
        if local_log is not None and mirror_handle is not None:
            discard_created_file(local_log, mirror_handle)
        elif mirror_handle is not None:
            _close_quietly(mirror_handle)
        raise ValidationError(
            f"{label}: executable not found: {argv[0]}"
        ) from None
    except PermissionError as exc:
        if local_log is not None and mirror_handle is not None:
            discard_created_file(local_log, mirror_handle)
        elif mirror_handle is not None:
            _close_quietly(mirror_handle)
        raise ValidationError(
            f"{label}: cannot execute {argv[0]}: "
            f"{exc.strerror or exc}"
        ) from None
    except ValueError as exc:
        if local_log is not None and mirror_handle is not None:
            discard_created_file(local_log, mirror_handle)
        elif mirror_handle is not None:
            _close_quietly(mirror_handle)
        raise ValidationError(
            f"{label}: invalid argv: {exc}"
        ) from None
    except OSError as exc:
        if local_log is not None and mirror_handle is not None:
            discard_created_file(local_log, mirror_handle)
        elif mirror_handle is not None:
            _close_quietly(mirror_handle)
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
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            _close_quietly(process.stdin)
            _close_quietly(process.stdout)
            _close_quietly(process.stderr)
            if mirror_handle is not None:
                _close_quietly(mirror_handle)
            raise

    writer = None
    if stdin_text is not None:
        payload = stdin_text.encode("utf-8")

        def feed() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                _close_quietly(process.stdin)

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()

    threads = [
        threading.Thread(
            target=drain, args=(process.stdout, "stdout"), daemon=True
        ),
        threading.Thread(
            target=drain, args=(process.stderr, "stderr"), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process_tree(process, force=False)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process_tree(process, force=True)
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    exit_code = None
            else:
                # The leader exited, but descendants in its process group
                # may still hold the output pipes. Reap the whole group
                # before the bounded drain.
                _stop_process_tree(process, force=True)
        _join_pipe_threads(threads, process=process, writer=writer)
    except BaseException:
        # KeyboardInterrupt/cancellation of this thread: never leak the
        # child, its process group, reader threads, or the log handle.
        _stop_process_tree(process, force=True)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _close_quietly(process.stdin)
        _join_pipe_threads(threads, process=process, writer=writer)
        raise
    finally:
        if mirror_handle is not None:
            _close_quietly(mirror_handle)

    result = {
        "outcome": (
            "adapter timed out and the process was terminated"
            if timed_out
            else f"adapter finished (exit {exit_code})"
        ),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_truncated": {
            "stdout": truncated["stdout"]
            if capture_output_limit is not None
            else counts["stdout"] > MAX_CAPTURE_BYTES,
            "stderr": truncated["stderr"]
            if capture_output_limit is not None
            else counts["stderr"] > MAX_CAPTURE_BYTES,
        },
        "output_bytes": {
            "stdout": counts["stdout"],
            "stderr": counts["stderr"],
        },
        "local_log_ref": (
            f"{LOCAL_RUNS_DIR_NAME}/{local_log.name}"
            if local_log is not None
            else None
        ),
        "pid": pid,
    }
    if capture_output_limit is not None:
        result["_stdout_capture"] = captured["stdout"].decode(
            "utf-8", errors="replace"
        )
        result["_stderr_capture"] = captured["stderr"].decode(
            "utf-8", errors="replace"
        )
    return result


def _validate_provider_base(
    config: object, *, label: str, allowed: set[str]
) -> dict:
    if not isinstance(config, dict):
        raise ValidationError(
            f"{label} config must be a JSON object with optional "
            "'executable', 'model', 'working_dir', 'timeout_seconds', "
            "'env_policy', and provider-specific keys; arbitrary provider "
            "prefix arguments are not accepted"
        )
    if "args" in config:
        raise ValidationError(
            f"{label} config has unknown field 'args'; arbitrary provider "
            "prefix arguments are not accepted because they could override "
            "sandbox, permission, tool, or config-loading controls. Only the "
            "documented non-privileged options are supported."
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


def _resolved_executable(executable: str) -> str | None:
    try:
        found = shutil.which(executable)
        if found is not None:
            return found
        path = Path(executable)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    except (OSError, TypeError, ValueError):
        return None
    return None


def _safe_version(text: str) -> str | None:
    for line in text.splitlines():
        cleaned = "".join(
            char for char in line.strip()
            if char.isprintable() and char not in "\r\n\x00"
        )
        if cleaned:
            return cleaned[:200]
    return None


def _provider_probe(
    adapter: "CliAgentAdapter", validated: dict
) -> dict:
    """Passive capability probe: --version and --help only, never a model."""
    working_dir = Path(validated.get("working_dir") or ".")
    env = build_environment(validated)
    executable = validated["executable"]
    report = {
        "version": None,
        "version_error": None,
        "help_checked": False,
        "help_error": None,
        "missing_flags": [],
        "missing_controls": [],
        "error": None,
    }
    try:
        version_run = _run_captured_process(
            argv=[executable, "--version"],
            working_dir=working_dir,
            env=env,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
            local_log=None,
            label=f"{adapter.id} version probe",
            capture_output_limit=MAX_PROVIDER_PROBE_BYTES,
        )
        report["version"] = _safe_version(
            version_run.get("_stdout_capture", "")
            + "\n"
            + version_run.get("_stderr_capture", "")
        )
    except (AperioError, OSError) as exc:
        report["version_error"] = str(exc)[:300]

    try:
        help_run = _run_captured_process(
            argv=adapter._probe_argv(validated),
            working_dir=working_dir,
            env=env,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
            local_log=None,
            label=f"{adapter.id} capability probe",
            capture_output_limit=MAX_PROVIDER_PROBE_BYTES,
        )
    except (AperioError, OSError) as exc:
        report["help_error"] = str(exc)[:300]
        report["error"] = (
            f"{adapter.id} capability probe failed ({exc}); aperio refuses "
            "to launch a provider whose required controls cannot be "
            "confirmed"
        )
        return report
    help_text = (
        help_run.get("_stdout_capture", "")
        + "\n"
        + help_run.get("_stderr_capture", "")
    )
    if help_run.get("exit_code") != 0:
        report["help_error"] = (
            f"`{adapter.id} --help` exited {help_run.get('exit_code')}"
        )
        report["error"] = (
            f"{adapter.id} capability probe failed ({report['help_error']}); "
            "aperio refuses to guess provider flags"
        )
        return report
    report["help_checked"] = True
    report["missing_flags"] = [
        flag for flag in adapter._required_flags(validated)
        if flag not in help_text
    ]
    missing_tokens = [
        token for token in adapter._required_capability_tokens(validated)
        if token not in help_text
    ]
    report["missing_controls"] = report["missing_flags"] + missing_tokens
    if report["missing_controls"]:
        report["error"] = (
            f"{adapter.id} does not advertise required flag/control(s): "
            f"{', '.join(report['missing_controls'])}; aperio refuses to "
            "launch an unsupported CLI version"
        )
    return report


def _parse_codex_stdout(text: str, *, truncated: bool) -> dict:
    base = {
        "provider": CODEX_CLI_ADAPTER_ID,
        "format": "jsonl",
        "parse_status": "ok",
        "model_reported": "unknown",
        "event_count": 0,
        "turn_completed": False,
        "turn_failed": False,
        "error_events": 0,
        "unknown_events": 0,
        "item_counts": {},
        "usage": None,
    }
    if truncated:
        base["parse_status"] = "oversized"
        base["detail"] = "structured stdout exceeded the bounded capture"
        return base
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        base["parse_status"] = "empty"
        base["detail"] = "the provider emitted no structured output"
        return base
    item_counts: dict[str, int] = {}
    usage = None
    completed = failed = False
    errors = unknown = 0
    seen = 0
    for line in lines:
        if len(line.encode("utf-8", errors="replace")) > MAX_PROVIDER_EVENT_BYTES:
            base["parse_status"] = "oversized"
            base["detail"] = "a structured event exceeded the size bound"
            return base
        try:
            event = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            base["parse_status"] = "malformed"
            base["detail"] = "structured output contained invalid JSON"
            return base
        if not isinstance(event, dict):
            base["parse_status"] = "malformed"
            base["detail"] = "a structured event was not a JSON object"
            return base
        seen += 1
        if seen > MAX_PROVIDER_EVENTS:
            base["parse_status"] = "oversized"
            base["detail"] = "the provider emitted too many structured events"
            return base
        kind = event.get("type")
        if not isinstance(kind, str) or not kind:
            base["parse_status"] = "malformed"
            base["detail"] = "a structured event had no valid type"
            return base
        if kind == "turn.completed":
            completed = True
            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                usage = {}
                for key in (
                    "input_tokens", "output_tokens", "cached_input_tokens",
                    "reasoning_output_tokens",
                ):
                    value = _bounded_usage(event_usage.get(key))
                    if value is not None:
                        usage[key] = value
        elif kind in ("turn.failed", "error"):
            failed = True
            errors += 1
        elif kind == "item.completed":
            item = event.get("item")
            item_kind = item.get("type") if isinstance(item, dict) else None
            if isinstance(item_kind, str) and item_kind:
                known = {
                    "agent_message", "reasoning", "command_execution",
                    "file_change", "mcp_tool_call", "collab_tool_call",
                    "web_search", "todo_list", "error",
                }
                if item_kind in known:
                    item_counts[item_kind] = item_counts.get(item_kind, 0) + 1
                else:
                    unknown += 1
            else:
                base["parse_status"] = "malformed"
                base["detail"] = "an item.completed event had no valid item type"
                return base
        elif kind not in {
            "thread.started", "turn.started", "item.started", "item.updated",
        }:
            unknown += 1
    base.update({
        "event_count": seen,
        "turn_completed": completed,
        "turn_failed": failed,
        "error_events": errors,
        "unknown_events": unknown,
        "item_counts": item_counts,
        "usage": usage,
    })
    if failed:
        base["model_reported"] = "failed"
    elif completed:
        base["model_reported"] = "success"
    else:
        base["model_reported"] = "incomplete"
    return base


def _bounded_usage(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_PROVIDER_USAGE_VALUE:
        return None
    return value


def _parse_claude_stdout(text: str, *, truncated: bool) -> dict:
    base = {
        "provider": CLAUDE_CLI_ADAPTER_ID,
        "format": "json",
        "parse_status": "ok",
        "model_reported": "unknown",
        "event_count": 0,
        "error_events": 0,
        "unknown_events": 0,
        "subtype": None,
        "is_error": None,
        "num_turns": None,
        "duration_ms": None,
        "total_cost_usd": None,
        "usage": None,
        "permission_denials": None,
        "result_text_present": False,
    }
    if truncated:
        base["parse_status"] = "oversized"
        base["detail"] = "structured stdout exceeded the bounded capture"
        return base
    if not text.strip():
        base["parse_status"] = "empty"
        base["detail"] = "the provider emitted no structured output"
        return base
    if len(text.encode("utf-8", errors="replace")) > MAX_PROVIDER_EVENT_BYTES:
        base["parse_status"] = "oversized"
        base["detail"] = "the structured result exceeded the size bound"
        return base
    try:
        event = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        base["parse_status"] = "malformed"
        base["detail"] = "structured output was not valid JSON"
        return base
    if not isinstance(event, dict):
        base["parse_status"] = "malformed"
        base["detail"] = "structured output was not a JSON object"
        return base
    if event.get("type") != "result":
        base["parse_status"] = "malformed"
        base["detail"] = "structured output was not a Claude result message"
        return base
    subtype = event.get("subtype")
    if not isinstance(subtype, str) or not subtype:
        base["parse_status"] = "malformed"
        base["detail"] = "the result message had no valid subtype"
        return base
    is_error = event.get("is_error")
    if not isinstance(is_error, bool):
        base["parse_status"] = "malformed"
        base["detail"] = "the result message had no valid is_error flag"
        return base
    known_subtypes = {
        "success", "error_during_execution", "error_max_turns",
        "error_max_budget_usd", "error_max_structured_output_retries",
    }
    base["subtype"] = subtype if subtype in known_subtypes else "other"
    base["event_count"] = 1
    base["is_error"] = is_error
    num_turns = event.get("num_turns")
    base["num_turns"] = _bounded_usage(num_turns)
    duration = event.get("duration_ms")
    base["duration_ms"] = _bounded_usage(duration)
    cost = event.get("total_cost_usd")
    if (
        not isinstance(cost, bool)
        and isinstance(cost, (int, float))
        and math.isfinite(cost)
        and 0 <= cost <= 10**9
    ):
        base["total_cost_usd"] = float(cost)
    usage = event.get("usage")
    if isinstance(usage, dict):
        neutral_usage = {}
        for key in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = _bounded_usage(usage.get(key))
            if value is not None:
                neutral_usage[key] = value
        base["usage"] = neutral_usage
    denials = event.get("permission_denials")
    if isinstance(denials, list):
        base["permission_denials"] = min(len(denials), 1000)
    base["result_text_present"] = isinstance(event.get("result"), str)
    if is_error or subtype.startswith("error_"):
        base["model_reported"] = "failed"
        base["error_events"] = 1
    elif subtype == "success":
        base["model_reported"] = "success"
    else:
        base["model_reported"] = "incomplete"
    return base


class CliAgentAdapter(AgentAdapter):
    """Provider-neutral base for documented non-interactive coding CLIs.

    Prompt text is delivered on stdin; argv contains only static provider
    flags generated here. Arbitrary operator prefix arguments are refused,
    because provider flag precedence is not a security boundary. The
    process is bounded, observed, and mirrored exactly like local-process.
    Two separate consents are required by the runtime:

    - ``--authorize-exec`` for launching the provider process;
    - ``--authorize-write`` for a provider mode that can modify files.

    A configured working directory is a path boundary, not an OS sandbox.
    """

    requires_authorization = True
    default_executable = ""

    def available(self, config: dict | None = None) -> tuple[bool, str]:
        executable = self.default_executable
        if isinstance(config, dict):
            configured = config.get("executable")
            if isinstance(configured, str) and configured.strip():
                executable = configured.strip()
        if _resolved_executable(executable) is None:
            return False, (
                f"executable {executable!r} was not found; install the "
                "CLI or set 'executable' in the adapter config"
            )
        return True, ""

    def resolve_executable(self, config: dict | None = None) -> str | None:
        executable = self.default_executable
        if isinstance(config, dict):
            configured = config.get("executable")
            if isinstance(configured, str) and configured.strip():
                executable = configured.strip()
        return _resolved_executable(executable)

    def validate_config(self, config: dict | None, aperio_dir: Path) -> dict:
        validated = self._validate_provider_config(config)
        resolved = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        # Normalize once so probes, previews, and execution all use the
        # same validated path rather than re-resolving a relative value.
        validated["working_dir"] = str(resolved)
        return validated

    def _validate_provider_config(self, config: dict | None) -> dict:
        raise NotImplementedError

    def _provider_args(
        self, validated: dict, working_dir: Path, *, workspace_write: bool
    ) -> list[str]:
        raise NotImplementedError

    def _argv(
        self, validated: dict, working_dir: Path, *, workspace_write: bool
    ) -> list[str]:
        return [
            validated["executable"],
            *self._provider_args(
                validated, working_dir, workspace_write=workspace_write
            ),
        ]

    def _effective_permissions(
        self, validated: dict, *, workspace_write: bool
    ) -> dict:
        raise NotImplementedError

    def _requested_workspace_write(self, validated: dict) -> bool:
        return False

    def _probe_argv(self, validated: dict) -> list[str]:
        return [validated["executable"], "--help"]

    def _required_flags(self, validated: dict) -> tuple[str, ...]:
        return ()

    def _required_capability_tokens(self, validated: dict) -> tuple[str, ...]:
        """Values/semantics the version must advertise in ``--help``."""
        return ()

    def _parse_structured(self, text: str, *, truncated: bool) -> dict:
        raise NotImplementedError

    def authorization_error(
        self, config: dict | None, *, authorize_workspace_write: bool
    ) -> str | None:
        if not isinstance(config, dict):
            return None
        if (
            self._requested_workspace_write(config)
            and not authorize_workspace_write
        ):
            return (
                f"{self.id} is configured to request workspace writes or "
                "shell access, but the separate workspace-write "
                "authorization was not given. Re-run with --authorize-write "
                "(or --authorize-workspace-write). No run record was created "
                "and no task was claimed."
            )
        return None

    def capabilities(self, validated: dict) -> dict:
        probe = _provider_probe(self, validated)
        read_only = not probe["error"]
        return {
            "installed": True,
            "version": probe["version"],
            "version_error": probe["version_error"],
            "help_checked": probe["help_checked"],
            "help_error": probe["help_error"],
            "required_flags": list(self._required_flags(validated)),
            "required_controls": (
                list(self._required_flags(validated))
                + list(self._required_capability_tokens(validated))
            ),
            "missing_flags": probe["missing_flags"],
            "missing_controls": probe["missing_controls"],
            "capability_error": probe["error"],
            "read_only_supported": read_only,
            "workspace_write_supported": read_only,
        }

    def check_capabilities(self, config: dict | None) -> None:
        if not isinstance(config, dict):
            return
        report = self.capabilities(config)
        if report["capability_error"]:
            raise StateError(
                f"adapter {self.id!r} cannot be launched safely: "
                f"{report['capability_error']}. No run record was created "
                "and no task was claimed."
            )

    def _command_payload(
        self,
        validated: dict,
        working_dir: Path,
        *,
        task: dict | None,
        workspace_write_authorized: bool,
    ) -> dict:
        permissions = self._effective_permissions(
            validated, workspace_write=workspace_write_authorized
        )
        command = {
            "executable": validated["executable"],
            "args": self._argv(
                validated,
                working_dir,
                workspace_write=workspace_write_authorized,
            )[1:],
            "working_dir": str(working_dir),
            "timeout_seconds": validated["timeout_seconds"],
            "env_policy": validated["env_policy"],
            "prompt_transport": "stdin",
            "effective_permissions": permissions,
            "requested_workspace_write": self._requested_workspace_write(
                validated
            ),
            "workspace_write_authorized": workspace_write_authorized,
        }
        if task is not None:
            prompt = render_agent_prompt(
                run_id="RUN-NNNN", task=task, working_dir=working_dir
            )
            encoded = prompt.encode("utf-8")
            command["prompt_bytes"] = len(encoded)
            command["prompt_sha256"] = hashlib.sha256(encoded).hexdigest()
        return command

    def command_preview(
        self,
        config: dict | None,
        aperio_dir: Path,
        *,
        task: dict | None = None,
        workspace_write_authorized: bool = False,
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        return self._command_payload(
            validated,
            working_dir,
            task=task,
            workspace_write_authorized=workspace_write_authorized,
        )

    def preview(
        self,
        *,
        task: dict,
        config: dict | None,
        aperio_dir: Path,
        authorize_workspace_write: bool = False,
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        command = self._command_payload(
            validated,
            working_dir,
            task=task,
            workspace_write_authorized=authorize_workspace_write,
        )
        permissions = command["effective_permissions"]
        risk_notes = [
            "launches a real installed AI coding CLI with the operator's "
            "privileges; a configured working directory is a path boundary, "
            "not an OS sandbox",
            "the prompt is sent over stdin and is not written to the "
            "shareable execution record",
            "exit 0 only means the provider process finished; Aperio "
            "verification and independent review are still required",
            "effective provider permissions: "
            + json.dumps(permissions, sort_keys=True, ensure_ascii=False),
        ]
        if command["requested_workspace_write"] and not authorize_workspace_write:
            risk_notes.append(
                "workspace write or shell access was requested but is not "
                "authorized; this preview is read-only and an authorized "
                "launch would be refused without --authorize-write"
            )
        return {
            "adapter_id": self.id,
            "mode": (
                "non-interactive CLI execution (requires --authorize-exec"
                + (
                    " and --authorize-write)"
                    if command["requested_workspace_write"]
                    else ")"
                )
            ),
            "task_id": task["id"],
            **command,
            "command": command,
            "risk_notes": risk_notes,
        }

    def start(
        self,
        *,
        run_id: str,
        aperio_dir: Path,
        task: dict,
        config: dict | None,
        on_running=None,
        workspace_write_authorized: bool = False,
    ) -> dict:
        validated = self.validate_config(config, aperio_dir)
        if (
            self._requested_workspace_write(validated)
            and not workspace_write_authorized
        ):
            raise StateError(self.authorization_error(
                validated, authorize_workspace_write=False
            ) or "workspace-write authorization is required")
        working_dir = _resolve_project_working_dir(
            validated.get("working_dir"), aperio_dir, self.id
        )
        prompt = render_agent_prompt(
            run_id=run_id, task=task, working_dir=working_dir
        )
        argv = self._argv(
            validated,
            working_dir,
            workspace_write=workspace_write_authorized,
        )
        local_log = self._log_path(aperio_dir, run_id)
        result = _run_captured_process(
            argv=argv,
            working_dir=working_dir,
            env=build_environment(validated),
            timeout_seconds=validated["timeout_seconds"],
            local_log=local_log,
            label=self.id,
            stdin_text=prompt,
            capture_output_limit=MAX_PROVIDER_STDOUT_BYTES,
            on_running=on_running,
        )
        stdout = result.pop("_stdout_capture", "")
        result.pop("_stderr_capture", None)
        result["provider_result"] = self._parse_structured(
            stdout, truncated=bool(result["output_truncated"]["stdout"])
        )
        return result


class CodexCliAdapter(CliAgentAdapter):
    """OpenAI Codex CLI via the documented non-interactive ``codex exec``."""

    id = CODEX_CLI_ADAPTER_ID
    display_name = "OpenAI Codex CLI (non-interactive exec)"
    default_executable = "codex"

    def _validate_provider_config(self, config: dict | None) -> dict:
        config = {} if config is None else config
        allowed = {
            "executable", "model", "working_dir", "timeout_seconds",
            "env_policy", "sandbox", "skip_git_repo_check", "ephemeral",
        }
        validated = _validate_provider_base(
            config, label=self.id, allowed=allowed
        )
        if validated["executable"] == "":
            validated["executable"] = self.default_executable
        sandbox = config.get("sandbox", "read-only")
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

    def _requested_workspace_write(self, validated: dict) -> bool:
        return validated.get("sandbox") == "workspace-write"

    def _effective_permissions(
        self, validated: dict, *, workspace_write: bool
    ) -> dict:
        effective = (
            "workspace-write"
            if workspace_write and self._requested_workspace_write(validated)
            else "read-only"
        )
        return {
            "provider_mode": effective,
            "sandbox": effective,
            "workspace_write": effective == "workspace-write",
            "shell": True,
            "shell_scope": (
                "provider sandbox permits commands; filesystem writes are "
                + ("allowed inside the workspace" if effective == "workspace-write"
                   else "blocked by the read-only sandbox")
            ),
            "approval_policy": "never",
            "tools": None,
        }

    def _provider_args(
        self, validated: dict, working_dir: Path, *, workspace_write: bool
    ) -> list[str]:
        effective = self._effective_permissions(
            validated, workspace_write=workspace_write
        )["sandbox"]
        args = [
            "exec",
            "--json",
            "--sandbox", effective,
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

    def _probe_argv(self, validated: dict) -> list[str]:
        return [validated["executable"], "exec", "--help"]

    def _required_flags(self, validated: dict) -> tuple[str, ...]:
        # Safety-relevant controls only. Optional documented flags that
        # a provider may omit from its help are allowed to fail at process
        # startup rather than causing a false capability refusal.
        return ("--json", "--sandbox", "--cd", "--config")

    def _parse_structured(self, text: str, *, truncated: bool) -> dict:
        return _parse_codex_stdout(text, truncated=truncated)


class ClaudeCliAdapter(CliAgentAdapter):
    """Anthropic Claude Code via documented non-interactive print mode."""

    id = CLAUDE_CLI_ADAPTER_ID
    display_name = "Anthropic Claude Code CLI (print mode)"
    default_executable = "claude"

    def _validate_provider_config(self, config: dict | None) -> dict:
        config = {} if config is None else config
        if isinstance(config, dict) and "permission_mode" in config:
            mode = config.get("permission_mode")
            if mode == "bypassPermissions":
                raise ValidationError(
                    "claude-cli config: 'permission_mode' cannot be "
                    "'bypassPermissions'; permission bypass is deliberately "
                    "not offered. Use --authorize-exec/--authorize-write and "
                    "the derived plan/acceptEdits modes."
                )
            raise ValidationError(
                "claude-cli config: 'permission_mode' is not configurable; "
                "aperio derives 'plan' (read-only) or 'acceptEdits' "
                "(workspace-write) from the explicit execution and write "
                "authorizations, so mode and tools cannot conflict."
            )
        if isinstance(config, dict) and "allowed_tools" in config:
            raise ValidationError(
                "claude-cli config: 'allowed_tools' is not configurable; "
                "aperio derives a restrictive --tools/--allowedTools set "
                "from the selected mode. Bash requires allow_shell=true plus "
                "workspace-write authorization."
            )
        allowed = {
            "executable", "model", "working_dir", "timeout_seconds",
            "env_policy", "allow_shell", "max_turns", "max_budget_usd",
            "no_session_persistence",
        }
        validated = _validate_provider_base(
            config, label=self.id, allowed=allowed
        )
        if validated["executable"] == "":
            validated["executable"] = self.default_executable
        validated["allow_shell"] = _config_bool(
            config, "allow_shell", False, self.id
        )
        turns = config.get("max_turns")
        if turns is not None:
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

    def _requested_workspace_write(self, validated: dict) -> bool:
        return bool(validated.get("allow_shell"))

    def _effective_permissions(
        self, validated: dict, *, workspace_write: bool
    ) -> dict:
        if workspace_write:
            tools = list(CLAUDE_WRITE_TOOLS)
            if validated.get("allow_shell"):
                tools.append(CLAUDE_SHELL_TOOL)
            mode = "acceptEdits"
        else:
            tools = list(CLAUDE_READ_ONLY_TOOLS)
            mode = "plan"
        return {
            "provider_mode": mode,
            "permission_mode": mode,
            "workspace_write": workspace_write,
            "shell": CLAUDE_SHELL_TOOL in tools,
            "tools": tools,
            "allowed_tools": list(tools),
            "permission_prompts": "none (prompted tools are denied)",
            "network_tools": False,
        }

    def _provider_args(
        self, validated: dict, working_dir: Path, *, workspace_write: bool
    ) -> list[str]:
        permissions = self._effective_permissions(
            validated, workspace_write=workspace_write
        )
        tools = ",".join(permissions["tools"])
        args = [
            "-p",
            "--permission-mode", permissions["permission_mode"],
            "--permission-prompts", "none",
            # --tools restricts what exists; --allowedTools auto-approves
            # only that same restrictive set. Never mix the two meanings.
            "--tools", tools,
            "--allowedTools", tools,
            "--output-format", "json",
        ]
        if validated["max_turns"] is not None:
            args.extend(["--max-turns", str(validated["max_turns"])])
        if validated["no_session_persistence"]:
            args.append("--no-session-persistence")
        if validated["max_budget_usd"] is not None:
            args.extend([
                "--max-budget-usd", f"{validated['max_budget_usd']:.2f}"
            ])
        if validated["model"]:
            args.extend(["--model", validated["model"]])
        return args

    def _probe_argv(self, validated: dict) -> list[str]:
        return [validated["executable"], "--help"]

    def _required_flags(self, validated: dict) -> tuple[str, ...]:
        flags = [
            "--print", "--permission-mode", "--permission-prompts",
            "--tools", "--allowedTools", "--output-format",
            "--no-session-persistence",
        ]
        if validated.get("max_budget_usd") is not None:
            flags.append("--max-budget-usd")
        if validated.get("model"):
            flags.append("--model")
        return tuple(flags)

    def _required_capability_tokens(self, validated: dict) -> tuple[str, ...]:
        # --help advertises the supported output values and permission
        # modes. Refuse older/newer CLIs that do not expose the modes the
        # derived policy relies on instead of guessing their semantics.
        return ("json", "plan", "acceptEdits")

    def _parse_structured(self, text: str, *, truncated: bool) -> dict:
        return _parse_claude_stdout(text, truncated=truncated)


def provider_doctor(
    adapter_id: str, *, config: dict | None, aperio_dir: Path
) -> dict:
    """Passive provider diagnostics. Never contacts a model or logs in."""
    adapter = get_adapter(adapter_id)
    if not isinstance(adapter, CliAgentAdapter):
        raise ValidationError(
            f"agent-doctor supports provider CLI adapters only; "
            f"{adapter_id!r} is not one"
        )
    resolved = adapter.resolve_executable(config)
    report = {
        "adapter_id": adapter_id,
        "installed": resolved is not None,
        "executable": resolved,
        "version": None,
        "version_error": None,
        "installation_error": None,
        "required_flags": [],
        "required_controls": [],
        "missing_flags": [],
        "missing_controls": [],
        "configuration": {"valid": False, "error": None},
        "authentication": {
            "status": "unknown",
            "checked": False,
            "note": (
                "passive diagnostics never read credentials, perform login, "
                "or make model requests"
            ),
        },
        "read_only_execution": {"supported": False},
        "workspace_write_execution": {
            "supported": False,
            "requires": "--authorize-write (or --authorize-workspace-write)",
        },
        "live_integration": {
            "status": "unverified",
            "note": "no live model request was made by this diagnostic",
        },
        "capability_error": None,
    }
    try:
        validated = adapter.validate_config(config, aperio_dir)
    except ValidationError as exc:
        report["configuration"]["error"] = str(exc)
        return report
    report["configuration"]["valid"] = True
    if resolved is None:
        report["installation_error"] = (
            f"executable {validated['executable']!r} was not found"
        )
        return report
    capabilities = adapter.capabilities(validated)
    report["version"] = capabilities["version"]
    report["version_error"] = capabilities["version_error"]
    report["required_flags"] = capabilities["required_flags"]
    report["required_controls"] = capabilities["required_controls"]
    report["missing_flags"] = capabilities["missing_flags"]
    report["missing_controls"] = capabilities["missing_controls"]
    report["capability_error"] = capabilities["capability_error"]
    report["read_only_execution"] = {
        "supported": capabilities["read_only_supported"],
        "mode": "provider read-only sandbox / plan mode",
    }
    report["workspace_write_execution"] = {
        "supported": capabilities["workspace_write_supported"],
        "requires": "--authorize-write (or --authorize-workspace-write)",
        "shell_requires": (
            "allow_shell=true plus workspace-write authorization"
            if adapter_id == CLAUDE_CLI_ADAPTER_ID
            else "workspace-write sandbox plus authorization"
        ),
    }
    return report

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

    def preview(
        self, *, task: dict, config: dict | None, aperio_dir: Path,
        authorize_workspace_write: bool = False,
    ) -> dict:
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
        self, config: dict | None, aperio_dir: Path, *, task: dict | None = None,
        workspace_write_authorized: bool = False,
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
        workspace_write_authorized: bool = False,
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
