"""Execution runtime: records, eligibility, dispatch, reconciliation.

Execution records live in ``.aperio/executions.json`` (exec-schema v1,
dict-based, unknown keys preserved) and every mutation of the existing
store follows the single-snapshot protocol under ``executions.lock``:
one snapshot read → rule checks against that snapshot → mutate →
exclusive temp file → byte compare-and-swap → atomic rename. Initial
creation happens under the same lock with an absent snapshot.

Dispatch additionally holds ``.aperio/dispatch.lock`` for its whole
duration (short bounded wait: concurrent dispatch fails fast). Lock
discipline: at most one long-held dispatch lock; the inner artifact
locks (executions, plan) are acquired one at a time, never
simultaneously — a strictly outer-inner order with no cycles, so no
deadlock is possible.

Run lifecycle (execution status is SEPARATE from the Feature-03 task
status; exit 0 never completes a task)::

    running (local process started; pid+machine recorded atomically)
    completed (adapter finished — NOT task verification)
    failed (nonzero exit, timeout, missing executable, claim lost, ...)
    cancelled (explicit run-cancel, or stale prepared record)
    interrupted (reconciliation: recorded process no longer alive)

Manual handoff runs go straight from the record-creation step to
``awaiting-external``. Honest guarantees: the byte-CAS detects
non-cooperating writes landing before the compare; a write between the
compare and the rename can still win. Crash-consistency is best-effort
(no fsync). Pids are only interpreted on the machine that recorded them
(a hostname token is stored alongside); same-machine pid reuse is a
documented residual hazard borne by the explicit run-cancel action.

Privacy boundary: raw subprocess output, external-reported outcomes and
external-agent diagnostics are stored ONLY machine-locally
(``.aperio/local/runs/``); shareable records carry neutral outcome
metadata, exit codes, truncation indicators, and safe project-relative
artifact references — never child output, raw external text, absolute
machine paths, or full argv.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

from aperio.adapters import (
    MAX_PROVIDER_COUNT_VALUE,
    MAX_PROVIDER_COST_USD,
    MAX_PROVIDER_METADATA_VALUE,
    MANUAL_ADAPTER_ID,
    get_adapter,
    runs_dir,
)
from aperio.artifacts import (
    check_artifact_dir,
    create_exclusive_text,
    read_confined_artifact,
)
from aperio.errors import (
    AperioError,
    NotInitializedError,
    PathError,
    StateError,
    ValidationError,
)
from aperio.locks import LOCK_TIMEOUT_SECONDS, acquire_lock, release_lock
from aperio.policy import append_event as _policy_event
from aperio.policy import load_policy, require_action
from aperio.planstore import read_plan, release_failed_claim, update_task
from aperio.project import require_supported_state
from aperio.schema import utc_now_iso

EXECUTIONS_FILE_NAME = "executions.json"
EXECUTIONS_TMP_NAME = "executions.json.tmp"
EXECUTIONS_LOCK_NAME = "executions.lock"
DISPATCH_LOCK_NAME = "dispatch.lock"
EXECUTIONS_SCHEMA_VERSION = 1

RUN_STATUSES = (
    "prepared",
    "awaiting-external",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)
ACTIVE_RUN_STATUSES = ("prepared", "awaiting-external", "running")
RUN_ID_RE = re.compile(r"^RUN-\d+$")
RUN_HANDOFF_SUFFIX = ".handoff.md"
EXTERNAL_REPORT_FILE_SUFFIX = ".report.txt"


def executions_path(aperio_dir: Path) -> Path:
    return aperio_dir / EXECUTIONS_FILE_NAME


def executions_tmp_path(aperio_dir: Path) -> Path:
    return aperio_dir / EXECUTIONS_TMP_NAME


def executions_lock_path(aperio_dir: Path) -> Path:
    return aperio_dir / EXECUTIONS_LOCK_NAME


def dispatch_lock_path(aperio_dir: Path) -> Path:
    return aperio_dir / DISPATCH_LOCK_NAME


def machine_token() -> str:
    """Identity of the machine that recorded a pid (hostname).

    Pids are only meaningful on the machine that created them; records
    synced to another machine are reported, never decided, by
    reconciliation and cancellation.
    """
    return platform.node()


# --- snapshot read/parse --------------------------------------------------------


def _read_snapshot_bytes(path: Path) -> bytes | None:
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading "
            "execution records; remove it and re-run 'aperio init' to repair"
        )
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}"
        ) from None


def _decode_executions(raw: bytes, path: Path) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify "
            "it. Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: "
            f"{exc.msg}); aperio will not overwrite it. Inspect or restore "
            "the file manually."
        ) from None
    return data


def validate_executions_document(data: object, path: Path) -> dict:
    """Validate a parsed executions document (unknown keys tolerated)."""
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: executions must be a JSON object")
    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise StateError(
            f"{path} is corrupt: 'schema_version' must be an integer"
        )
    if schema_version != EXECUTIONS_SCHEMA_VERSION:
        raise StateError(
            f"{path} was written with execution-record schema version "
            f"{schema_version}, but this aperio supports exactly version "
            f"{EXECUTIONS_SCHEMA_VERSION}; aperio will not modify it."
        )
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise StateError(f"{path} is corrupt: 'runs' must be a list")
    seen: set[str] = set()
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise StateError(f"{path} is corrupt: runs[{index}] must be an object")
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("RUN-"):
            raise StateError(
                f"{path} is corrupt: runs[{index}].run_id is invalid"
            )
        if run_id in seen:
            raise StateError(
                f"{path} is corrupt: duplicate run_id {run_id}"
            )
        seen.add(run_id)
        if run.get("status") not in RUN_STATUSES:
            raise StateError(
                f"{path} is corrupt: runs[{index}].status is invalid"
            )
        for key in ("task_id", "adapter_id", "started_at"):
            if not isinstance(run.get(key), str) or not run[key]:
                raise StateError(
                    f"{path} is corrupt: runs[{index}].{key} must be a "
                    "non-empty string"
                )
        provider_result = run.get("provider_result")
        if provider_result is not None:
            _validate_provider_result(provider_result, path, index)
    return data


def _validate_provider_result(value: object, path: Path, index: int) -> None:
    """Provider events are untrusted input; records carry only neutral metadata."""
    if not isinstance(value, dict):
        raise StateError(
            f"{path} is corrupt: runs[{index}].provider_result must be an "
            "object"
        )
    for key, allowed in (
        ("provider", {"codex-cli", "claude-cli"}),
        ("format", {"json", "jsonl"}),
        ("parse_status", {"ok", "empty", "malformed", "oversized"}),
        ("model_reported", {"success", "failed", "incomplete", "unknown"}),
    ):
        if value.get(key) not in allowed:
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.{key} "
                "is invalid"
            )
    for key in ("event_count", "error_events", "unknown_events"):
        number = value.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 0 <= number <= MAX_PROVIDER_COUNT_VALUE
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.{key} "
                "must be a bounded non-negative integer"
            )
    allowed_keys = {
        "provider", "format", "parse_status", "model_reported", "detail",
        "event_count", "error_events", "unknown_events", "subtype",
        "is_error", "num_turns", "duration_ms", "total_cost_usd", "usage",
        "permission_denials", "result_text_present", "turn_completed",
        "turn_failed", "item_counts",
    }
    if set(value) - allowed_keys:
        raise StateError(
            f"{path} is corrupt: runs[{index}].provider_result has unknown "
            "fields (provider output must stay neutral metadata)"
        )
    detail = value.get("detail")
    if detail is not None and (
        not isinstance(detail, str) or len(detail) > 500
    ):
        raise StateError(
            f"{path} is corrupt: runs[{index}].provider_result.detail is "
            "invalid"
        )
    usage = value.get("usage")
    if usage is not None:
        usage_keys = {
            "input_tokens", "output_tokens", "cached_input_tokens",
            "reasoning_output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        }
        if not isinstance(usage, dict) or set(usage) - usage_keys or any(
            isinstance(v, bool) or not isinstance(v, int)
            or not 0 <= v <= MAX_PROVIDER_METADATA_VALUE
            for v in usage.values()
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.usage is "
                "invalid"
            )
    if value["provider"] == "codex-cli":
        if not isinstance(value.get("turn_completed"), bool) or not isinstance(
            value.get("turn_failed"), bool
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result turn "
                "flags must be booleans"
            )
        counts = value.get("item_counts")
        count_keys = {
            "agent_message", "reasoning", "command_execution", "file_change",
            "mcp_tool_call", "collab_tool_call", "web_search", "todo_list",
            "error",
        }
        if not isinstance(counts, dict) or set(counts) - count_keys or any(
            isinstance(v, bool) or not isinstance(v, int)
            or not 0 <= v <= MAX_PROVIDER_COUNT_VALUE
            for v in counts.values()
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.item_counts "
                "must be a bounded known-item object"
            )
    else:
        subtype = value.get("subtype")
        if subtype is not None and subtype not in {
            "success", "error_during_execution", "error_max_turns",
            "error_max_budget_usd", "error_max_structured_output_retries",
            "other",
        }:
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.subtype "
                "is invalid"
            )
        if value.get("is_error") is not None and not isinstance(
            value.get("is_error"), bool
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result.is_error "
                "is invalid"
            )
        if value.get("result_text_present") is not None and not isinstance(
            value.get("result_text_present"), bool
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result."
                "result_text_present is invalid"
            )
        for key in ("num_turns", "duration_ms", "permission_denials"):
            number = value.get(key)
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int)
                or not 0 <= number <= MAX_PROVIDER_METADATA_VALUE
            ):
                raise StateError(
                    f"{path} is corrupt: runs[{index}].provider_result.{key} "
                    "is invalid"
                )
        cost = value.get("total_cost_usd")
        if cost is not None and (
            isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or not 0 <= cost <= MAX_PROVIDER_COST_USD
        ):
            raise StateError(
                f"{path} is corrupt: runs[{index}].provider_result."
                "total_cost_usd is invalid"
            )


def parse_executions_snapshot(raw: bytes, path: Path) -> dict | None:
    """Parse raw executions bytes; None means the file does not exist yet.

    :raises StateError: corrupt or unsupported exec-schema versions.
    """
    data = _decode_executions(raw, path)
    return validate_executions_document(data, path)


def read_executions(aperio_dir: Path) -> dict | None:
    """Read the executions document; None when no runs were ever recorded."""
    require_supported_state(aperio_dir)
    path = executions_path(aperio_dir)
    raw = _read_snapshot_bytes(path)
    if raw is None:
        return None
    return parse_executions_snapshot(raw, path)


def read_executions_or_fail(aperio_dir: Path) -> dict:
    """Read executions or raise a clear 'no runs' StateError."""
    document = read_executions(aperio_dir)
    if document is None:
        raise StateError(
            f"no execution records at {executions_path(aperio_dir)} "
            "(no run has ever been dispatched)"
        )
    return document


def _snapshot_or_new(aperio_dir: Path) -> tuple[dict, bytes | None]:
    """Read the executions snapshot under the caller's lock; returns
    (document-or-minimal-new, raw-bytes-or-None)."""
    path = executions_path(aperio_dir)
    raw = _read_snapshot_bytes(path)
    if raw is None:
        return {"schema_version": EXECUTIONS_SCHEMA_VERSION, "runs": []}, None
    return parse_executions_snapshot(raw, path), raw


def _publish_executions(aperio_dir: Path, data: dict, snapshot: bytes | None) -> None:
    path = executions_path(aperio_dir)
    tmp = executions_tmp_path(aperio_dir)
    validate_executions_document(data, path)
    try:
        payload = json.dumps(
            data, indent=2, ensure_ascii=False, allow_nan=False
        ) + "\n"
    except ValueError as exc:
        raise StateError(
            f"cannot serialize execution records at {path}: {exc}; "
            "non-standard JSON values (NaN/Infinity) in extension fields "
            "must be resolved manually; nothing was modified"
        ) from None
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot prepare execution-record write at {tmp}: "
            f"{exc.strerror or exc}; the temp name is occupied by a "
            "concurrent aperio write (retry shortly) or a leftover from a "
            "crashed write (removing it is your decision); nothing was "
            "modified"
        ) from None

    try:
        current = path.read_bytes() if snapshot is not None else None
    except FileNotFoundError:
        current = None
        if snapshot is not None:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"the execution records at {path} disappeared during the "
                "write; nothing was modified"
            ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot read {path} before publishing: {exc.strerror or exc}; "
            "nothing was modified"
        ) from None
    if snapshot is None:
        if current is not None:
            _discard_created_tmp(tmp, created_tmp)
            raise StateError(
                f"execution records appeared at {path} during creation; "
                "nothing was modified. Re-run the command."
            )
    elif current != snapshot:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"the execution records at {path} changed during the write "
            "(a tool outside aperio's coordination may have written it); "
            "nothing was modified. Re-read and retry."
        )

    try:
        os.replace(tmp, path)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish execution records at {path}: "
            f"{exc.strerror or exc}; no existing content was modified"
        ) from None


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover is a user-resolved conflict


# --- eligibility ------------------------------------------------------------------


@dataclass(frozen=True)
class Eligibility:
    """Deterministic result of the execution eligibility gate."""

    task_id: str
    eligible: bool
    reason: str
    plan_version: int
    plan_approved: bool
    already_claimed: bool  # task is in-progress (e.g. a retry situation)


def check_eligibility(plan: dict, task_id: str, active_run: dict | None) -> Eligibility:
    """Evaluate execution eligibility for one task.

    Plan approval and the active-run check are evaluated here against the
    snapshot the caller supplies; dependency and status checks run here
    too, so the whole decision is made against one consistent read.
    """
    tasks = {task["id"]: task for task in plan["tasks"]}
    if task_id not in tasks:
        return Eligibility(task_id, False, "no such task in the plan",
                           plan["plan_version"], False, False)
    task = tasks[task_id]
    approval = plan["approval"]
    approved = approval["status"] == "approved"
    if not approved:
        return Eligibility(
            task_id, False,
            f"plan revision {plan['plan_version']} is "
            f"{approval['status']!r}; execution tracking requires an "
            "approved plan",
            plan["plan_version"], False, task["status"] == "in-progress",
        )
    if task["status"] == "blocked":
        return Eligibility(
            task_id, False,
            f"task is blocked ({task['blocked_reason']!r})",
            plan["plan_version"], True, False,
        )
    if task["status"] == "completed":
        return Eligibility(
            task_id, False, "task is already completed",
            plan["plan_version"], True, False,
        )
    if active_run is not None:
        return Eligibility(
            task_id, False,
            f"an active run already exists for this task "
            f"({active_run['run_id']}, {active_run['status']!r})",
            plan["plan_version"], True, task["status"] == "in-progress",
        )
    unmet = [
        dep
        for dep in task["depends_on"]
        if next(t["status"] for t in plan["tasks"] if t["id"] == dep)
        != "completed"
    ]
    if unmet:
        return Eligibility(
            task_id, False,
            f"dependencies not completed: {', '.join(unmet)}",
            plan["plan_version"], True, task["status"] == "in-progress",
        )
    return Eligibility(
        task_id, True, "eligible", plan["plan_version"], True,
        task["status"] == "in-progress",
    )


def _active_run_for_task(runs: list[dict], task_id: str) -> dict | None:
    for run in runs:
        if run["task_id"] == task_id and run["status"] in ACTIVE_RUN_STATUSES:
            return run
    return None


def _next_run_id(runs: list[dict]) -> str:
    highest = 0
    for run in runs:
        run_id = run.get("run_id", "")
        if isinstance(run_id, str) and run_id.startswith("RUN-"):
            try:
                highest = max(highest, int(run_id[4:]))
            except ValueError:
                continue
    return f"RUN-{highest + 1:04d}"


def _new_run_record(
    *, run_id: str, task_id: str, plan_version: int, adapter_id: str,
    status: str, owner: str | None, previous_run_id: str | None,
) -> dict:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "plan_version": plan_version,
        "adapter_id": adapter_id,
        "status": status,
        "owner": owner,
        "previous_run_id": previous_run_id,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "cancelled_at": None,
        "exit_code": None,
        "outcome": None,
        "diagnostics_note": None,
        "handoff_ref": None,
        "local_log_ref": None,
        "output_truncated": None,
        "provider_result": None,
        "pid": None,
        "machine": machine_token() if status == "running" else None,
    }


# --- dispatch ----------------------------------------------------------------------


def dispatch_task(
    aperio_dir: Path,
    task_id: str,
    *,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    authorize_workspace_write: bool = False,
    authorize_write: bool | None = None,
    previous_run_id: str | None = None,
    actor: str | None = None,
    dispatch_lock_timeout: float = 5.0,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict:
    """Dispatch one task through the full protocol.

    Acquires ``dispatch.lock`` once for the whole dispatch (short bounded
    wait: concurrent dispatch fails fast — same-workspace execution is
    sequential by design). Protocol: config validation → eligibility
    against a fresh plan read → run record created → task claimed through
    the Feature-03 lifecycle API → adapter executes → record finalized. A
    claim lost to a competing writer finalizes the record as failed;
    nothing is left dangling.

    Subprocess-capable adapters WITHOUT ``--authorize-exec`` are a
    read-only preview: no run record is created, the task is never claimed,
    and no process is started — a delayed preparation must not block a
    later authorized execution.
    """
    adapter = get_adapter(adapter_id)
    require_supported_state(aperio_dir)
    if not isinstance(authorize_workspace_write, bool):
        raise ValidationError(
            "authorize_workspace_write must be true or false"
        )
    if authorize_write is not None:
        if not isinstance(authorize_write, bool):
            raise ValidationError(
                "authorize_write must be true or false when given"
            )
        authorize_workspace_write = (
            authorize_workspace_write or authorize_write
        )
    policy_role = None
    if load_policy(aperio_dir) is not None:
        policy_role = require_action(
            aperio_dir, actor, "execution.dispatch", target=f"task:{task_id}"
        )

    # Config validation happens BEFORE any persistent mutation: an
    # invalid config must never leave an orphaned task claim or a
    # partial record. The same boundary enforces the separate write
    # consent and verifies that the installed CLI advertises every
    # provider control aperio will rely on.
    validated_config = adapter.validate_config(config, aperio_dir)
    available, reason = adapter.available(validated_config)
    if not available:
        raise StateError(f"adapter {adapter_id!r} is not available: {reason}")
    if authorize_exec:
        authorization_error = adapter.authorization_error(
            validated_config,
            authorize_workspace_write=authorize_workspace_write,
        )
        if authorization_error:
            raise StateError(authorization_error)
        adapter.check_capabilities(validated_config)

    dispatch_token = acquire_lock(
        dispatch_lock_path(aperio_dir),
        timeout=dispatch_lock_timeout,
        artifact="dispatch",
    )
    try:
        result = _dispatch_locked(
            aperio_dir, task_id, adapter=adapter, adapter_id=adapter_id,
            config=validated_config, authorize_exec=authorize_exec,
            authorize_workspace_write=authorize_workspace_write,
            previous_run_id=previous_run_id, actor=actor,
            lock_timeout=lock_timeout,
        )
        if result.get("status") == "preview":
            return result
        record = _dispatch_execute(
            aperio_dir, result, adapter=adapter, adapter_id=adapter_id,
            config=validated_config, actor=actor,
            authorize_workspace_write=authorize_workspace_write,
        )
        if policy_role is not None:
            _policy_event(
                aperio_dir,
                actor=actor,
                role=policy_role,
                action="execution.dispatch",
                target=f"task:{task_id}",
                revision=record.get("plan_version"),
                outcome=str(record.get("status", "")),
                note=f"run {record.get('run_id')}",
            )
        return record
    finally:
        release_lock(dispatch_lock_path(aperio_dir), dispatch_token)


def _dispatch_locked(
    aperio_dir: Path,
    task_id: str,
    *,
    adapter,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    authorize_workspace_write: bool,
    previous_run_id: str | None,
    actor: str | None = None,
    lock_timeout: float,
) -> dict:
    """Run the eligibility + record-creation steps.

    The dispatch lock is already held; the executions lock is acquired
    alone. For local-process without authorization this returns a
    read-only preview dict (``status: "preview"``) — no record, no
    claim. Otherwise it creates the record and returns the dispatch
    result for the execution step.
    """
    executions_token = acquire_lock(
        executions_lock_path(aperio_dir),
        timeout=lock_timeout,
        artifact="execution records",
    )
    try:
        path = executions_path(aperio_dir)
        raw = _read_snapshot_bytes(path)
        if raw is None:
            document = {"schema_version": EXECUTIONS_SCHEMA_VERSION, "runs": []}
        else:
            document = parse_executions_snapshot(raw, path)
        runs = document["runs"]
        plan = read_plan(aperio_dir)  # lockless read; full validation
        active = _active_run_for_task(runs, task_id)
        eligibility = check_eligibility(plan, task_id, active)
        if not eligibility.eligible:
            raise StateError(
                f"task {task_id} is not eligible for execution: "
                f"{eligibility.reason}. Nothing was modified."
            )
        already_claimed = eligibility.already_claimed
        if already_claimed:
            task = next(t for t in plan["tasks"] if t["id"] == task_id)
            owner = task["owner"] or ""
            if not owner.startswith(f"{adapter_id}:"):
                raise StateError(
                    f"task {task_id} is in-progress and owned by "
                    f"{owner!r}; a different adapter cannot retry it. "
                    "Return it to pending first (task-status blocked, "
                    "then pending), or dispatch with the owning adapter."
                )
        run_id = _next_run_id(runs)
        claim_owner = f"{adapter_id}:{run_id}"

        # Real subprocess adapters without authorization: entirely
        # read-only preview — no record, no claim, no process.
        if getattr(adapter, "requires_authorization", False) and not authorize_exec:
            assert config is not None  # validated before the lock
            return {
                "status": "preview",
                "task_id": task_id,
                "adapter_id": adapter_id,
                "plan_version": plan["plan_version"],
                "already_claimed": already_claimed,
                "note": (
                    "read-only preparation: no run record was created "
                    "and no process was started; re-run with "
                    "--authorize-exec to execute"
                ),
                "command": adapter.command_preview(
                    config, aperio_dir,
                    task=next(t for t in plan["tasks"] if t["id"] == task_id),
                    workspace_write_authorized=authorize_workspace_write,
                ),
            }
        record = _new_run_record(
            run_id=run_id,
            task_id=task_id,
            plan_version=plan["plan_version"],
            adapter_id=adapter_id,
            status="prepared",
            owner=claim_owner,
            previous_run_id=previous_run_id,
        )
        runs.append(record)
        # CAS semantics: snapshot is None only when the file did not
        # exist (creation path); otherwise the compare protects against
        # concurrent writers between the read and the publish.
        _publish_executions(aperio_dir, document, raw)
    finally:
        release_lock(executions_lock_path(aperio_dir), executions_token)
    return {
        "run_id": run_id,
        "claim_owner": claim_owner,
        "already_claimed": already_claimed,
        "adapter_id": adapter_id,
        "task_id": task_id,
        "config": config,
    }


def _task_state_or_none(
    aperio_dir: Path, task_id: str
) -> tuple[str, str | None] | None:
    """Return ``(status, owner)`` for one task, or None when unreadable."""
    try:
        plan = read_plan(aperio_dir)
    except (StateError, NotInitializedError):
        return None
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task["status"], task["owner"]
    return None


def _run_status_or_none(aperio_dir: Path, run_id: str) -> str | None:
    """Read one run's status without deciding anything (None if unknown)."""
    try:
        document = read_executions(aperio_dir)
    except AperioError:
        return None
    if document is None:
        return None
    for run in document["runs"]:
        if run["run_id"] == run_id:
            return run["status"]
    return None


