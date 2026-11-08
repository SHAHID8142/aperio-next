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

    prepared (local-process, no authorization yet / dry-run)
    awaiting-external (manual handoff prepared)
    running (local process started; pid+machine recorded atomically)
    completed (adapter finished — NOT task verification)
    failed (nonzero exit, timeout, missing executable, claim lost, ...)
    cancelled (explicit run-cancel, or stale prepared record)
    interrupted (reconciliation: recorded process no longer alive)

Honest guarantees: the byte-CAS detects non-cooperating writes landing
before the compare; a write between the compare and the rename can still
win. Crash-consistency is best-effort (no fsync). Pids are only
interpreted on the machine that recorded them (a hostname token is
stored alongside); same-machine pid reuse is a documented residual
hazard borne by the explicit run-cancel action.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from aperio import __version__
from aperio.adapters import (
    LOCAL_PROCESS_ADAPTER_ID,
    MANUAL_ADAPTER_ID,
    get_adapter,
    runs_dir,
    sanitize_diagnostics,
)
from aperio.errors import (
    AperioError,
    NotInitializedError,
    PathError,
    StateError,
    ValidationError,
)
from aperio.locks import LOCK_TIMEOUT_SECONDS, acquire_lock, release_lock
from aperio.planstore import read_plan, update_task
from aperio.schema import utc_now_iso
from aperio.project import aperio_dir_for, require_supported_state
from aperio.progress import update_phase

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

EXECUTIONS_JSON_FIELDS = ("schema_version", "runs")


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
    return data


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
        payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
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

    Active runs and plan approval are checked by the caller against the
    same snapshot this plan came from; the dependency and status checks
    run here.
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


def eligibility_with_records(
    aperio_dir: Path, task_id: str, plan: dict, runs: list[dict]
) -> Eligibility:
    return check_eligibility(
        plan, task_id, _active_run_for_task(runs, task_id)
    )


# --- dispatch ----------------------------------------------------------------------


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
    record = {
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
        "diagnostics": None,
        "handoff_path": None,
        "pid": None,
        "machine": machine_token() if status == "running" else None,
    }
    return record




