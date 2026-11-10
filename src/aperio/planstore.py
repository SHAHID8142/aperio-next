"""Plan persistence and operations (Feature 03).

``.aperio/plan.json`` is created explicitly (``aperio plan-create``) — it
is not part of ``init`` and not one of the init-created context files
(``status`` deliberately does not report it; use ``memory``, ``plan-show``
or ``task-list``).

Every mutation of an EXISTING plan (revise, approve, task-status) follows
the same single-snapshot protocol under the advisory ``plan.lock`` (never
stolen; bounded wait — see :mod:`aperio.locks`):

1. boundary validation (genuine initialized project, safe paths);
2. one snapshot read of ``plan.json``;
3. validation and rule checks against that snapshot;
4. mutation of the parsed document (only aperio-owned fields; unknown
   keys found on disk are preserved verbatim);
5. exclusive temp file (``plan.json.tmp``, git-ignored, never auto-
   deleted — a leftover is a user-resolved conflict);
6. byte compare-and-swap against the snapshot, then atomic rename.

``create`` is the exception: it is create-only and needs no lock — its
existence guarantee comes from an exclusive temp file plus an atomic
hard-link publish (``os.link`` fails if a plan raced into existence).

Concurrency: concurrent aperio writers serialize on the lock and each
reads fresh, so they cannot silently overwrite each other. The byte-CAS
detects writes from tools that ignore aperio's coordination *before* the
compare; a non-cooperating write landing between the compare and the
rename can still win — the filesystem cannot provide absolute protection
there. Crash-consistency is best-effort (no fsync).

Error mapping: invalid input shape → ValidationError; rule and state
conflicts (approval gate, dependencies, evidence gate, stale ``revises``,
corrupt/unsupported files) → StateError. A missing ``plan.json`` is a
normal state (the plan is optional), reported as StateError with
``aperio plan-create`` guidance — not NotInitializedError.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path

from aperio import __version__
from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.locks import LOCK_TIMEOUT_SECONDS, acquire_lock, release_lock
from aperio.policy import (
    _check_actor_id,
    _check_text,
    append_event as _policy_event,
    load_policy,
    plan_author_events,
    require_action,
)
from aperio.planmodel import (
    PLAN_SCHEMA_VERSION,
    TASK_SYSTEM_KEYS,
    check_reopen_allowed,
    ready_task_ids,
    validate_plan_document,
    validate_plan_input,
    validate_transition,
)
from aperio.project import aperio_dir_for, require_supported_state
from aperio.schema import utc_now_iso

PLAN_FILE_NAME = "plan.json"
PLAN_TMP_NAME = "plan.json.tmp"
PLAN_LOCK_NAME = "plan.lock"


def plan_path(aperio_dir: Path) -> Path:
    """Path of the plan file inside an ``.aperio/`` directory."""
    return aperio_dir / PLAN_FILE_NAME


# --- snapshot read -------------------------------------------------------------


def _read_snapshot(path: Path) -> bytes:
    """Read the plan file, refusing symlinks (portable-read boundary)."""
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading the plan "
            "(it could point at local or private data); remove it and "
            "re-run 'aperio plan-create' to restore the plan"
        )
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise StateError(
            f"no plan at {path}; create one with 'aperio plan-create'"
        ) from None
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None


def _parse_snapshot(raw: bytes, path: Path) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file manually."
        ) from None
    return validate_plan_document(data, path)


def read_plan(aperio_dir: Path) -> dict:
    """Read and validate the plan document.

    :raises StateError: when the plan does not exist (with plan-create
        guidance), is corrupt, or declares an unsupported schema version.
    :raises PathError: when the plan file is a symlink.
    """
    require_supported_state(aperio_dir)
    return _parse_snapshot(_read_snapshot(plan_path(aperio_dir)), plan_path(aperio_dir))


def plan_exists(aperio_dir: Path) -> bool:
    """True when a readable plan file exists (symlinks are refused by the
    read path, so this is only a light existence probe)."""
    path = plan_path(aperio_dir)
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading the plan; "
            "remove it and re-run 'aperio plan-create' to restore the plan"
        )
    return path.exists()


# --- shared mutation tail -------------------------------------------------------


def _publish(aperio_dir: Path, data: dict, snapshot: bytes, path: Path, tmp: Path) -> None:
    """Validate, write to an exclusive temp file, byte-CAS, atomic rename."""
    validate_plan_document(data, path)
    try:
        # ensure_ascii=False keeps author-supplied text readable; strict
        # JSON only — NaN/Infinity in preserved extension fields would
        # round-trip into a file strict parsers reject, so refuse instead.
        payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError as exc:
        raise StateError(
            f"cannot serialize the plan at {path}: {exc}; the document "
            "contains non-standard JSON values (NaN/Infinity, likely in "
            "extension fields). Resolve them manually; nothing was modified."
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
            f"cannot prepare plan write at {tmp}: {exc.strerror or exc}; "
            "the temp name is occupied by a concurrent aperio write (retry "
            "shortly) or a leftover from a crashed write (removing it is "
            "your decision); no existing content was modified"
        ) from None

    try:
        current = path.read_bytes()
    except FileNotFoundError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"the plan at {path} disappeared during the write; nothing was "
            "modified. Re-run 'aperio plan-create' if appropriate."
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot read {path} before publishing: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    if current != snapshot:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"the plan at {path} changed during the write (a tool outside "
            "aperio's coordination may have written it); nothing was "
            "modified. Re-read the plan and retry."
        )

    try:
        os.replace(tmp, path)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish plan at {path}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover is a user-resolved conflict


def _check_single_line_payload(field: str, value: str | None) -> str | None:
    """Payload fields must respect the shared line model: single-line
    fields additionally reject embedded newlines."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"plan: field '{field}' must be a non-empty string when given")
    stripped = value.strip()
    check_no_foreign_line_separators("plan.json", field, stripped)
    if "\n" in stripped:
        raise ValidationError(f"plan: field '{field}' must be a single line")
    return stripped