def _release_startup_claim(
    aperio_dir: Path, task_id: str, claim_owner: str, run_id: str
) -> tuple[str, str | None]:
    """Return the claim to pending ONLY when ownership and state verify.

    Returns ``(state, owner)`` where state is ``released``, ``not_ours``
    (the task moved on / belongs to someone else — left untouched), or
    ``unverifiable`` (plan unreadable or the lifecycle API refused — left
    untouched and reported as an orphaned claim).
    """
    state = _task_state_or_none(aperio_dir, task_id)
    if state is None:
        return "unverifiable", None
    status, owner = state
    if status != "in-progress" or owner != claim_owner:
        return "not_ours", owner
    try:
        release_failed_claim(
            aperio_dir, task_id, run_id=run_id, claim_owner=claim_owner
        )
    except AperioError:
        return "unverifiable", owner
    return "released", owner


def _claim_recovery_hint(task_id: str) -> str:
    return (
        "recover explicitly with 'aperio task-status <project> "
        f"{task_id} --status blocked --reason \"dispatch failed\"' and "
        "then '--status pending'"
    )


def _finalize_startup_failure(
    aperio_dir: Path,
    *,
    run_id: str,
    task_id: str,
    claim_owner: str,
    already_claimed: bool,
    error: AperioError,
) -> str:
    """Finalize a run whose adapter never started, then explain the claim.

    The record is finalized as ``failed`` with neutral, non-secret text
    (never the raw exception, argv, or config). A claim is released back
    to ``pending`` only when this run's ownership and the in-progress
    state are positively verified; otherwise it is left untouched and
    marked ``claim_orphaned`` so reconciliation reports it. A run already
    cancelled/finalized by a competing writer is never overwritten.
    """
    status = _run_status_or_none(aperio_dir, run_id)
    claim_state = "unverifiable"
    claim_owner_seen: str | None = None
    if status == "prepared":
        if already_claimed:
            claim_state = "previous"
            previous = _task_state_or_none(aperio_dir, task_id)
            claim_owner_seen = previous[1] if previous is not None else None
        else:
            claim_state, claim_owner_seen = _release_startup_claim(
                aperio_dir, task_id, claim_owner, run_id
            )
    elif status in ("cancelled", "failed", "interrupted", "completed"):
        claim_state = "run_finalized"

    record = _finalize_run(
        aperio_dir,
        run_id,
        updates={
            "status": "failed",
            "outcome": (
                "execution error: the adapter did not start "
                f"({type(error).__name__}); nothing was executed"
            ),
            "diagnostics_note": (
                "startup failed before any result was recorded; re-run the "
                "command to see the actionable error (not stored in the "
                "shareable record)"
            ),
            "claim_released": claim_state == "released",
            "claim_orphaned": claim_state == "unverifiable",
            "finished_at": utc_now_iso(),
        },
        status="failed",
    )
    if record.get("status") == "cancelled":
        return (
            f"Run {run_id} was already cancelled; the cancelled status and "
            "the task claim were left untouched for explicit resolution."
        )
    if claim_state == "released":
        return (
            "The task claim was verified as this run's and returned to "
            "pending; nothing was executed."
        )
    if claim_state == "previous":
        return (
            f"The task remains claimed by {claim_owner_seen!r} from a "
            "previous run; it was left untouched. Resolve it explicitly "
            f"({_claim_recovery_hint(task_id)}) if the earlier attempt is "
            "abandoned."
        )
    if claim_state == "not_ours":
        return (
            f"The task is now owned by {claim_owner_seen!r}, not by this "
            "run; it was left untouched."
        )
    if claim_state == "run_finalized":
        return (
            "The run had already been finalized by a competing writer; "
            "its status and the task claim were left untouched for "
            "explicit resolution."
        )
    return (
        "The task claim could not be verified or released automatically; "
        "it is recorded as an orphaned claim and reconciliation reports it "
        f"while the claim survives. {_claim_recovery_hint(task_id)}."
    )


