"""Durable task continuity: checkpoints and recovery (checkpoint-schema v1).

A checkpoint is one compare-and-swap document (``.aperio/checkpoint.json``)
**derived from durable state** — the plan, the execution records, the
intake log, the decision log — plus a small amount of interpreted
guidance. It is written at meaningful transitions (task status changes,
run reports, captures, resolves, phase changes) and on explicit request
(``aperio checkpoint-write``), not only when a session ends gracefully.

What a checkpoint is:

* a durable snapshot of *where the work stands*: active task identity,
  approved objective and scope, completed/pending substeps, which
  completions carry recorded verification evidence and which do not,
  pending brain-dump items, and the plan/execution versions it was
  derived from;
* the input to recovery: ``aperio recover`` validates the checkpoint,
  compares it against the **current** durable state, reports drift and
  stale assumptions, separates verified completion from unverified agent
  claims, and states resume eligibility with reasons.

What a checkpoint is not:

* never a claim that unverified agent statements are completions —
  verification evidence comes only from the plan lifecycle (recorded
  evidence, limitations, and approved reviews where governance is on);
* never written for work that was never durably recorded — recovery
  cannot resurrect what no artifact holds, and no document here claims
  otherwise;
* not a lock on resuming: eligibility is advice with reasons; the plan
  lifecycle (approval, evidence, reviews) remains the actual gate.

Writes are serialized by a lock and published through a byte
compare-and-swap, so two writers cannot silently overwrite each other;
a stale writer conflicts and re-derives. The document is plain JSON
inside `.aperio/`, portable by the ordinary project archive, and
validated on every read.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import PathError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_MAX_BYTES = 256 * 1024

_REASON_MAX_BYTES = 500


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _validate_checkpoint(data: object) -> dict:
    """Validate a checkpoint document (checkpoint-schema v1)."""

    if not isinstance(data, dict):
        raise ValidationError(
            "checkpoint: the document must be a JSON object"
        )
    version = data.get("checkpoint_schema_version")
    if version != CHECKPOINT_SCHEMA_VERSION:
        if isinstance(version, int):
            raise StateError(
                f"checkpoint: unsupported checkpoint_schema_version "
                f"{version}; this aperio supports version "
                f"{CHECKPOINT_SCHEMA_VERSION}"
            )
        raise ValidationError(
            "checkpoint: 'checkpoint_schema_version' must be an integer"
        )
    required = (
        "revision", "written_at", "reason", "project_state",
        "tasks_summary", "inbox", "next_recommended_action",
        "resume_eligibility", "derived_from",
    )
    missing = [key for key in required if key not in data]
    if missing:
        raise ValidationError(
            "checkpoint: missing required fields: " + ", ".join(missing)
        )
    if isinstance(data["revision"], bool) or not isinstance(
        data["revision"], int
    ) or data["revision"] < 1:
        raise ValidationError(
            "checkpoint: 'revision' must be a positive integer"
        )
    from aperio.planmodel import _ISO_UTC_Z_RE
    from datetime import datetime as _dt

    if not isinstance(data["written_at"], str) or not _ISO_UTC_Z_RE.match(
        data["written_at"]
    ):
        raise ValidationError(
            "checkpoint: 'written_at' must be ISO-8601 UTC (…Z)"
        )
    try:
        _dt.strptime(data["written_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValidationError(
            "checkpoint: 'written_at' is not a calendar-valid UTC timestamp"
        ) from None
    for key in ("project_state", "tasks_summary", "inbox",
                "resume_eligibility", "derived_from"):
        if not isinstance(data[key], dict):
            raise ValidationError(f"checkpoint: '{key}' must be an object")
    if not isinstance(data["next_recommended_action"], str):
        raise ValidationError(
            "checkpoint: 'next_recommended_action' must be a string"
        )
    return data


class CheckpointStore:
    """The project's checkpoint document under a lock with byte CAS."""

    def __init__(self, aperio_dir: Path):
        self.aperio_dir = Path(aperio_dir)
        self.path = self.aperio_dir / "checkpoint.json"
        self.lock_path = self.aperio_dir / "checkpoint.lock"

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict:
        """Read and validate the checkpoint; refuse symlinks and damage."""

        if os.path.islink(self.path):
            raise PathError(
                f"refusing to follow symlink at {self.path} for the "
                "checkpoint"
            )
        if not self.path.is_file():
            raise StateError(
                f"no checkpoint at {self.path}; write one with "
                "'aperio checkpoint-write'"
            )
        raw = self.path.read_bytes()
        if len(raw) > CHECKPOINT_MAX_BYTES:
            raise StateError(
                f"checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(
                f"checkpoint at {self.path} is damaged ({exc}); it is "
                "never repaired automatically — re-derive it with "
                "'aperio checkpoint-write --reason recovery'"
            ) from None
        return _validate_checkpoint(data)

    def write_derived(self, content: dict, *, reason: str) -> dict:
        """Derive-then-publish: re-read durable state, bump the revision,
        and publish through the byte CAS. Idempotent re-derivation."""

        reason = _check_reason(reason)
        token = acquire_lock(self.lock_path, artifact="checkpoint")
        try:
            current = None
            if self.path.exists():
                if os.path.islink(self.path):
                    raise PathError(
                        f"refusing to follow symlink at {self.path} for "
                        "the checkpoint"
                    )
                raw = self.path.read_bytes()
                if raw:
                    try:
                        current = _validate_checkpoint(
                            json.loads(raw.decode("utf-8"))
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise StateError(
                            "the existing checkpoint is damaged; repair or "
                            "remove .aperio/checkpoint.json explicitly "
                            "before writing a new one"
                        ) from None
            revision = (current["revision"] + 1) if current else 1
            document = {
                "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                "revision": revision,
                "written_at": _utc_now(),
                "reason": reason,
                **content,
            }
            bookkeeping = ("checkpoint_schema_version", "revision",
                           "written_at")
            if current is not None:
                same_content = {
                    k: v for k, v in document.items() if k not in bookkeeping
                } == {
                    k: v for k, v in current.items()
                    if k not in bookkeeping
                }
                if same_content:
                    # the derived state is unchanged: keep the existing
                    # revision and timestamp instead of churning the file
                    return current
            tmp_path = self.path.with_name("checkpoint.json.tmp")
            if os.path.islink(tmp_path) or tmp_path.exists():
                raise StateError(
                    f"{tmp_path.name} is occupied; resolve it explicitly "
                    "(aperio never deletes files it did not create)"
                )
            fd = os.open(tmp_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                payload = json.dumps(document, ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n"
                data = payload.encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, self.path)
            return document
        finally:
            release_lock(self.lock_path, token)


def _check_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError(
            "checkpoint: --reason must be non-empty text"
        )
    if "\x00" in reason or len(reason.encode("utf-8")) > _REASON_MAX_BYTES:
        raise ValidationError(
            f"checkpoint: --reason must be at most {_REASON_MAX_BYTES} "
            "UTF-8 bytes without NUL"
        )
    return " ".join(reason.split())


# ---------------------------------------------------------------------------
# derivation from durable state
# ---------------------------------------------------------------------------


def derive_checkpoint(aperio_dir: Path) -> dict:
    """Derive the checkpoint content from durable state. Read-only.

    Every field comes from an artifact: the plan (objective, scope,
    tasks, verification evidence), execution records (active runs), the
    intake log (pending brain-dump items), and the decision log (only
    via its file presence — the log itself remains the audit trail).
    Agent statements are never completion evidence; where the only
    signal is a claim, it is listed under ``unverified_signals``.
    """

    from aperio.intake import IntakeLog, _active_task
    from aperio.planstore import read_plan
    from aperio.runtime import read_executions

    plan = None
    plan_error = None
    try:
        plan = read_plan(aperio_dir)
    except StateError as exc:
        plan_error = str(exc)

    tasks_summary = {
        "verified_completed": [],
        "completed_without_evidence": [],
        "in_progress": [],
        "pending": [],
        "blocked": [],
    }
    plan_state = {
        "present": bool(plan),
        "plan_version": plan.get("plan_version") if plan else None,
        "approval_status": (plan or {}).get("approval", {}).get("status"),
        "approved_revision": (plan or {}).get("approval", {})
        .get("approved_revision"),
        "objective": (plan or {}).get("objective"),
    }
    unverified_signals: list[str] = []
    if plan:
        for task in plan.get("tasks", []):
            status = task.get("status")
            entry = {"task_id": task["id"], "title": task.get("title")}
            if status == "completed":
                verification = task.get("verification") or {}
                if verification.get("evidence"):
                    tasks_summary["verified_completed"].append({
                        **entry,
                        "evidence": verification.get("evidence"),
                        "limitations": verification.get("limitations"),
                        "result": verification.get("result"),
                    })
                else:
                    # legacy/ungoverned completion without recorded
                    # evidence is reported, never promoted
                    tasks_summary["completed_without_evidence"].append(entry)
                    unverified_signals.append(
                        f"{task['id']} is completed without recorded "
                        "verification evidence"
                    )
            elif status == "in-progress":
                tasks_summary["in_progress"].append({
                    **entry, "owner": task.get("owner"),
                })
            elif status == "blocked":
                tasks_summary["blocked"].append({
                    **entry, "reason": task.get("blocked_reason"),
                })
            else:
                tasks_summary["pending"].append(entry)

    exec_state = None
    try:
        exec_state = read_executions(aperio_dir)
    except StateError:
        exec_state = None
    active_runs = []
    if exec_state:
        for run in exec_state.get("runs", []):
            if run.get("status") in ("running", "awaiting-external"):
                active_runs.append({
                    "run_id": run.get("run_id"),
                    "task_id": run.get("task_id"),
                    "status": run.get("status"),
                })

    inbox_state = IntakeLog(aperio_dir).read()
    pending_items = inbox_state["counts"]["pending_items"]
    urgent_pending = sum(
        1
        for capture_event in inbox_state["captures"]
        if capture_event.get("operator_urgent")
        and any(
            (capture_event["capture_id"], i)
            not in {
                (r["capture_id"], r["item_index"])
                for r in inbox_state["resolves"]
            }
            for i in range(len(capture_event.get("items", [])))
        )
    )

    active = _active_task(aperio_dir)

    # next recommended action: derived, conservative, never invented
    if plan_error:
        next_action = (
            "resolve the damaged or unsupported plan state before "
            "resuming; recovery refuses to interpret it"
        )
    elif active_runs:
        next_action = (
            f"reconcile the active run(s) "
            f"({', '.join(r['run_id'] or '?' for r in active_runs)}) "
            "with 'aperio runs-reconcile' before resuming work"
        )
    elif urgent_pending:
        next_action = (
            "address the operator-urgent capture before continuing any "
            "task"
        )
    elif tasks_summary["in_progress"]:
        task = tasks_summary["in_progress"][0]
        next_action = (
            f"continue {task['task_id']} ({task['title']}); its claim and "
            "verification obligations are unchanged"
        )
    elif tasks_summary["pending"] and plan_state["approval_status"] == \
            "approved":
        next_action = (
            f"dispatch {tasks_summary['pending'][0]['task_id']} through "
            "the approved plan's normal eligibility checks"
        )
    elif tasks_summary["pending"]:
        next_action = (
            "the plan has pending tasks but no current approval: review "
            "and approve the plan before executing anything"
        )
    else:
        next_action = "no active work: capture or plan the next task"

    # resume eligibility: reasons, not permission
    reasons: list[str] = []
    eligible = True
    if plan_error:
        eligible = False
        reasons.append("plan state is damaged or unsupported")
    if active_runs:
        eligible = False
        reasons.append("an execution run is active; reconcile first")
    if plan and plan.get("approval", {}).get("status") != "approved":
        eligible = False
        reasons.append(
            "the current plan revision is not approved (a revision resets "
            "approval by design)"
        )
    if pending_items and not urgent_pending:
        reasons.append(
            f"{pending_items} brain-dump item(s) are pending review; "
            "resuming is allowed but review them first"
        )
    if tasks_summary["completed_without_evidence"]:
        reasons.append(
            "some completions lack recorded verification evidence; they "
            "are listed as such and need review before being trusted"
        )
    if not plan:
        reasons.append("no plan exists; nothing to resume — capture or "
                       "plan work first")
        if not tasks_summary["in_progress"]:
            eligible = False

    content = {
        "project_state": {
            **plan_state,
            "phase_known": True,
            "damaged_plan": bool(plan_error),
        },
        "tasks_summary": tasks_summary,
        "active_task": active,
        "active_runs": active_runs,
        "unverified_signals": unverified_signals,
        "inbox": {
            "pending_items": pending_items,
            "urgent_pending": urgent_pending,
            "captures": inbox_state["counts"]["captures"],
        },
        "next_recommended_action": next_action,
        "resume_eligibility": {"eligible": eligible, "reasons": reasons},
        "derived_from": {
            "plan_version": plan_state["plan_version"],
            "executions_present": exec_state is not None,
            "intake_captures": inbox_state["counts"]["captures"],
        },
    }
    return content


def refresh_checkpoint(aperio_dir: Path, *, reason: str) -> dict:
    """Derive and publish a checkpoint (the write path used by hooks)."""

    store = CheckpointStore(aperio_dir)
    content = derive_checkpoint(aperio_dir)
    return store.write_derived(content, reason=reason)


def read_checkpoint(aperio_dir: Path) -> dict:
    return CheckpointStore(aperio_dir).read()


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


def recover_report(aperio_dir: Path) -> dict:
    """Recovery inspection: checkpoint vs current durable state.

    Steps (directive §3): load the last durable checkpoint, validate it,
    inspect the actual current state, detect drift and stale
    assumptions, separate verified completed work from unverified
    claims, state resume eligibility with reasons, and require review
    when anything is ambiguous. Read-only: recovery never mutates
    anything and never invents completion.
    """

    store = CheckpointStore(aperio_dir)
    if not store.exists():
        return {
            "format": "aperio-recovery-report",
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "status": "no-checkpoint",
            "guidance": (
                "no durable checkpoint exists; nothing was recorded to "
                "recover. Inspect the plan, executions, and intake log "
                "directly (aperio plan-show / runs-list / inbox), then "
                "write a checkpoint with 'aperio checkpoint-write' before "
                "starting work"
            ),
            "resume_eligibility": {"eligible": False,
                                   "reasons": ["no checkpoint"]},
        }
    checkpoint = store.read()
    current = derive_checkpoint(aperio_dir)

    drift: list[str] = []
    derived = checkpoint.get("derived_from", {})
    if derived.get("plan_version") != current["project_state"][
        "plan_version"
    ]:
        drift.append(
            f"plan_version moved {derived.get('plan_version')} → "
            f"{current['project_state']['plan_version']}; the checkpoint's "
            "task snapshot may be stale"
        )
    if derived.get("intake_captures") != current["inbox"]["captures"]:
        drift.append(
            f"intake captures moved {derived.get('intake_captures')} → "
            f"{current['inbox']['captures']}; new brain-dump items are "
            "not in the checkpoint"
        )
    checkpoint_active = checkpoint.get("active_task")
    current_active = current.get("active_task")
    if (checkpoint_active or current_active) and (
        (checkpoint_active or {}).get("task_id")
        != (current_active or {}).get("task_id")
    ):
        drift.append(
            "the active task changed since the checkpoint "
            f"({(checkpoint_active or {}).get('task_id')} → "
            f"{(current_active or {}).get('task_id')})"
        )

    stale = bool(drift)
    verified = current["tasks_summary"]["verified_completed"]
    without_evidence = current["tasks_summary"]["completed_without_evidence"]

    review_required = stale or bool(without_evidence) or bool(
        current["unverified_signals"]
    )
    reasons = list(current["resume_eligibility"]["reasons"])
    if stale:
        reasons.append(
            "the checkpoint is stale relative to current durable state; "
            "re-derive it with 'aperio checkpoint-write --reason recovery' "
            "after reviewing the drift"
        )
    if review_required:
        reasons.append(
            "recovery is ambiguous in places (drift or unevidenced "
            "completions): review before resuming"
        )
    eligible = current["resume_eligibility"]["eligible"] and not stale

    return {
        "format": "aperio-recovery-report",
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "status": "stale" if stale else "current",
        "checkpoint_revision": checkpoint["revision"],
        "checkpoint_written_at": checkpoint["written_at"],
        "checkpoint_reason": checkpoint.get("reason"),
        "drift": drift,
        "verified_completed": verified,
        "completed_without_evidence": without_evidence,
        "unverified_signals": current["unverified_signals"],
        "active_task": current_active,
        "active_runs": current["active_runs"],
        "inbox": current["inbox"],
        "next_recommended_action": current["next_recommended_action"],
        "resume_eligibility": {"eligible": eligible, "reasons": reasons},
        "limitations": [
            "recovery reads durable artifacts only: work that was never "
            "recorded cannot be recovered and is not claimed recoverable",
            "agent statements about completion are unverified signals, "
            "never evidence",
            "resume eligibility is advice with reasons; the plan "
            "lifecycle (approval, evidence, reviews) remains the gate",
        ],
    }


def render_recovery_markdown(report: dict) -> str:
    lines = ["# Recovery report", ""]
    if report["status"] == "no-checkpoint":
        lines.append(f"**{report['status']}** — {report['guidance']}")
        lines.append("")
        for limitation in report.get("limitations", []):
            lines.append(f"- *{limitation}*")
        lines.append("")
        return "\n".join(lines)
    lines.append(f"Checkpoint: revision {report['checkpoint_revision']}, "
                 f"written {report['checkpoint_written_at']} "
                 f"({report['checkpoint_reason']})")
    lines.append(f"Status: **{report['status']}**")
    lines.append("")
    if report["drift"]:
        lines.append("Drift since the checkpoint:")
        lines.append("")
        for entry in report["drift"]:
            lines.append(f"- {entry}")
        lines.append("")
    lines.append(
        f"Verified completed work: {len(report['verified_completed'])} "
        f"task(s) with recorded evidence"
    )
    if report["completed_without_evidence"]:
        lines.append(
            f"- completions WITHOUT recorded evidence (need review): "
            + ", ".join(t["task_id"]
                        for t in report["completed_without_evidence"])
        )
    if report["unverified_signals"]:
        lines.append("Unverified signals (not evidence):")
        for signal in report["unverified_signals"]:
            lines.append(f"- {signal}")
    lines.append("")
    lines.append(f"Next: {report['next_recommended_action']}")
    lines.append("")
    lines.append(
        f"Resume eligibility: **{'eligible' if report['resume_eligibility']['eligible'] else 'gated'}**"
    )
    for reason in report["resume_eligibility"]["reasons"]:
        lines.append(f"- {reason}")
    lines.append("")
    for limitation in report.get("limitations", []):
        lines.append(f"- *{limitation}*")
    lines.append("")
    return "\n".join(lines)