def _check_payload_text(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"plan: field '{field}' must be a non-empty string when given")
    stripped = value.strip()
    check_no_foreign_line_separators("plan.json", field, stripped)
    return stripped


def _fresh_task(task: dict) -> dict:
    """A created task starts pending with no lifecycle history."""
    fresh = dict(task)
    fresh.update(
        {
            "status": "pending",
            "owner": None,
            "blocked_reason": None,
            "verification": {
                "evidence": None,
                "limitations": None,
                "result": None,
                "verified_at": None,
            },
        }
    )
    return fresh


def _new_document(plan_content: dict, *, plan_version: int, approval: dict,
                  created_at: str) -> dict:
    """Assemble a full plan document from validated content (all tasks
    fresh: pending, unowned, unverified)."""
    doc: dict = {"schema_version": PLAN_SCHEMA_VERSION}
    doc.update(plan_content)
    doc["tasks"] = [_fresh_task(task) for task in plan_content["tasks"]]
    doc["plan_version"] = plan_version
    doc["approval"] = approval
    doc["created_at"] = created_at
    doc["updated_at"] = utc_now_iso()
    doc["aperio_version"] = __version__
    return doc


def _author_record(snapshot: dict, revision: int) -> dict | None:
    authors = snapshot.get("authors")
    if not isinstance(authors, dict):
        return None
    record = authors.get(str(revision))
    return record if isinstance(record, dict) else None


def _author_entry(actor: str, role: str, method: str, **extra) -> dict:
    entry = {
        "actor": actor,
        "role": role,
        "recorded_at": utc_now_iso(),
        "method": method,
    }
    entry.update(extra)
    return entry


def _record_attribution_event(
    aperio_dir: Path,
    *,
    actor: str,
    role: str,
    action: str,
    revision: int,
    outcome: str,
    note: str | None = None,
) -> None:
    """Write an attribution event, failing with a recovery instruction.

    The plan document (written atomically first) is the durable source of
    authorship; the audit log is the append-only history that approval
    cross-checks. If this write fails the revision stays published and
    approval stays blocked until 'aperio plan-sync-authors' succeeds.
    """
    try:
        _policy_event(
            aperio_dir,
            actor=actor,
            role=role,
            action=action,
            target="plan",
            revision=revision,
            outcome=outcome,
            note=note,
        )
    except StateError as exc:
        raise StateError(
            f"plan revision {revision} was published with durable author "
            f"attribution, but its audit record could not be written ({exc}). "
            "Approval is blocked until the attribution history is repaired; "
            "run 'aperio plan-sync-authors <project>' after fixing the audit "
            "log. The plan itself was not rolled back."
        ) from None


def _require_approval_attribution(
    aperio_dir: Path, snapshot: dict, revision: int
) -> dict:
    """Fail-closed authorship check for a governed plan approval.

    Requires (1) a durable author record inside the atomically published
    plan and (2) exactly one matching attribution event in the audit log.
    Missing, unreadable, or conflicting evidence blocks approval; recovery
    is explicit (adoption for pre-governance plans, sync for a lost audit
    event). Never guesses an author.
    """
    record = _author_record(snapshot, revision)
    if record is None:
        raise StateError(
            f"plan revision {revision} has no durable author attribution, so "
            "it cannot be approved. If this plan predates governance, an "
            "authorized operator must adopt it explicitly with 'aperio "
            "plan-adopt <project> --author <id> --reason <why>'; authorship "
            "is never inferred. Nothing was modified."
        )
    events = plan_author_events(aperio_dir, revision)
    if not events:
        raise StateError(
            f"plan revision {revision} has durable author attribution "
            f"({record.get('actor')!r}) but no matching audit record; approval "
            "is blocked rather than trusting partial history. Run 'aperio "
            "plan-sync-authors <project>' to record the missing attribution, "
            "then retry. Nothing was modified."
        )
    authors = {event_actor for _action, event_actor in events}
    if len(authors) != 1:
        raise StateError(
            f"plan revision {revision} has conflicting author attribution in "
            f"the audit log ({', '.join(sorted(authors))}); approval is "
            "blocked. Inspect the audit history and restore a single "
            "attribution before retrying. Nothing was modified."
        )
    event_actor = events[0][1]
    if event_actor != record.get("actor"):
        raise StateError(
            f"plan revision {revision} attributes authorship to "
            f"{record.get('actor')!r} in the plan but {event_actor!r} in the "
            "audit log; approval is blocked on this conflict. Nothing was "
            "modified."
        )
    return record