def _with_note(exc: AperioError, note: str) -> AperioError:
    """Re-raise an actionable Aperio error of the same class plus a note."""
    base = str(exc).rstrip()
    if base and base[-1] not in ".!?":
        base += "."  # keep the two sentences readable on one line
    try:
        return type(exc)(f"{base} {note}")
    except Exception:  # pragma: no cover - all AperioError types take one arg
        return exc


def _dispatch_execute(
    aperio_dir: Path, dispatch_result: dict, *, adapter, adapter_id: str,
    config: dict | None = None, actor: str | None = None,
    authorize_workspace_write: bool = False,
) -> dict:
    """Claim, execute, and finalize after the record-creation step."""
    run_id = dispatch_result["run_id"]
    already_claimed = dispatch_result["already_claimed"]
    task_id = dispatch_result["task_id"]
    claim_owner = dispatch_result["claim_owner"]

    # Claim through the Feature-03 lifecycle API (never nested inside the
    # executions lock). A claim lost to a competing writer finalizes the
    # record as failed — never dangling.
    if not already_claimed:
        try:
            update_task(aperio_dir, task_id, new_status="in-progress",
                        owner=claim_owner, actor=actor)
        except AperioError as exc:
            _finalize_run(
                aperio_dir, run_id,
                updates={
                    "status": "failed",
                    "outcome": (
                        "claim lost: the task could not be claimed (a "
                        "competing writer may have claimed or changed it)"
                    ),
                    "diagnostics_note": (
                        "task claim rejected "
                        f"({type(exc).__name__}); the actionable error was "
                        "reported to the operator and is not stored in the "
                        "shareable record"
                    ),
                    "finished_at": utc_now_iso(),
                },
                status="failed",
            )
            raise StateError(
                f"task {task_id} could not be claimed for execution: {exc}. "
                "The run was recorded as failed; no execution happened."
            ) from None

    # Execution. The pid is captured via the on_running callback so the
    # running status + pid + machine token are recorded atomically.
    pid_holder: dict = {}

    def on_running(pid: int) -> None:
        pid_holder["pid"] = pid
        _flip_to_running(aperio_dir, run_id, pid)

    if adapter_id == MANUAL_ADAPTER_ID:
        # Shared dispatch error boundary: a manual handoff that cannot be
        # written must never leave the run 'prepared' with a live claim.
        try:
            task = _task_from_plan(aperio_dir, task_id)
            result = adapter.start(
                run_id=run_id, aperio_dir=aperio_dir,
                task=task, config=None,
            )
        except AperioError as exc:
            note = _finalize_startup_failure(
                aperio_dir, run_id=run_id, task_id=task_id,
                claim_owner=claim_owner, already_claimed=already_claimed,
                error=exc,
            )
            raise _with_note(exc, note) from None
        return _finalize_run(
            aperio_dir, run_id,
            updates={
                "status": "awaiting-external",
                "outcome": result.get("outcome"),
                "handoff_ref": result.get("handoff_ref"),
                "finished_at": None,
            },
            status="awaiting-external",
        )

    try:
        result = adapter.start(
            run_id=run_id, aperio_dir=aperio_dir,
            task=_task_from_plan(aperio_dir, task_id), config=config,
            on_running=on_running,
            workspace_write_authorized=authorize_workspace_write,
        )
    except KeyboardInterrupt:
        if "pid" in pid_holder:
            _terminate_process(pid_holder["pid"])
        _finalize_run(
            aperio_dir, run_id,
            updates={
                "status": "interrupted",
                "outcome": "interrupted by operator interrupt (Ctrl-C); "
                           "the child process was signalled",
                "finished_at": utc_now_iso(),
            },
            status="interrupted",
        )
        raise
    except AperioError as exc:
        if "pid" in pid_holder:
            _terminate_process(pid_holder["pid"])
        if "pid" not in pid_holder:
            # The child process was never created: no work happened, so
            # the same startup boundary as the manual adapter applies.
            note = _finalize_startup_failure(
                aperio_dir, run_id=run_id, task_id=task_id,
                claim_owner=claim_owner, already_claimed=already_claimed,
                error=exc,
            )
            raise _with_note(exc, note) from None
        record = _finalize_run(
            aperio_dir, run_id,
            updates={
                "status": "failed",
                "outcome": (
                    "execution error: the adapter failed after the child "
                    f"was created ({type(exc).__name__}); the actionable "
                    "error was reported to the operator"
                ),
                "diagnostics_note": (
                    "execution failed before any result was recorded; "
                    "re-run the command to see the specific error"
                ),
                "finished_at": utc_now_iso(),
            },
            status="failed",
        )
        if record.get("status") == "cancelled":
            note = (
                f"Run {run_id} was cancelled during startup; the cancelled "
                "status and the task claim were left untouched for "
                "explicit resolution."
            )
        else:
            note = (
                "The task remains claimed by this run; retry with the same "
                f"adapter or {_claim_recovery_hint(task_id)}."
            )
        raise _with_note(exc, note) from None

    status = (
        "completed"
        if not result.get("timed_out") and result.get("exit_code") == 0
        else "failed"
    )
    return _finalize_run(
        aperio_dir, run_id,
        updates={
            "pid": result.get("pid"),
            "machine": machine_token(),
            "outcome": result.get("outcome"),
            "exit_code": result.get("exit_code"),
            "output_truncated": result.get("output_truncated"),
            "provider_result": result.get("provider_result"),
            "local_log_ref": result.get("local_log_ref"),
            "finished_at": utc_now_iso(),
        },
        status=status,
    )