def dispatch_task(
    aperio_dir: Path,
    task_id: str,
    *,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    previous_run_id: str | None = None,
    dispatch_lock_timeout: float = 5.0,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict:
    """Dispatch one task through the full protocol.

    Holds ``dispatch.lock`` for the whole dispatch (short bounded wait:
    concurrent dispatch fails fast — same-workspace execution is
    sequential by design). Protocol: eligibility against a fresh plan
    read → run record created (prepared) → task claimed through the
    Feature-03 lifecycle API → adapter executes → record finalized. A
    claim lost to a competing writer finalizes the record as failed;
    nothing is left dangling.
    """
    if adapter_id not in (MANUAL_ADAPTER_ID, LOCAL_PROCESS_ADAPTER_ID):
        raise ValidationError(f"unknown agent adapter {adapter_id!r}")
    adapter = get_adapter(adapter_id)
    available, reason = adapter.available()
    if not available:
        raise StateError(f"adapter {adapter_id!r} is not available: {reason}")
    require_supported_state(aperio_dir)

    dispatch_token = acquire_lock(
        dispatch_lock_path(aperio_dir),
        timeout=dispatch_lock_timeout,
        artifact="dispatch",
    )
    try:
        return _dispatch_under_dispatch_lock(
            aperio_dir, task_id, adapter=adapter, adapter_id=adapter_id,
            config=config, authorize_exec=authorize_exec,
            previous_run_id=previous_run_id, lock_timeout=lock_timeout,
        )
    finally:
        release_lock(dispatch_lock_path(aperio_dir), dispatch_token)


def _dispatch_under_dispatch_lock(
    aperio_dir: Path,
    task_id: str,
    *,
    adapter,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    previous_run_id: str | None,
    lock_timeout: float,
) -> dict:
    executions_token = acquire_lock(
        executions_lock_path(aperio_dir),
        timeout=lock_timeout,
        artifact="execution records",
    )
    try:
        plan = read_plan(aperio_dir)  # lockless read; full validation
        runs = (
            read_executions(aperio_dir)["runs"]
            if read_executions(aperio_dir) is not None
            else []
        )
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
        record = _new_run_record(
            run_id=run_id,
            task_id=task_id,
            plan_version=plan["plan_version"],
            adapter_id=adapter_id,
            status="prepared",
            owner=task["owner"] if already_claimed else claim_owner,
            previous_run_id=previous_run_id,
        )
        document, snapshot = _snapshot_or_new(aperio_dir)
        document["runs"].append(record)
        _publish_executions(aperio_dir, document, snapshot)
    finally:
        release_lock(executions_lock_path(aperio_dir), executions_token)

    # Claim through the Feature-03 lifecycle API (never nested inside the
    # executions lock). A claim lost to a competing writer finalizes the
    # record as failed — never dangling.
    if not already_claimed:
        try:
            update_phase_claim(aperio_dir, task_id, claim_owner)
        except AperioError as exc:
            _finalize_run(
                aperio_dir, run_id,
                updates={
                    "status": "failed",
                    "outcome": (
                        "claim lost: the task could not be claimed (a "
                        "competing writer may have claimed or changed it)"
                    ),
                    "diagnostics": sanitize_diagnostics(str(exc)),
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

    if adapter_id == LOCAL_PROCESS_ADAPTER_ID and not authorize_exec:
        # Preparation mode: no process is ever started without the
        # explicit authorization flag.
        record = _finalize_run(
            aperio_dir, run_id,
            updates={
                "outcome": (
                    "preparation only: no --authorize-exec, so no process "
                    "was started; re-run with --authorize-exec to execute"
                ),
                "finished_at": utc_now_iso(),
            },
            status="prepared",
        )
        return record

    try:
        result = adapter.start(
            run_id=run_id,
            aperio_dir=aperio_dir,
            task=_task_from_plan(aperio_dir, task_id),
            config=config,
            on_running=on_running if adapter_id == LOCAL_PROCESS_ADAPTER_ID else None,
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
        _finalize_run(
            aperio_dir, run_id,
            updates={
                "status": "failed",
                "outcome": f"execution error: {exc}",
                "diagnostics": sanitize_diagnostics(str(exc)),
                "finished_at": utc_now_iso(),
            },
            status="failed",
        )
        raise

    if adapter_id == MANUAL_ADAPTER_ID:
        _finalize_run(
            aperio_dir, run_id,
            updates={
                "status": "awaiting-external",
                "outcome": result.get("outcome"),
                "handoff_path": result.get("handoff_path"),
                "finished_at": None,
            },
            status="awaiting-external",
        )
        return _get_run(aperio_dir, run_id)

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
            "diagnostics": result.get("diagnostics"),
            "output_truncated": result.get("output_truncated"),
            "local_log": result.get("local_log"),
            "finished_at": utc_now_iso(),
        },
        status=status,
    )


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


def update_phase_claim(aperio_dir: Path, task_id: str, owner: str) -> dict:
    """Claim a pending task through the Feature-03 lifecycle API."""
    return update_task(aperio_dir, task_id, new_status="in-progress", owner=owner)


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


def runs_dispatch(
    aperio_dir: Path,
    *,
    adapter_id: str,
    config: dict | None,
    authorize_exec: bool,
    limit: int | None = None,
    lock_timeout: float = 5.0,
) -> list[dict]:
    """Deterministic scheduler: dispatch eligible tasks in id order.

    Holds ``dispatch.lock`` for the whole run; concurrent dispatch fails
    fast (sequential same-workspace execution by design — non-overlapping
    scopes cannot be enforced, so parallelism is not offered in 0.4.0).
    Eligibility is re-evaluated per task inside the loop.
    """
    require_supported_state(aperio_dir)
    adapter = get_adapter(adapter_id)
    available, _reason = adapter.available()
    if not available:
        raise StateError(f"adapter {adapter_id!r} is not available: {_reason}")

    dispatch_token = acquire_lock(
        dispatch_lock_path(aperio_dir),
        timeout=lock_timeout,
        artifact="dispatch",
    )
    dispatched: list[dict] = []
    try:
        while True:
            if limit is not None and len(dispatched) >= limit:
                break
            plan = read_plan(aperio_dir)
            eligible = _eligible_task_ids(aperio_dir, plan)
            remaining = [
                task_id for task_id in eligible
                if task_id not in {r["task_id"] for r in dispatched}
            ]
            if not remaining:
                break
            task_id = remaining[0]
            dispatched.append(
                dispatch_task(
                    aperio_dir, task_id,
                    adapter_id=adapter_id, config=config,
                    authorize_exec=authorize_exec,
                    lock_timeout=lock_timeout,
                )
            )
    finally:
        release_lock(dispatch_lock_path(aperio_dir), dispatch_token)
    return dispatched


def _eligible_task_ids(aperio_dir: Path, plan: dict) -> list[str]:
    executions = read_executions(aperio_dir)
    runs = executions["runs"] if executions is not None else []
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
                if machine != machine_token():
                    raise StateError(
                        f"run {run_id} was recorded on another machine "
                        f"({machine!r}); its process cannot be signalled "
                        "from here. Resolve it on that machine, then "
                        "re-run reconciliation."
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
    try:
        os.kill(pid, signal.SIGTERM)
        delivered = True
    except (ProcessLookupError, PermissionError):
        return False
    for _ in range(50):  # 5 s grace for POSIX
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return delivered
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
        delivered = True
    except (ProcessLookupError, PermissionError):
        pass
    return delivered


def record_external_result(
    aperio_dir: Path, run_id: str, result: dict
) -> dict:
    """Consume an external agent's result for an awaiting-external run.

    ``result`` requires ``outcome_reported`` (non-empty string) and
    ``success_reported`` (boolean); optional ``diagnostics``. Reporting
    is attribution only — it NEVER completes the task or substitutes for
    Feature-03 verification.
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
            run["status"] = (
                "completed" if success else "failed"
            )
            run["outcome"] = (
                "external agent reported "
                + ("success" if success else "failure")
                + f": {outcome.strip()}"
            )
            run["diagnostics"] = (
                sanitize_diagnostics(diagnostics) if diagnostics else None
            )
            run["finished_at"] = utc_now_iso()
            _publish_executions(aperio_dir, document, snapshot)
            return run
        raise StateError(f"no run {run_id} in {path}")
    finally:
        release_lock(executions_lock_path(aperio_dir), token)


def reconcile_runs(aperio_dir: Path) -> dict:
    """Detect incomplete runs without inventing success or deleting
    evidence. Only status corrections that cannot invent success
    (interrupted, cancelled-stale) are written; pid interpretation is
    refused for records from other machines; an awaiting-external run
    whose task was completed out-of-band is flagged (suggested for
    cancellation), never auto-cancelled."""
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
            elif task_status is None:
                report["cancelled_stale"].append(run["run_id"])
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
    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise StateError(
            f"{path} is corrupt: execution records must be a JSON object"
        )
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


def read_executions_or_fail(aperio_dir: Path) -> dict:
    """Read executions or raise a clear 'no runs' StateError."""
    document = read_executions(aperio_dir)
    if document is None:
        raise StateError(
            f"no execution records at {executions_path(aperio_dir)} "
            "(no run has ever been dispatched)"
        )
    return document


def execution_preview(
    aperio_dir: Path, task_id: str, *, adapter_id: str, config: dict | None
) -> dict:
    """Deterministic, side-effect-free execution preview."""
    adapter = get_adapter(adapter_id)
    require_supported_state(aperio_dir)
    plan = read_plan(aperio_dir)
    executions = read_executions(aperio_dir)
    runs = executions["runs"] if executions is not None else []
    active = _active_run_for_task(runs, task_id)
    eligibility = check_eligibility(plan, task_id, active)
    preview: dict = {
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
        preview.update(adapter.preview(task=task, config=config, aperio_dir=aperio_dir))
    return preview