def _empty_approval() -> dict:
    return {
        "status": "draft",
        "approved_revision": None,
        "approved_at": None,
        "approved_by": None,
    }


def _carry_lifecycle(
    old_tasks: list[dict],
    new_tasks: list[dict],
    approval_status: str = "approved",
) -> tuple[list[dict], list[str]]:
    """Carry system-managed lifecycle state from the old document.

    Surviving task ids keep status/owner/blocked_reason/verification
    verbatim **only while their contract is unchanged**. The contract is
    the task's material content: objective, deliverables, acceptance
    criteria, and dependencies (a title edit is non-semantic and may
    accompany carried state). A material change to a non-pending task is
    REFUSED — the previous verification evidence would otherwise appear
    valid for a different task contract. Resolution is deliberate:
    reopen the task (``task-status --status in-progress --reopen``,
    clearing the evidence) or assign a new task id.

    New ids start fresh (pending); removing a non-pending task is refused
    so verification evidence is never silently destroyed; and the merged
    document may not leave an in-progress or completed task with
    unsatisfied dependencies.
    """
    old_by_id = {task["id"]: task for task in old_tasks}
    new_ids = {task["id"] for task in new_tasks}
    material_keys = ("objective", "deliverables", "acceptance_criteria", "depends_on")
    merged: list[dict] = []
    for task in new_tasks:
        old = old_by_id.get(task["id"])
        if old is None:
            fresh = dict(task)
            fresh.update(
                {
                    "status": "pending",
                    "owner": None,
                    "blocked_reason": None,
                    "verification": {
                        "evidence": None,
                        "limitations": None,
                        "result": None,
                        "verified_at": None,
                    },
                }
            )
            merged.append(fresh)
        else:
            if old["status"] != "pending":
                material = [
                    key
                    for key in material_keys
                    if task[key] != old[key]
                ]
                if material:
                    raise StateError(
                        f"plan revision makes material changes to "
                        f"{old['status']} task {task['id']} "
                        f"(changed: {', '.join(material)}); its recorded "
                        "lifecycle state and verification evidence would "
                        "no longer match the new contract. Return the task "
                        "to 'pending' through explicit lifecycle "
                        "transitions first (reopen if completed, then "
                        "block and unblock), or assign a new task id — "
                        "either way the old verification evidence stays "
                        "recoverable in the plan file until you decide. "
                        + (
                            "Note: the plan is currently in draft; "
                            "re-approve it (aperio plan-approve) before "
                            "resuming execution tracking. "
                            if approval_status != "approved"
                            else "Note: each guided transition changes "
                            "plan_version — re-read the plan (aperio "
                            "plan-show --json) and declare the new version "
                            "when revising. "
                        )
                        + "Nothing was modified."
                    )
            # Merge onto the OLD stored task so unknown compatible
            # extension fields (and their nested values) survive the
            # revision untouched; the new input replaces only the known
            # content fields, and lifecycle fields stay Aperio-controlled.
            carried = dict(old)
            carried.update(task)
            for key in TASK_SYSTEM_KEYS:
                carried[key] = old[key]
            merged.append(carried)

    removed_non_pending = [
        task["id"]
        for task in old_tasks
        if task["id"] not in new_ids and task["status"] != "pending"
    ]
    if removed_non_pending:
        raise StateError(
            "plan revision would remove non-pending task(s): "
            f"{', '.join(sorted(removed_non_pending))}; their lifecycle "
            "state and verification evidence would be silently destroyed. "
            "Keep them in the revision, or resolve them first (a pending "
            "task may simply be dropped)."
        )

    # A revision must not leave an in-progress or completed task with
    # unsatisfied dependencies: execution was authorized on the old graph.
    for task in merged:
        if task["status"] in ("in-progress", "completed"):
            for dep in task["depends_on"]:
                dep_status = next(
                    t["status"] for t in merged if t["id"] == dep
                )
                if dep_status != "completed":
                    raise StateError(
                        f"plan revision would leave {task['status']} task "
                        f"{task['id']} with unsatisfied dependency {dep} "
                        f"(now {dep_status!r}). Resolve the dependency "
                        "chain in the revision or revert it; nothing was "
                        "modified."
                    )
    return merged, sorted(
        task["id"] for task in old_tasks if task["id"] not in new_ids
    )