def _flip_to_running(aperio_dir: Path, run_id: str, pid: int) -> None:
    """CAS prepared → running, recording pid + machine in the same write.

    Refuses (StateError) if the run was cancelled during startup — the
    adapter contract then kills and reaps the child.
    """
    token = acquire_lock(
        executions_lock_path(aperio_dir), timeout=5.0,
        artifact="execution records",
    )
    try:
        path = executions_path(aperio_dir)
        snapshot = _read_snapshot_bytes(path)
        document = (
            parse_executions_snapshot(snapshot, path)
            if snapshot is not None
            else None
        )
        if document is None:
            raise StateError(
                f"execution records vanished at {path}; cannot start run "
                f"{run_id}"
            )
        for run in document["runs"]:
            if run["run_id"] != run_id:
                continue
            if run["status"] == "cancelled":
                raise StateError(
                    f"run {run_id} was cancelled during startup; the "
                    "child process has been terminated"
                )
            if run["status"] != "prepared":
                raise StateError(
                    f"run {run_id} is {run['status']!r}, not prepared; "
                    "cannot flip to running"
                )
            run["status"] = "running"
            run["pid"] = pid
            run["machine"] = machine_token()
            _publish_executions(aperio_dir, document, snapshot)
            return
        raise StateError(f"run {run_id} vanished from {path}")
    finally:
        release_lock(executions_lock_path(aperio_dir), token)


