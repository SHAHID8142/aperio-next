"""Agent adapter contract and the two built-in adapters (Feature 04).

An adapter connects Aperio's task graph to an execution-capable agent.
Adapters never decide policy: authorization (``--authorize-exec``) is
enforced by the runtime (:mod:`aperio.runtime`), which is the only caller
of :meth:`AgentAdapter.start`. Adapters expose:

- ``id`` / ``display_name`` — stable identifiers
- ``available()`` — environment-level capability check only (never
  config-dependent; config validation happens at prepare/start)
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
  grace), stdin DEVNULL, always reaped.

Honest boundary: ``--authorize-exec`` is a procedural human-in-the-loop
checkpoint, not a sandbox. A tool with shell access can bypass aperio
entirely; the gate exists so agent-configured execution always requires
an explicit human decision. Subprocesses are treated as potentially
powerful and NOT sandboxed.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

from aperio.errors import PathError, ValidationError
from aperio.inputs import check_no_foreign_line_separators

MANUAL_ADAPTER_ID = "manual"
LOCAL_PROCESS_ADAPTER_ID = "local-process"

RUNS_DIR_NAME = "runs"
LOCAL_RUNS_DIR_NAME = "local/runs"
RUN_HANDOFF_MARKER = "<!-- aperio:run-handoff:v1 -->"

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 3600
MAX_CAPTURE_BYTES = 64 * 1024  # per stream, retained tail

_ARGV_ITEM_RE = re.compile(r"^[^\x00]*$")  # NUL is never legal in argv


def runs_dir(aperio_dir: Path) -> Path:
    """Directory holding per-run portable artifacts (shareable)."""
    return aperio_dir / RUNS_DIR_NAME


def local_runs_dir(aperio_dir: Path) -> Path:
    """Machine-local directory for raw run output (never shared)."""
    return aperio_dir / LOCAL_RUNS_DIR_NAME


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


def sanitize_diagnostics(text: str, limit: int = 2000) -> str:
    """Bound and clean free-form diagnostics for the shareable record.

    Decoding already used errors='replace'; control characters other than
    newline/tab are dropped so the shareable record stays readable, and
    the text is hard-truncated (the full output lives only in the
    machine-local log).
    """
    cleaned = "".join(
        char for char in text if char in ("\n", "\t") or ord(char) >= 32
    )
    cleaned = cleaned.strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + f"... [truncated, {len(cleaned)} chars total]"
    return cleaned


class AgentAdapter:
    """Contract for execution-capable agents (plain class, no magic).

    Subclasses implement ``preview`` and ``start``; authorization is
    enforced by the runtime, which calls ``start`` only in execute mode.
    """

    id: str = "abstract"
    display_name: str = "abstract adapter"

    def available(self) -> tuple[bool, str]:
        """Environment-level capability check (never config-dependent)."""
        return True, ""

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

        :returns: a result dict with at least ``outcome``, ``exit_code``
            (int or None), and ``diagnostics``.
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

    def available(self) -> tuple[bool, str]:
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
        handoff_path = runs_dir(aperio_dir) / f"{run_id}.handoff.md"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        if handoff_path.is_symlink():
            raise PathError(
                f"refusing to follow symlink at {handoff_path}; "
                "remove it and re-run 'aperio init' to repair"
            )
        if handoff_path.exists():
            raise ValidationError(
                f"run handoff already exists at {handoff_path}; "
                "refusing to overwrite it"
            )
        document = render_run_handoff(run_id=run_id, task=task)
        # Exclusive creation: a file that raced in is never truncated.
        fd = os.open(handoff_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(document)
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

    def available(self) -> tuple[bool, str]:
        return True, ""

    def validate_config(self, config: dict | None, aperio_dir: Path) -> dict:
        """Validate config (and the working-directory boundary) BEFORE
        any persistent mutation."""
        import sys as _sys
        print(f"DEBUG validate_config: config={config!r} type={type(config)}", file=_sys.stderr)
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

    def _validate_config(self, config: dict | None, aperio_dir: Path) -> dict:
        return self.validate_config(config, aperio_dir)

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
        validated = self._validate_config(config, aperio_dir)
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
                "output is captured machine-locally; only a bounded, "
                "sanitized tail enters the shareable record",
                "exit 0 means the adapter finished - task completion still "
                "requires Feature-03 verification with evidence",
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
        validated = self._validate_config(config, aperio_dir)
        working_dir = self._resolve_working_dir(validated, aperio_dir)

        argv = [validated["executable"], *validated["args"]]
        local_log = local_runs_dir(aperio_dir) / f"{run_id}.log"
        local_log.parent.mkdir(parents=True, exist_ok=True)
        if os.path.islink(local_log):
            raise PathError(
                f"refusing to follow symlink at {local_log}; "
                "remove it and re-run the command to repair"
            )
        if local_log.exists():
            raise StateError(
                f"a local output log already exists at {local_log}; run "
                "ids are unique, so this is a leftover from an older "
                "aperio version — remove it manually if it is not needed"
            )
        try:
            mirror_handle = open(local_log, "xb")
        except OSError as exc:
            raise StateError(
                f"cannot create local output log {local_log}: "
                f"{exc.strerror or exc}; nothing was executed"
            ) from None

        stdout_tail, stderr_tail, truncated = {"text": "", "bytes": 0}, {
            "text": "",
            "bytes": 0,
        }, {"stdout": 0, "stderr": 0}

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
                env=build_environment(validated),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ValidationError(
                f"local-process: executable not found: {validated['executable']}"
            ) from None
        except PermissionError as exc:
            raise ValidationError(
                f"local-process: cannot execute {validated['executable']}: "
                f"{exc.strerror or exc}"
            ) from None
        except ValueError as exc:
            raise ValidationError(
                f"local-process: invalid argv: {exc}"
            ) from None

        pid = process.pid
        if on_running is not None:
            try:
                on_running(pid)
            except BaseException:
                # The run was cancelled or rejected during startup: kill
                # and reap the child, then propagate the refusal.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise

        threads = [
            threading.Thread(target=drain, args=(process.stdout, "stdout")),
            threading.Thread(target=drain, args=(process.stderr, "stderr")),
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=validated["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()  # POSIX: SIGTERM; Windows: TerminateProcess
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()  # SIGKILL / TerminateProcess
                exit_code = process.wait()
        for thread in threads:
            thread.join()
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
        # Privacy boundary: raw output stays in the machine-local log;
        # the shareable record carries only neutral metadata.
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
            "local_log_ref": f"{LOCAL_RUNS_DIR_NAME}/{run_id}.log",
            "pid": pid,
        }


def get_adapter(adapter_id: str) -> AgentAdapter:
    """Look up a built-in adapter by id."""
    for adapter in (ManualHandoffAdapter(), LocalProcessAdapter()):
        if adapter.id == adapter_id:
            return adapter
    raise ValidationError(
        f"unknown agent adapter {adapter_id!r} "
        f"(available: {MANUAL_ADAPTER_ID}, {LOCAL_PROCESS_ADAPTER_ID})"
    )