# --- operations -----------------------------------------------------------------


def create_plan(
    aperio_dir: Path, content: dict, *, actor: str | None = None
) -> dict:
    """Create the plan (create-only). Every task starts pending with no
    lifecycle history; the plan starts at revision 1, status draft.

    :raises ValidationError: for invalid input content.
    :raises StateError: when a plan already exists (use plan-revise).
    """
    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "plan.create", target="plan", revision=None
    )
    path = plan_path(aperio_dir)
    tmp = aperio_dir / PLAN_TMP_NAME
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio plan-create' to repair"
        )
    if path.exists():
        raise StateError(
            f"a plan already exists at {path}; revise it with "
            "'aperio plan-revise' instead of creating a new one"
        )
    if os.path.islink(tmp):
        raise PathError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run 'aperio plan-create' to repair"
        )

    plan_content = validate_plan_input(content)
    now = utc_now_iso()
    document = _new_document(
        plan_content, plan_version=1, approval=_empty_approval(), created_at=now
    )
    if role is not None:
        document["authors"] = {
            "1": _author_entry(actor, role, "created")
        }
    validate_plan_document(document, path)

    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot prepare plan creation at {tmp}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    try:
        os.link(tmp, path)
    except FileExistsError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"a plan appeared at {path} during creation; use 'aperio "
            "plan-revise' on the existing plan. Nothing was modified."
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish plan at {path}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    try:
        os.unlink(tmp)
    except OSError:
        pass  # leftover is a user-resolved conflict
    if role is not None:
        _record_attribution_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="plan.create",
            revision=document["plan_version"],
            outcome="created",
        )
    return document


@contextmanager
def _reviews_guard(aperio_dir: Path):
    """Hold reviews.lock after plan.lock (never the reverse)."""
    from aperio.reviews import REVIEW_LOCK_NAME

    token = acquire_lock(
        aperio_dir / REVIEW_LOCK_NAME, timeout=5.0, artifact="review records"
    )
    try:
        yield
    finally:
        release_lock(aperio_dir / REVIEW_LOCK_NAME, token)


def _mutate_under_lock(
    aperio_dir: Path,
    artifact: str,
    mutate,
    lock_timeout: float,
    *,
    guard=None,
):
    """Run ``mutate(snapshot)`` -> (document, changed) under plan.lock.

    The snapshot (bytes) feeds both the rule checks and the final byte
    compare-and-swap — a single read, per the handoff pattern.
    """
    require_supported_state(aperio_dir)
    path = plan_path(aperio_dir)
    tmp = aperio_dir / PLAN_TMP_NAME
    lock_path = aperio_dir / PLAN_LOCK_NAME
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio plan-create' to repair"
        )
    if os.path.islink(tmp):
        raise PathError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run the command to repair"
        )
    token = acquire_lock(lock_path, timeout=lock_timeout, artifact=artifact)
    try:
        with (guard() if guard is not None else nullcontext()):
            snapshot = _read_snapshot(path)
            document = _parse_snapshot(snapshot, path)
            new_document, changed = mutate(document)
            if changed:
                new_document["updated_at"] = utc_now_iso()
                new_document["aperio_version"] = __version__
                _publish(aperio_dir, new_document, snapshot, path, tmp)
            return new_document, changed
    finally:
        release_lock(lock_path, token)


def revise_plan(
    aperio_dir: Path,
    content: dict,
    revises: int,
    *,
    actor: str | None = None,
) -> dict:
    """Replace the plan content with a new draft revision.

    ``revises`` must equal the current ``plan_version`` (stale → conflict).
    Lifecycle state (status/owner/blocked_reason/verification) is carried
    verbatim for surviving tasks; new tasks start pending; removing a
    non-pending task is refused. Every revision resets approval to draft:
    renewed (human) approval is required before execution tracking resumes.
    """
    if isinstance(revises, bool) or not isinstance(revises, int) or revises < 1:
        raise ValidationError(
            "plan-revise: --revises must be a positive integer "
            "(the plan_version you are revising; see 'aperio plan-show')"
        )
    plan_content = validate_plan_input(content)
    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "plan.revise", target="plan", revision=revises
    )

    def mutate(snapshot: dict):
        current_version = snapshot["plan_version"]
        if revises != current_version:
            raise StateError(
                f"plan moved on: current plan_version is {current_version}, "
                f"but the revision declares {revises} (a stale revision "
                "would silently revert task progress). Re-read the plan "
                "with 'aperio plan-show --json', re-apply your changes, "
                "and retry. Nothing was modified."
            )
        merged_tasks, _removed = _carry_lifecycle(
            snapshot["tasks"],
            plan_content["tasks"],
            approval_status=snapshot["approval"]["status"],
        )
        new_document = dict(snapshot)  # preserves unknown keys
        new_document.update(plan_content)
        new_document["tasks"] = merged_tasks
        new_document["plan_version"] = current_version + 1
        new_document["approval"] = _empty_approval()
        if role is not None:
            authors = dict(snapshot.get("authors") or {})
            authors[str(current_version + 1)] = _author_entry(
                actor, role, "revised"
            )
            new_document["authors"] = authors
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "plan revision", mutate, LOCK_TIMEOUT_SECONDS
    )
    if role is not None:
        _record_attribution_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="plan.revise",
            revision=document["plan_version"],
            outcome="revised",
        )
    return document