def update_phase_claim(aperio_dir: Path, task_id: str, owner: str) -> dict:
    """Claim a pending task through the Feature-03 lifecycle API."""
    return update_task(aperio_dir, task_id, new_status="in-progress", owner=owner)


def _task_from_plan(aperio_dir: Path, task_id: str) -> dict:
    plan = read_plan(aperio_dir)
    return next(t for t in plan["tasks"] if t["id"] == task_id)


def _finalize_run(
    aperio_dir: Path, run_id: str, *, updates: dict, status: str | None = None
) -> dict:
    """Finalize (or annotate) one run under executions.lock + byte-CAS.

    Precedence rule: a run already marked ``cancelled`` is never
    resurrected — the finalize outcome is appended as a note instead.
    A run already finalized (completed/failed/interrupted) is never
    overwritten — history is immutable once final.
    """
    token = acquire_lock(
        executions_lock_path(aperio_dir), timeout=5.0,
        artifact="execution records",
    )
    try:
        path = executions_path(aperio_dir)
        snapshot = _read_snapshot_bytes(path)
        document = (
            parse_executions_snapshot(snapshot, path)
            if snapshot is not None
            else None
        )
        if document is None:
            raise StateError(
                f"execution records vanished at {path}; cannot finalize run "
                f"{run_id}"
            )
        for run in document["runs"]:
            if run["run_id"] != run_id:
                continue
            if run["status"] == "cancelled":
                run["outcome_note"] = (
                    f"finalize after cancellation ignored: "
                    f"{updates.get('outcome')!r}"
                )
                _publish_executions(aperio_dir, document, snapshot)
                return run
            if status is not None:
                run["status"] = status
            elif run["status"] in ("completed", "failed", "interrupted"):
                return run  # already finalized; never overwrite history
            for key, value in updates.items():
                run[key] = value
            _publish_executions(aperio_dir, document, snapshot)
            return run
        raise StateError(
            f"run {run_id} vanished from {path}; cannot finalize it"
        )
    finally:
        release_lock(executions_lock_path(aperio_dir), token)