def approve_plan(
    aperio_dir: Path,
    revision: int | None = None,
    by: str | None = None,
    *,
    actor: str | None = None,
) -> dict:
    """Record plan approval.

    This is an operational marker recording that the approval decision was
    made — it is NOT cryptographic proof of human authorization; the
    operator is responsible for genuine review. Approval never bumps
    ``plan_version`` (it is a stamp, not a content change). Re-approving
    an already-approved plan is allowed and re-stamps the metadata.

    :param revision: approve a specific plan_version; the current one when
        omitted. A mismatch is a conflict (the plan moved on).
    """
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
    ):
        raise ValidationError(
            "plan-approve: --revision must be a positive integer"
        )
    approved_by = None
    if by is not None:
        if not by.strip():
            raise ValidationError("plan-approve: --by must be a non-empty string")
        approved_by = by.strip()
        check_no_foreign_line_separators("plan.json", "approved_by", approved_by)
        if "\n" in approved_by:
            raise ValidationError("plan-approve: --by must be a single line")

    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "plan.approve", target="plan", revision=revision
    )
    def mutate(snapshot: dict):
        current_version = snapshot["plan_version"]
        if revision is not None and revision != current_version:
            raise StateError(
                f"plan moved on: current plan_version is {current_version}, "
                f"but approval was requested for revision {revision}. "
                "Nothing was modified."
            )
        if role is not None:
            author_record = _require_approval_attribution(
                aperio_dir, snapshot, current_version
            )
            author = author_record["actor"]
            if author == actor:
                raise StateError(
                    f"separation of duties: actor {actor!r} authored plan "
                    f"revision {current_version} and cannot approve it. Have "
                    "an independent declared planner or reviewer approve this "
                    "revision. Nothing was modified."
                )
        new_document = dict(snapshot)
        new_document["approval"] = {
            "status": "approved",
            "approved_revision": current_version,
            "approved_at": utc_now_iso(),
            "approved_by": (actor if role is not None else approved_by),
        }
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "plan approval", mutate, LOCK_TIMEOUT_SECONDS
    )
    if role is not None:
        _policy_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="plan.approve",
            target="plan",
            revision=document["plan_version"],
            outcome="approved",
            note="author differs",
        )
    return document


def adopt_plan_author(
    aperio_dir: Path,
    revision: int,
    author: str,
    *,
    reason: str,
    actor: str | None = None,
) -> dict:
    """Explicitly adopt authorship of a plan revision created pre-governance.

    This is the only way to give an attribution-less revision an author.
    It refuses to overwrite an existing attribution (including one whose
    audit event was lost: repair that with 'plan-sync-authors'). The
    adoption is recorded durably in the plan and in the audit log with the
    adopting operator and reason.
    """
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValidationError("plan-adopt: --revision must be a positive integer")
    author = _check_actor_id(author, where="--author")
    reason_text = _check_text(reason, where="--reason", required=True) or ""
    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "plan.adopt", target="plan", revision=revision
    )
    if role is None:
        raise StateError(
            "plan adoption requires project governance; enable it with "
            "'aperio policy-init' first"
        )

    def mutate(snapshot: dict):
        current_version = snapshot["plan_version"]
        if revision != current_version:
            raise StateError(
                f"plan moved on: current plan_version is {current_version}, "
                f"so revision {revision} can no longer be adopted. Nothing "
                "was modified."
            )
        if _author_record(snapshot, revision) is not None:
            raise StateError(
                f"plan revision {revision} already has durable author "
                "attribution; adoption never overwrites history. If its "
                "audit record is missing, run 'aperio plan-sync-authors'."
            )
        authors = dict(snapshot.get("authors") or {})
        authors[str(revision)] = _author_entry(
            author, role, "adopted",
            adopted_by=actor, reason=reason_text,
        )
        new_document = dict(snapshot)
        new_document["authors"] = authors
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "plan attribution adoption", mutate, LOCK_TIMEOUT_SECONDS
    )
    _record_attribution_event(
        aperio_dir,
        actor=author,
        role=role,
        action="plan.adopt",
        revision=revision,
        outcome="adopted",
        note=f"adopted by {actor}: {reason_text}",
    )
    return document