def _get_run(aperio_dir: Path, run_id: str) -> dict:
    document = read_executions(aperio_dir)
    if document is None:
        raise StateError(
            f"no execution records at {executions_path(aperio_dir)}"
        )
    for run in document["runs"]:
        if run["run_id"] == run_id:
            return run
    raise StateError(f"no run {run_id} in {executions_path(aperio_dir)}")


def runs_dispatch(
    aperio_dir: Path,
    *,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    authorize_workspace_write: bool = False,
    authorize_write: bool | None = None,
    limit: int | None = None,
    actor: str | None = None,
    dispatch_lock_timeout: float = 5.0,
) -> list[dict]:
    """Deterministic scheduler: dispatch eligible tasks in id order.

    Acquires ``dispatch.lock`` ONCE for the whole run and dispatches each
    task through the lock-free internal path; concurrent dispatch fails
    fast (sequential same-workspace execution by design — non-overlapping
    scopes cannot be enforced, so parallelism is not offered in 0.4.0).
    Eligibility is re-evaluated per task inside the loop.

    Without ``authorize_exec`` a subprocess-capable adapter returns one
    read-only preview dict per eligible task (``status: "preview"``): no
    records, no task claims, and no processes are created, so a later
    authorized dispatch works normally.
    """
    require_supported_state(aperio_dir)
    adapter = get_adapter(adapter_id)
    if not isinstance(authorize_workspace_write, bool):
        raise ValidationError(
            "authorize_workspace_write must be true or false"
        )
    if authorize_write is not None:
        if not isinstance(authorize_write, bool):
            raise ValidationError(
                "authorize_write must be true or false when given"
            )
        authorize_workspace_write = (
            authorize_workspace_write or authorize_write
        )
    validated_config = adapter.validate_config(config, aperio_dir)
    available, _reason = adapter.available(validated_config)
    if not available:
        raise StateError(f"adapter {adapter_id!r} is not available: {_reason}")
    if authorize_exec:
        authorization_error = adapter.authorization_error(
            validated_config,
            authorize_workspace_write=authorize_workspace_write,
        )
        if authorization_error:
            raise StateError(authorization_error)
        adapter.check_capabilities(validated_config)

    dispatch_token = acquire_lock(
        dispatch_lock_path(aperio_dir),
        timeout=dispatch_lock_timeout,
        artifact="dispatch",
    )
    dispatched: list[dict] = []
    try:
        while True:
            if limit is not None and len(dispatched) >= limit:
                break
            plan = read_plan(aperio_dir)
            executions = read_executions(aperio_dir)
            runs = executions["runs"] if executions is not None else []
            eligible = _eligible_task_ids(plan, runs)
            remaining = [
                task_id for task_id in eligible
                if task_id not in {r["task_id"] for r in dispatched}
            ]
            if not remaining:
                break
            task_id = remaining[0]
            policy_role = None
            if load_policy(aperio_dir) is not None:
                policy_role = require_action(
                    aperio_dir,
                    actor,
                    "execution.dispatch",
                    target=f"task:{task_id}",
                )
            record = _dispatch_locked(
                aperio_dir, task_id, adapter=adapter, adapter_id=adapter_id,
                config=validated_config,
                authorize_exec=authorize_exec,
                authorize_workspace_write=authorize_workspace_write,
                previous_run_id=None,
                actor=actor,
                lock_timeout=LOCK_TIMEOUT_SECONDS,
            )
            if record.get("status") == "preview":
                # Unauthorized local-process scheduling is a read-only
                # preview: no record, no claim, no process. Never hand
                # preview data to the execution step.
                dispatched.append(record)
                continue
            executed = _dispatch_execute(
                aperio_dir, record, adapter=adapter,
                adapter_id=adapter_id, config=validated_config,
                actor=actor,
                authorize_workspace_write=authorize_workspace_write,
            )
            if policy_role is not None:
                _policy_event(
                    aperio_dir,
                    actor=actor,
                    role=policy_role,
                    action="execution.dispatch",
                    target=f"task:{task_id}",
                    revision=executed.get("plan_version"),
                    outcome=str(executed.get("status", "")),
                    note=f"run {executed.get('run_id')}",
                )
            dispatched.append(executed)
    finally:
        release_lock(dispatch_lock_path(aperio_dir), dispatch_token)
    return dispatched


def _eligible_task_ids(plan: dict, runs: list[dict]) -> list[str]:
    eligible = []
    for task in sorted(plan["tasks"], key=lambda t: t["id"]):
        active = _active_run_for_task(runs, task["id"])
        if check_eligibility(plan, task["id"], active).eligible:
            eligible.append(task["id"])
    return eligible


# --- cancel / report / reconcile -----------------------------------------------------


def cancel_run(aperio_dir: Path, run_id: str, reason: str | None = None) -> dict:
    """Cancel a run. Never steals or signals a foreign-machine pid; never
    resurrects a finalized run. Cancelling does not change the task's
    Feature-03 status — resolving the claim is a deliberate task-status
    operation."""
    if not isinstance(run_id, str) or not run_id.startswith("RUN-"):
        raise ValidationError("run-cancel: run id must look like RUN-NNNN")
    token = acquire_lock(
        executions_lock_path(aperio_dir), timeout=5.0,
        artifact="execution records",
    )
    try:
        path = executions_path(aperio_dir)
        snapshot = _read_snapshot_bytes(path)
        document = (
            parse_executions_snapshot(snapshot, path)
            if snapshot is not None
            else None
        )
        if document is None:
            raise StateError(
                f"no execution records at {path}; nothing to cancel"
            )
        for run in document["runs"]:
            if run["run_id"] != run_id:
                continue
            if run["status"] in ("completed", "failed", "cancelled", "interrupted"):
                raise StateError(
                    f"run {run_id} is already finalized "
                    f"({run['status']!r}); nothing to cancel"
                )
            pid = run.get("pid")
            machine = run.get("machine")
            cancelled_process = False
            if run["status"] == "running" and isinstance(pid, int):
                # A recorded pid is only a claim. Signal it only when the
                # record is genuinely local: an imported record never gets
                # this far (portable archives may not carry pid/machine),
                # and a forged/absurd pid is refused rather than trusted.
                if machine != machine_token():
                    raise StateError(
                        f"run {run_id} was recorded on another machine "
                        f"({machine!r}); its process cannot be signalled "
                        "from here. Resolve it on that machine, then "
                        "re-run reconciliation."
                    )
                if isinstance(pid, bool) or pid <= 1 or pid == os.getpid():
                    raise StateError(
                        f"run {run_id} records pid {pid!r}, which does not "
                        "identify a process aperio may signal from here. "
                        "Refusing to signal it; the run was left unchanged. "
                        "Verify the record and resolve it on the machine "
                        "that ran it."
                    )
                cancelled_process = _terminate_process(pid)
            run["status"] = "cancelled"
            run["cancelled_at"] = utc_now_iso()
            run["outcome"] = "cancelled by operator" + (
                f": {reason}" if reason else ""
            ) + (" (process signalled)" if cancelled_process else "")
            _publish_executions(aperio_dir, document, snapshot)
            return run
        raise StateError(f"no run {run_id} in {path}")
    finally:
        release_lock(executions_lock_path(aperio_dir), token)


def _terminate_process(pid: int) -> bool:
    """POSIX SIGTERM → grace → SIGKILL. Returns True if a signal was
    delivered to a live process. Windows: TerminateProcess (hard kill,
    no grace — documented)."""
    import signal
    import time

    delivered = False

    def signal_process(*, force: bool) -> None:
        nonlocal delivered
        if force:
            sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        else:
            sig = signal.SIGTERM
        if os.name == "posix":
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                    delivered = True
                    return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.kill(pid, sig)
            delivered = True
        except (ProcessLookupError, PermissionError):
            pass

    try:
        signal_process(force=False)
        if not delivered:
            return False
    except (ProcessLookupError, PermissionError):
        return False
    for _ in range(50):  # 5 s grace for POSIX
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return delivered
        time.sleep(0.1)
    signal_process(force=True)
    return delivered


def record_external_result(
    aperio_dir: Path, run_id: str, result: dict
) -> dict:
    """Consume an external agent's result for an awaiting-external run.

    ``result`` requires ``outcome_reported`` (non-empty string) and
    ``success_reported`` (boolean); optional ``diagnostics``. Reporting
    is attribution only — it NEVER completes the task or substitutes for
    Feature-03 verification.

    Privacy boundary: both ``outcome_reported`` and ``diagnostics`` are
    untrusted, potentially secret-bearing external text. They are written
    to one machine-local file under ``.aperio/local/runs/`` (created
    exclusively, never appended to or truncated) and the shareable record
    receives only neutral, bounded status and attribution metadata: a
    fixed outcome phrase, the success boolean, lengths, and a local-file
    note. No raw external text, absolute path, or private identifier can
    reach ``executions.json`` or portable memory.
    """
    if not isinstance(result, dict):
        raise ValidationError("run-report: result must be a JSON object")
    unknown = sorted(
        set(result) - {"outcome_reported", "success_reported", "diagnostics"}
    )
    if unknown:
        raise ValidationError(
            "run-report: unknown field(s): "
            f"{', '.join(unknown)} (allowed: outcome_reported, "
            "success_reported, diagnostics)"
        )
    outcome = result.get("outcome_reported")
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValidationError(
            "run-report: field 'outcome_reported' must be a non-empty string"
        )
    success = result.get("success_reported")
    if not isinstance(success, bool):
        raise ValidationError(
            "run-report: field 'success_reported' must be a boolean"
        )
    diagnostics = result.get("diagnostics")
    if diagnostics is not None and not isinstance(diagnostics, str):
        raise ValidationError(
            "run-report: field 'diagnostics' must be a string when given"
        )
    outcome_text = outcome.strip()
    diagnostics_text = (diagnostics or "").strip()

    require_supported_state(aperio_dir)
    token = acquire_lock(
        executions_lock_path(aperio_dir), timeout=5.0,
        artifact="execution records",
    )
    try:
        path = executions_path(aperio_dir)
        snapshot = _read_snapshot_bytes(path)
        document = (
            parse_executions_snapshot(snapshot, path)
            if snapshot is not None
            else None
        )
        if document is None:
            raise StateError(
                f"no execution records at {path}; nothing to report on"
            )
        for run in document["runs"]:
            if run["run_id"] != run_id:
                continue
            if run["status"] != "awaiting-external":
                raise StateError(
                    f"run {run_id} is {run['status']!r}, not "
                    "'awaiting-external'; external results can only be "
                    "recorded on manual handoff runs"
                )
            # Untrusted external text: machine-local, exclusive creation.
            # A raw external report is machine-local evidence and never
            # enters a shareable record or portable memory.
            report_dir = check_artifact_dir(aperio_dir, "local", "runs")
            report_path = report_dir / f"{run_id}{EXTERNAL_REPORT_FILE_SUFFIX}"
            report_body = (
                "[run-report]\n"
                f"run: {run_id}\n"
                f"task: {run['task_id']}\n"
                f"adapter: {run['adapter_id']}\n"
                f"success_reported: {'true' if success else 'false'}\n"
                "\n## outcome_reported\n"
                f"{outcome_text}\n"
            )
            if diagnostics_text:
                report_body += f"\n## diagnostics\n{diagnostics_text}\n"
            create_exclusive_text(
                report_path, report_body, what="external run report"
            )
            finished_at = utc_now_iso()
            run["status"] = "completed" if success else "failed"
            run["outcome"] = "external agent reported " + (
                "success" if success else "failure"
            )
            run["external_reported"] = {
                "success": success,
                "attributed_to": "external-agent",
                "reported_at": finished_at,
                "outcome_chars": len(outcome_text),
                "diagnostics_chars": len(diagnostics_text),
                "raw_stored_locally": True,
            }
            run["diagnostics_note"] = (
                "external diagnostics stored machine-locally "
                f"({len(diagnostics_text)} chars)"
                if diagnostics_text
                else None
            )
            run["finished_at"] = finished_at
            _publish_executions(aperio_dir, document, snapshot)
            return run
        raise StateError(f"no run {run_id} in {path}")
    finally:
        release_lock(executions_lock_path(aperio_dir), token)