def sync_plan_authors(aperio_dir: Path, *, actor: str | None = None) -> list[int]:
    """Repair missing attribution audit events from durable plan records.

    Only appends events that are missing; refuses to resolve conflicting
    actors and never edits existing history. Returns repaired revisions.
    """
    require_supported_state(aperio_dir)
    role = require_action(aperio_dir, actor, "plan.sync", target="plan")
    if role is None:
        raise StateError(
            "attribution recovery requires project governance; enable it "
            "with 'aperio policy-init' first"
        )
    plan = read_plan(aperio_dir)
    authors = plan.get("authors") or {}
    action_for = {
        "created": "plan.create",
        "revised": "plan.revise",
        "adopted": "plan.adopt",
    }
    repaired: list[int] = []
    for key in sorted(authors, key=int):
        revision = int(key)
        record = authors[key]
        events = plan_author_events(aperio_dir, revision)
        actors = {event_actor for _action, event_actor in events}
        if actors and actors != {record["actor"]}:
            raise StateError(
                f"cannot repair attribution for revision {revision}: the audit "
                f"log names {', '.join(sorted(actors))} but the plan names "
                f"{record['actor']!r}. Conflicting history is never "
                "overwritten; restore the correct records manually."
            )
        if actors:
            continue
        _record_attribution_event(
            aperio_dir,
            actor=record["actor"],
            role=record.get("role", role),
            action=action_for.get(record.get("method"), "plan.create"),
            revision=revision,
            outcome="recovered",
            note="recovered from durable plan attribution",
        )
        repaired.append(revision)
    return repaired


def release_failed_claim(
    aperio_dir: Path,
    task_id: str,
    *,
    run_id: str,
    claim_owner: str,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict:
    """Runtime-only recovery: return this run's failed startup claim to pending.

    Allowed only for a run that never started (``prepared``) and a task
    that is still ``in-progress`` under the exact owner string derived
    from that run. It cannot target a completed task, cannot change
    ownership, and never records verification. Every other condition is
    refused so delayed recovery cannot reverse a competing writer.
    """
    require_supported_state(aperio_dir)
    from aperio.runtime import read_executions  # local: avoid import cycle

    document = read_executions(aperio_dir)
    run = None
    if document is not None:
        run = next(
            (item for item in document["runs"] if item["run_id"] == run_id),
            None,
        )
    if run is None:
        raise StateError(
            f"cannot release a claim for run {run_id}: no such execution "
            "record. Nothing was modified."
        )
    if run.get("status") != "prepared":
        raise StateError(
            f"cannot release a claim for run {run_id}: its status is "
            f"{run.get('status')!r}, not 'prepared'. Only a run that failed "
            "before starting may release a claim. Nothing was modified."
        )
    if run.get("task_id") != task_id or run.get("owner") != claim_owner:
        raise StateError(
            f"cannot release a claim for run {run_id}: the execution record "
            "does not own this task claim. Nothing was modified."
        )
    policy = load_policy(aperio_dir)

    def mutate(snapshot: dict):
        task = next(
            (item for item in snapshot["tasks"] if item["id"] == task_id),
            None,
        )
        if task is None:
            raise StateError(
                f"cannot release a claim for task {task_id}: it is no longer "
                "part of the plan. Nothing was modified."
            )
        if task.get("status") != "in-progress" or task.get("owner") != claim_owner:
            raise StateError(
                f"cannot release a claim for task {task_id}: it is now "
                f"{task.get('status')!r} under owner {task.get('owner')!r}, "
                "not this run's in-progress claim. A competing change is "
                "never reversed. Nothing was modified."
            )
        updated = dict(task)
        updated["status"] = "pending"
        updated["owner"] = None
        updated["blocked_reason"] = None
        if policy is not None:
            updated["revision"] = int(task.get("revision", 0)) + 1
            updated["attempt"] = int(task.get("attempt", 0))
        new_document = dict(snapshot)
        new_document["tasks"] = [
            updated if item["id"] == task_id else item
            for item in snapshot["tasks"]
        ]
        new_document["plan_version"] = snapshot["plan_version"] + 1
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "startup claim recovery", mutate, lock_timeout
    )
    updated_task = next(
        task for task in document["tasks"] if task["id"] == task_id
    )
    if policy is not None:
        from aperio.policy import append_event

        append_event(
            aperio_dir,
            actor="system",
            role="implementer",
            action="task.recover",
            target=f"task:{task_id}",
            revision=document["plan_version"],
            outcome="pending",
            note=f"released failed startup claim for {run_id}",
        )
    return updated_task


def update_task(
    aperio_dir: Path,
    task_id: str,
    *,
    new_status: str,
    owner: str | None = None,
    reason: str | None = None,
    evidence: str | None = None,
    limitations: str | None = None,
    result: str | None = None,
    reopen: bool = False,
    actor: str | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict:
    """Perform one validated task-status transition (execution tracking).

    Requires an approved plan (the approval gate). Dependencies and the
    evidence gate are re-evaluated at operation time against the fresh
    in-lock snapshot. Reopening a completed task requires ``reopen=True``
    (an explicit affordance: the verification block is cleared, so record
    durable history with 'aperio decision-add' first). Returns the
    updated task dict.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValidationError("task-status: a task id is required")
    task_id = task_id.strip()
    require_supported_state(aperio_dir)
    policy = load_policy(aperio_dir)
    role = None
    if policy is not None:
        action = "task.claim" if new_status == "in-progress" else "task.status"
        role = require_action(
            aperio_dir, actor, action, target=f"task:{task_id}"
        )
    owner = _check_single_line_payload("owner", owner)
    reason = _check_payload_text("reason", reason)
    evidence = _check_payload_text("evidence", evidence)
    limitations = _check_payload_text("limitations", limitations)

    def mutate(snapshot: dict):
        approval = snapshot["approval"]
        if approval["status"] != "approved":
            raise StateError(
                "execution tracking requires an approved plan: the current "
                f"plan revision {snapshot['plan_version']} is "
                f"{approval['status']!r}. Have the plan approved with "
                "'aperio plan-approve' after genuine review. Nothing was "
                "modified."
            )

        task = validate_transition(
            snapshot,
            task_id,
            new_status,
            owner=owner,
            reason=reason,
            evidence=evidence,
            limitations=limitations,
            result=result,
            reopen=reopen,
        )

        if new_status == "in-progress" and task["status"] == "completed":
            # Explicit reopen: refuse while dependents rely on completion.
            check_reopen_allowed(snapshot, task_id)
        if policy is not None:
            if (
                new_status == "in-progress"
                and task["status"] != "pending"
                and owner is not None
                and owner.strip()
                and owner.strip() != actor
            ):
                raise StateError(
                    "ownership transfers are not implicit: a governed task "
                    "can only be claimed by its recorded implementer and no "
                    "audited reassignment workflow exists yet. Nothing was "
                    "modified."
                )
            responsible = task.get("implementer") or task.get("owner")
            if (
                responsible
                and new_status in ("in-progress", "completed")
                and responsible != actor
            ):
                raise StateError(
                    f"task {task_id} is implemented by {responsible!r}; "
                    f"declared actor {actor!r} cannot act on it. Claim or "
                    "assignment must be explicit (separation of duties). "
                    "Nothing was modified."
                )
            if new_status == "completed":
                from aperio.reviews import review_requirement

                review_requirement(
                    aperio_dir,
                    task_id,
                    snapshot["approval"].get("approved_revision"),
                    attempt=int(task.get("attempt", 0)),
                    task_revision=int(task.get("revision", 0)),
                    implemented_by=task.get("implementer") or task.get("owner"),
                    evidence=evidence,
                    limitations=limitations,
                )
        updated = dict(task)
        if policy is not None:
            base_attempt = int(task.get("attempt", 0))
            base_revision = int(task.get("revision", 0))
            updated["revision"] = base_revision + 1
            updated["attempt"] = base_attempt
            if new_status == "in-progress" and task["status"] in (
                "pending", "completed"
            ):
                updated["attempt"] = base_attempt + 1
        if new_status == "in-progress":
            updated["status"] = "in-progress"
            if policy is not None:
                updated["implementer"] = actor
            if task["status"] == "pending":
                # Fresh claim: the claiming agent takes ownership. A run
                # claim records the run's operational owner (which startup
                # recovery verifies) while ``implementer`` keeps the
                # declared actor attribution.
                updated["owner"] = (
                    (owner or "").strip()
                    or (actor if policy is not None else None)
                    or None
                )
            elif task["status"] == "completed":
                # Reopen: clear the verification block (durable history
                # belongs in the decision log); the new owner takes over.
                updated["owner"] = (owner or "").strip() or None
                updated["verification"] = {
                    "evidence": None,
                    "limitations": None,
                    "result": None,
                    "verified_at": None,
                }
            else:
                # Verification recording on the in-progress task: the
                # owner is preserved unless explicitly re-declared.
                if owner is not None and owner.strip():
                    updated["owner"] = owner.strip()
                updated["verification"] = {
                    "evidence": (evidence or "").strip(),
                    "limitations": (limitations or "").strip(),
                    "result": result,
                    "verified_at": utc_now_iso(),
                }
        elif new_status == "blocked":
            updated["status"] = "blocked"
            updated["blocked_reason"] = (reason or "").strip()
        elif new_status == "pending":
            # Unblock: back to an unclaimed pending task.
            updated["status"] = "pending"
            updated["owner"] = None
            updated["blocked_reason"] = None
        elif new_status == "completed":
            updated["status"] = "completed"
            updated["verification"] = {
                "evidence": (evidence or "").strip(),
                "limitations": (limitations or "").strip(),
                "result": "passed",
                "verified_at": utc_now_iso(),
            }
        else:  # pragma: no cover - validate_transition covers the rest
            raise ValidationError(f"task {task_id}: unsupported transition")

        new_document = dict(snapshot)
        new_document["tasks"] = [
            updated if t["id"] == task_id else t for t in snapshot["tasks"]
        ]
        new_document["plan_version"] = snapshot["plan_version"] + 1
        return new_document, True

    completion = policy is not None and new_status == "completed"
    document, _ = _mutate_under_lock(
        aperio_dir, "task update", mutate, lock_timeout,
        guard=(lambda: _reviews_guard(aperio_dir)) if completion else None,
    )
    updated_task = next(t for t in document["tasks"] if t["id"] == task_id)
    if role is not None:
        _policy_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="task.status",
            target=f"task:{task_id}",
            revision=document["plan_version"],
            outcome=updated_task["status"],
        )
    return updated_task


def plan_summary(aperio_dir: Path) -> dict | None:
    """Concise plan/task summary for the portable memory API.

    Returns None when no plan exists (a normal state). File-level damage
    (non-UTF-8, invalid JSON) is fatal; a *newer plan schema version* is
    non-fatal: a degraded summary with an ``unsupported`` marker is
    returned so ``aperio memory`` keeps working with an unreadable-but-
    present plan. Live symlinks are refused; dangling links read as
    absent (None).
    """
    path = plan_path(aperio_dir)
    if os.path.islink(path) and path.exists():
        raise PathError(
            f"refusing to follow symlink at {path} while reading the plan "
            "(it could point at local or private data); remove it and "
            "re-run 'aperio plan-create' to restore the plan"
        )
    if not path.exists():
        return None
    raw = _read_snapshot(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file manually."
        ) from None

    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if (
        isinstance(data, dict)
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version > PLAN_SCHEMA_VERSION
    ):
        # A genuine newer plan schema: degrade gracefully (never fatal for
        # an optional artifact) — anything else is corrupt and fatal.
        return {
            "present": True,
            "unsupported_schema": True,
            "schema_version": schema_version,
        }
    plan = validate_plan_document(data, path)
    counts: dict[str, int] = {status: 0 for status in
                              ("pending", "in-progress", "blocked", "completed")}
    for task in plan["tasks"]:
        counts[task["status"]] += 1
    approval = plan["approval"]
    dependency_ready = ready_task_ids(plan)
    approved = approval["status"] == "approved"
    return {
        "present": True,
        "unsupported_schema": False,
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_version": plan["plan_version"],
        # plan-schema v3 optional roadmap linkage (None when the plan
        # predates it or declares none — reading never invents one)
        "milestone": plan.get("milestone"),
        "approval_status": approval["status"],
        "approved_revision": approval["approved_revision"],
        "approval_required": not approved,
        "objective": plan["objective"],
        "task_counts": counts,
        # dependency-ready: pending with all dependencies completed
        "ready_task_ids": dependency_ready,
        # executable: dependency-ready AND the plan is approved — starting
        # work on a draft-plan task is refused by the approval gate
        "executable_task_ids": dependency_ready if approved else [],
        "blocked_task_ids": sorted(
            task["id"] for task in plan["tasks"] if task["status"] == "blocked"
        ),
    }


def plan_review_basis(aperio_dir: Path) -> dict | None:
    """Read-only snapshot used to judge whether a review still applies.

    Independent reviews are bound to a material plan revision, a work
    attempt, and an evidence revision. This returns exactly those facts so
    a read-only consumer (the universal context pack) can tell a *current*
    approval from a historical one without re-implementing the lifecycle.

    Returns ``None`` when no plan exists or the plan declares a newer
    schema version: in that case no review can be shown as current.

    :raises StateError: when the plan file is corrupt (a consumer that must
        degrade catches it and treats reviews as not current).
    :raises PathError: when the plan file is a symlink.
    """
    summary = plan_summary(aperio_dir)
    if summary is None or summary.get("unsupported_schema"):
        return None
    plan = read_plan(aperio_dir)
    approval = plan["approval"]
    tasks: dict[str, dict] = {}
    for task in plan["tasks"]:
        verification = task.get("verification") or {}
        tasks[task["id"]] = {
            "status": task["status"],
            "attempt": int(task.get("attempt", 0)),
            "revision": int(task.get("revision", 0)),
            "implementer": task.get("implementer") or task.get("owner"),
            "verification_result": verification.get("result"),
        }
    return {
        "approved": approval["status"] == "approved",
        "material_revision": approval["approved_revision"],
        "plan_version": plan["plan_version"],
        "tasks": tasks,
    }