def read_run_handoff(aperio_dir: Path, run_id: str) -> str:
    """Return the prepared run-handoff document for a manual run.

    The reference is read from the record's current ``handoff_ref`` schema
    (``runs/<run-id>.handoff.md``) and resolved only inside the project's
    ``.aperio/runs/`` directory: traversal, absolute paths, malformed
    references, symlinked directories or leaves, and links resolving to
    outside files are all refused with an actionable error.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ValidationError("run-handoff: run id must look like RUN-NNNN")
    require_supported_state(aperio_dir)
    run = _get_run(aperio_dir, run_id)
    ref = run.get("handoff_ref")
    if not ref:
        raise StateError(
            f"run {run_id} has no prepared run-handoff document (only "
            "manual runs prepare one)"
        )
    expected = f"runs/{run_id}{RUN_HANDOFF_SUFFIX}"
    if ref != expected:
        raise StateError(
            f"run {run_id} records an unexpected run-handoff reference "
            f"{ref!r}; this aperio resolves only the current handoff_ref "
            f"schema ({expected!r}). Nothing was read; inspect the "
            "execution records manually."
        )
    return read_confined_artifact(
        runs_dir(aperio_dir),
        ref,
        dir_name="runs",
        suffix=RUN_HANDOFF_SUFFIX,
        what="run handoff document",
    )


def reconcile_runs(aperio_dir: Path) -> dict:
    """Detect incomplete runs without inventing success or deleting
    evidence. Only status corrections that cannot invent success
    (interrupted, cancelled-stale) are written; pid interpretation is
    refused for records from other machines; an awaiting-external run
    whose task was completed out-of-band is flagged (suggested for
    cancellation), never auto-cancelled.

    Orphaned claims are reported (never silently resolved): a ``prepared``
    run whose task is not claimed, and a finalized-failed run whose
    startup claim could not be verified/released (``claim_orphaned``).
    """
    require_supported_state(aperio_dir)
    document = read_executions(aperio_dir)
    report = {
        "interrupted": [],
        "cancelled_stale": [],
        "awaiting_external_stale": [],
        "orphaned_tasks": [],
        "left_running": [],
        "foreign_machine": [],
        "plan_version_drift": [],
        "no_records": document is None,
    }
    if document is None:
        return report
    try:
        current_plan_version: int | None = read_plan(aperio_dir)["plan_version"]
    except (StateError, NotInitializedError):
        current_plan_version = None  # plan removed: recorded, not invented

    for run in document["runs"]:
        if run["status"] == "running":
            machine = run.get("machine")
            pid = run.get("pid")
            if machine != machine_token():
                report["foreign_machine"].append(
                    {"run_id": run["run_id"], "recorded_machine": machine}
                )
                continue
            alive = _pid_alive(pid) if isinstance(pid, int) else False
            if alive:
                report["left_running"].append(
                    {"run_id": run["run_id"], "reason": "process still alive"}
                )
            else:
                _finalize_run(
                    aperio_dir, run["run_id"],
                    updates={
                        "status": "interrupted",
                        "outcome": (
                            "interrupted by reconciliation: the recorded "
                            "process is no longer alive; no success was "
                            "invented and no evidence was deleted"
                        ),
                        "finished_at": utc_now_iso(),
                    },
                    status="interrupted",
                )
                report["interrupted"].append(run["run_id"])
        elif run["status"] == "prepared":
            task_status = _task_status_or_none(aperio_dir, run["task_id"])
            if task_status == "in-progress":
                # Crash between record creation and the running flip: the
                # claim is real, the run never started.
                _finalize_run(
                    aperio_dir, run["run_id"],
                    updates={
                        "status": "cancelled",
                        "outcome": (
                            "cancelled by reconciliation: the task was "
                            "claimed but the run never started (crash "
                            "between record creation and execution)"
                        ),
                        "cancelled_at": utc_now_iso(),
                    },
                    status="cancelled",
                )
                report["cancelled_stale"].append(run["run_id"])
                report["orphaned_tasks"].append(run["task_id"])
            elif task_status in (None, "pending", "blocked", "completed"):
                # The run never started and no claim is held by it: safe
                # to cancel the stale record so it cannot block dispatch.
                _finalize_run(
                    aperio_dir, run["run_id"],
                    updates={
                        "status": "cancelled",
                        "outcome": (
                            "cancelled by reconciliation: the run never "
                            "started and no task claim is held by it"
                        ),
                        "cancelled_at": utc_now_iso(),
                    },
                    status="cancelled",
                )
                report["cancelled_stale"].append(run["run_id"])
        elif run["status"] == "failed" and run.get("claim_orphaned") is True:
            task_status = _task_status_or_none(aperio_dir, run["task_id"])
            if (
                task_status == "in-progress"
                and run["task_id"] not in report["orphaned_tasks"]
            ):
                report["orphaned_tasks"].append(run["task_id"])
        elif run["status"] == "awaiting-external":
            task_status = _task_status_or_none(aperio_dir, run["task_id"])
            if task_status == "completed":
                report["awaiting_external_stale"].append(
                    {"run_id": run["run_id"], "task_id": run["task_id"]}
                )
        if (
            isinstance(run.get("plan_version"), int)
            and current_plan_version is not None
            and run["plan_version"] != current_plan_version
            and run["status"] in ACTIVE_RUN_STATUSES
        ):
            report["plan_version_drift"].append(
                {"run_id": run["run_id"],
                 "recorded_plan_version": run["plan_version"],
                 "current_plan_version": current_plan_version}
            )
    report["recovery_required"] = bool(
        report["interrupted"] or report["orphaned_tasks"]
    )
    return report


def _pid_alive(pid: int) -> bool:
    import signal

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _task_status_or_none(aperio_dir: Path, task_id: str) -> str | None:
    try:
        plan = read_plan(aperio_dir)
    except (StateError, NotInitializedError):
        return None
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task["status"]
    return None


def execution_summary(aperio_dir: Path) -> dict | None:
    """Concise execution summary for the portable memory API (null when
    no execution records exist; future exec-schema degrades gracefully)."""
    path = executions_path(aperio_dir)
    if os.path.islink(path) and path.exists():
        raise PathError(
            f"refusing to follow symlink at {path} while reading "
            "execution records; remove it and re-run 'aperio init' to repair"
        )
    if not path.exists():
        return None
    raw = _read_snapshot_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify "
            "it. Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: "
            f"{exc.msg}); aperio will not overwrite it."
        ) from None
    if not isinstance(data, dict):
        raise StateError(
            f"{path} is corrupt: execution records must be a JSON object"
        )
    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version > EXECUTIONS_SCHEMA_VERSION
    ):
        return {
            "present": True,
            "unsupported_schema": True,
            "schema_version": schema_version,
        }
    document = validate_executions_document(data, path)
    active_runs = [
        {
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "adapter_id": run["adapter_id"],
            "status": run["status"],
        }
        for run in document["runs"]
        if run["status"] in ACTIVE_RUN_STATUSES
    ]
    latest = document["runs"][-1] if document["runs"] else None
    recovery_required = any(
        run["status"] == "interrupted" for run in document["runs"]
    )
    return {
        "present": True,
        "unsupported_schema": False,
        "schema_version": EXECUTIONS_SCHEMA_VERSION,
        "total_runs": len(document["runs"]),
        "active_runs": active_runs,
        "latest_outcome": (
            {
                "run_id": latest["run_id"],
                "task_id": latest["task_id"],
                "status": latest["status"],
                "outcome": latest.get("outcome"),
                "finished_at": latest.get("finished_at"),
            }
            if latest is not None
            else None
        ),
        "recovery_required": recovery_required,
    }


def execution_preview(
    aperio_dir: Path, task_id: str, *, adapter_id: str, config: dict | None,
    authorize_workspace_write: bool = False,
) -> dict:
    """Deterministic, side-effect-free execution preview (JSON-safe)."""
    adapter = get_adapter(adapter_id)
    require_supported_state(aperio_dir)
    plan = read_plan(aperio_dir)
    executions = read_executions(aperio_dir)
    runs = executions["runs"] if executions is not None else []
    active = _active_run_for_task(runs, task_id)
    eligibility = check_eligibility(plan, task_id, active)
    preview = {
        "task_id": task_id,
        "adapter_id": adapter_id,
        "eligible": eligibility.eligible,
        "reason": eligibility.reason,
        "plan_version": eligibility.plan_version,
        "plan_approved": eligibility.plan_approved,
        "already_claimed": eligibility.already_claimed,
    }
    if eligibility.eligible:
        task = next(t for t in plan["tasks"] if t["id"] == task_id)
        available, reason = adapter.available(config)
        preview["adapter_available"] = available
        if not available:
            preview["availability_warning"] = reason
        preview.update(
            adapter.preview(
                task=task, config=config, aperio_dir=aperio_dir,
                authorize_workspace_write=authorize_workspace_write,
            )
        )
    return preview
