"""Independent review records and lifecycle (Feature 06).

Reviews are versioned, append-only artifacts in ``.aperio/reviews.json``.
A review is submitted by the declared implementer for one approved task
at the plan's current **material revision** (the revision recorded by
``plan.approval.approved_revision``; task-status stamps advance
``plan_version`` but do not change the task contract). A different
declared reviewer decides it. Decisions are immutable: a decided record is
never edited, and a fresh attempt always receives a new ``REV-NNNN`` id.

Staleness rules enforced here:

- a review may only be decided while its ``plan_revision`` is still the
  approved material revision (a material revision invalidates it);
- a reviewer may never decide their own submission;
- only the configured ``reviewer`` role may decide, and only the
  configured ``implementer`` role may submit;
- completion of a task requires an approved review for the task at the
  current material revision. Process exit success alone never satisfies
  this — the review is a separate, attributed decision.

Like the rest of Aperio, actor ids are declarations, not authenticated
identity; this module enforces workflow consistency only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aperio.errors import PathError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock
from aperio.policy import (
    _check_actor_id,
    _check_text,
    append_event,
    load_policy,
    require_action,
)
from aperio.project import require_supported_state
from aperio.schema import utc_now_iso

REVIEWS_FILE_NAME = "reviews.json"
REVIEWS_SCHEMA_VERSION = 1
REVIEW_LOCK_NAME = "reviews.lock"
REVIEWS_TMP_NAME = "reviews.json.tmp"

REVIEW_OUTCOMES = ("pending", "approved", "changes-requested", "rejected")
DECIDED_OUTCOMES = ("approved", "changes-requested", "rejected")

_REVIEW_ID_RE = re.compile(r"^REV-[0-9]{4,}$")
_RUN_ID_RE = re.compile(r"^RUN-[0-9]{4,}$")
_MAX_ITEMS = 20
_MAX_TEXT = 300


def reviews_path(aperio_dir: Path) -> Path:
    return aperio_dir / REVIEWS_FILE_NAME


def reviews_lock_path(aperio_dir: Path) -> Path:
    return aperio_dir / REVIEW_LOCK_NAME


def _empty_document() -> dict:
    return {"schema_version": REVIEWS_SCHEMA_VERSION, "reviews": []}


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PathError(
            f"refusing to follow symlink at {path} (review records); remove "
            "it and re-run 'aperio init' to repair"
        )


def _read_raw(aperio_dir: Path) -> bytes | None:
    path = reviews_path(aperio_dir)
    _reject_symlink(path)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}; nothing was modified"
        ) from None


def _parse(raw: bytes, path: Path) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it"
        ) from None
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: "
            f"{exc.msg}); aperio will not overwrite it"
        ) from None
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: expected a JSON object")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError(f"{path} is corrupt: 'schema_version' must be an integer")
    if version > REVIEWS_SCHEMA_VERSION:
        raise StateError(
            f"{path} uses review schema version {version}, newer than "
            f"supported version {REVIEWS_SCHEMA_VERSION}; this aperio will "
            "not interpret or convert it. Upgrade aperio before changing "
            "reviews."
        )
    if version != REVIEWS_SCHEMA_VERSION:
        raise StateError(
            f"{path} uses unsupported review schema version {version}; no "
            "migration is implemented for it"
        )
    validate_reviews_document(data, path)
    return data


def validate_reviews_document(data: object, path: Path | str) -> dict:
    """Validate a parsed review document structurally (unknown keys allowed)."""
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: reviews must be a JSON object")
    if data.get("schema_version") != REVIEWS_SCHEMA_VERSION:
        raise StateError(
            f"{path} is corrupt: schema_version must be {REVIEWS_SCHEMA_VERSION}"
        )
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        raise StateError(f"{path} is corrupt: 'reviews' must be a list")
    seen: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise StateError(f"{path} is corrupt: review entries must be objects")
        review_id = review.get("review_id")
        if not isinstance(review_id, str) or not _REVIEW_ID_RE.match(review_id):
            raise StateError(f"{path} is corrupt: invalid review_id {review_id!r}")
        if review_id in seen:
            raise StateError(f"{path} is corrupt: duplicate review id {review_id}")
        seen.add(review_id)
        target = review.get("target")
        if not isinstance(target, dict):
            raise StateError(f"{path} is corrupt: {review_id}.target must be an object")
        kind = target.get("kind")
        if kind not in ("task", "plan"):
            raise StateError(
                f"{path} is corrupt: {review_id}.target.kind must be 'task' or 'plan'"
            )
        revision = target.get("plan_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise StateError(
                f"{path} is corrupt: {review_id}.target.plan_revision must be a "
                "positive integer"
            )
        if kind == "task":
            task_id = target.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise StateError(
                    f"{path} is corrupt: {review_id}.target.task_id is required"
                )
            run_id = target.get("run_id")
            if run_id is not None and (
                not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id)
            ):
                raise StateError(
                    f"{path} is corrupt: {review_id}.target.run_id is invalid"
                )
            for key in ("attempt", "task_revision"):
                if key not in target:
                    continue
                value = target[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    raise StateError(
                        f"{path} is corrupt: {review_id}.target.{key} must be "
                        "a non-negative integer"
                    )
        submitted_by = review.get("submitted_by")
        if not isinstance(submitted_by, str) or not submitted_by.strip():
            raise StateError(
                f"{path} is corrupt: {review_id}.submitted_by is required"
            )
        implemented_by = review.get("implemented_by")
        if implemented_by is not None:
            if not isinstance(implemented_by, str) or not implemented_by.strip():
                raise StateError(
                    f"{path} is corrupt: {review_id}.implemented_by must be a "
                    "non-empty string when present"
                )
            if implemented_by != submitted_by:
                raise StateError(
                    f"{path} is corrupt: review {review_id} was submitted by "
                    f"{submitted_by!r} but attributes the implementation to "
                    f"{implemented_by!r}. No audited reassignment workflow "
                    "exists, so implementation attribution must name the "
                    "submitting owner."
                )
        for field in ("scope", "evidence"):
            value = review.get(field)
            if not isinstance(value, list) or not value or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise StateError(
                    f"{path} is corrupt: {review_id}.{field} must be a non-empty "
                    "list of non-empty strings"
                )
        outcome = review.get("outcome")
        if outcome not in REVIEW_OUTCOMES:
            raise StateError(
                f"{path} is corrupt: {review_id}.outcome must be one of "
                + ", ".join(REVIEW_OUTCOMES)
            )
        reviewer = review.get("reviewer")
        if reviewer is not None and (
            not isinstance(reviewer, str) or not reviewer.strip()
        ):
            raise StateError(
                f"{path} is corrupt: {review_id}.reviewer must be null or a string"
            )
        findings = review.get("findings")
        if findings is not None and not isinstance(findings, str):
            raise StateError(
                f"{path} is corrupt: {review_id}.findings must be null or a string"
            )
        if outcome == "pending":
            if reviewer is not None or review.get("decided_at") is not None:
                raise StateError(
                    f"{path} is corrupt: pending review {review_id} must not "
                    "have a reviewer or decision timestamp"
                )
        else:
            if not reviewer:
                raise StateError(
                    f"{path} is corrupt: decided review {review_id} requires a reviewer"
                )
            if not isinstance(review.get("decided_at"), str):
                raise StateError(
                    f"{path} is corrupt: decided review {review_id} requires "
                    "decided_at"
                )
            if reviewer == submitted_by:
                raise StateError(
                    f"{path} is corrupt: review {review_id} was decided by its "
                    "own submitter; independent review is required"
                )
            if implemented_by is not None and reviewer == implemented_by:
                raise StateError(
                    f"{path} is corrupt: review {review_id} was decided by the "
                    "recorded implementer; reviewer independence is required"
                )
            if outcome in ("changes-requested", "rejected") and not (
                isinstance(findings, str) and findings.strip()
            ):
                raise StateError(
                    f"{path} is corrupt: {outcome} review {review_id} requires "
                    "recorded findings"
                )
        if not isinstance(review.get("created_at"), str):
            raise StateError(
                f"{path} is corrupt: {review_id}.created_at is required"
            )
    return data


def read_reviews(aperio_dir: Path) -> dict:
    """Return the validated review document (empty when absent)."""
    raw = _read_raw(aperio_dir)
    if raw is None:
        return _empty_document()
    return _parse(raw, reviews_path(aperio_dir))


def reviews_present(aperio_dir: Path) -> bool:
    return reviews_path(aperio_dir).is_file()


def _publish(aperio_dir: Path, data: dict, snapshot: bytes | None) -> None:
    path = reviews_path(aperio_dir)
    _reject_symlink(path)
    tmp = aperio_dir / REVIEWS_TMP_NAME
    if tmp.is_symlink():
        raise PathError(f"refusing to follow symlink at {tmp}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise StateError(
            f"a leftover temporary file exists at {tmp}; inspect and remove "
            "it manually, then retry."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        current = _read_raw(aperio_dir)
        if current != snapshot:
            raise StateError(
                "review records changed while this update was preparing; "
                "nothing was modified. Re-read them and retry."
            )
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _next_review_id(reviews: list[dict]) -> str:
    highest = 0
    for review in reviews:
        try:
            highest = max(highest, int(review["review_id"][4:]))
        except (KeyError, ValueError):
            continue
    return f"REV-{highest + 1:04d}"


def _read_plan(aperio_dir: Path) -> dict:
    from aperio.planstore import read_plan  # local: planstore imports policy

    return read_plan(aperio_dir)


def _approved_plan(aperio_dir: Path) -> tuple[dict, int]:
    plan = _read_plan(aperio_dir)
    approval = plan["approval"]
    revision = approval.get("approved_revision")
    if approval["status"] != "approved" or not isinstance(revision, int):
        raise StateError(
            "governance reviews require an approved plan revision: the current "
            f"plan is {approval['status']!r}. Approve the material revision "
            "first with 'aperio plan-approve'."
        )
    return plan, revision


def _task(plan: dict, task_id: str) -> dict:
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task
    raise StateError(f"task {task_id} is not part of the current plan")


def _run_for_task(aperio_dir: Path, run_id: str | None, task_id: str) -> None:
    if run_id is None:
        return
    from aperio.runtime import read_executions  # local: avoid import cycle

    document = read_executions(aperio_dir)
    if document is None:
        raise StateError(f"run {run_id} does not exist (no execution records)")
    for run in document["runs"]:
        if run["run_id"] != run_id:
            continue
        if run["task_id"] != task_id:
            raise StateError(
                f"run {run_id} belongs to task {run['task_id']}, not {task_id}"
            )
        return
    raise StateError(f"run {run_id} does not exist")


def _scope_list(value, *, where: str) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValidationError(f"{where} must be a string or a list of strings")
    cleaned: list[str] = []
    for item in items:
        text = _check_text(item, where=where, required=True) or ""
        cleaned.append(text)
    if not cleaned:
        raise ValidationError(f"{where} must contain at least one item")
    if len(cleaned) > _MAX_ITEMS:
        raise ValidationError(f"{where} may contain at most {_MAX_ITEMS} items")
    return cleaned


def submit_review(
    aperio_dir: Path,
    *,
    actor: str | None,
    task_id: str,
    scope,
    evidence,
    limitations: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Submit an implemented task for independent review (new record)."""
    require_supported_state(aperio_dir)
    if load_policy(aperio_dir) is None:
        raise StateError(
            "independent reviews require project governance; enable it with "
            "'aperio policy-init' first"
        )
    role = require_action(
        aperio_dir, actor, "review.submit", target=f"task:{task_id}"
    )
    plan, revision = _approved_plan(aperio_dir)
    task = _task(plan, task_id)
    if task["status"] != "in-progress":
        raise StateError(
            f"task {task_id} is {task['status']!r}; only an in-progress "
            "implementation can be submitted for Review. Finish the work and "
            "record evidence first. Nothing was modified."
        )
    owner = task.get("implementer") or task.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise StateError(
            f"task {task_id} has no recorded implementer, so independent "
            "review cannot establish whose work is being approved. Claim the "
            "task explicitly before submitting. Nothing was modified."
        )
    if actor != owner:
        raise StateError(
            f"task {task_id} is implemented by {owner!r}; declared actor "
            f"{actor!r} cannot submit it for review. Submission must come "
            "from the recorded implementer (no implicit reassignment "
            "workflow exists). Nothing was modified."
        )
    attempt = int(task.get("attempt", 0))
    task_revision = int(task.get("revision", 0))
    _run_for_task(aperio_dir, run_id, task_id)
    scope_items = _scope_list(scope, where="--scope")
    evidence_items = _scope_list(evidence, where="--evidence")
    limitations_text = _check_text(limitations, where="--limitations")
    record = {
        "review_id": None,
        "target": {
            "kind": "task",
            "task_id": task_id,
            "plan_revision": revision,
            "run_id": run_id,
            "attempt": attempt,
            "task_revision": task_revision,
        },
        "submitted_by": actor,
        "implemented_by": owner,
        "submitted_role": role,
        "scope": scope_items,
        "evidence": evidence_items,
        "outcome": "pending",
        "reviewer": None,
        "findings": None,
        "limitations": limitations_text,
        "created_at": utc_now_iso(),
        "decided_at": None,
    }
    token = acquire_lock(reviews_lock_path(aperio_dir), timeout=5.0,
                         artifact="review records")
    try:
        raw = _read_raw(aperio_dir)
        document = _empty_document() if raw is None else _parse(
            raw, reviews_path(aperio_dir)
        )
        # Re-check authorization and staleness now that the store is locked:
        # a concurrent policy reassignment must not publish a review under a
        # role the actor no longer holds.
        require_action(
            aperio_dir, actor, "review.submit", target=f"task:{task_id}"
        )
        current_plan, current_revision = _approved_plan(aperio_dir)
        if current_revision != revision:
            raise StateError(
                "the plan approval changed while the review was being "
                "submitted; nothing was modified. Re-read the project and "
                "retry against the current material revision."
            )
        current_task = _task(current_plan, task_id)
        if (
            current_task.get("status") != "in-progress"
            or (current_task.get("implementer") or current_task.get("owner"))
            != owner
            or int(current_task.get("attempt", 0)) != attempt
            or int(current_task.get("revision", 0)) != task_revision
        ):
            raise StateError(
                "the task attempt changed while the review was being "
                "submitted (status, owner, attempt, or evidence revision); "
                "nothing was modified. Re-read the project and submit again "
                "for the current attempt."
            )
        record["review_id"] = _next_review_id(document["reviews"])
        document["reviews"].append(record)
        _publish(aperio_dir, document, raw)
    finally:
        release_lock(reviews_lock_path(aperio_dir), token)
    append_event(
        aperio_dir,
        actor=actor,
        role=role,
        action="review.submit",
        target=f"task:{task_id}",
        revision=revision,
        outcome="pending",
        review_id=record["review_id"],
    )
    return record


def decide_review(
    aperio_dir: Path,
    review_id: str,
    *,
    actor: str | None,
    outcome: str,
    findings: str | None = None,
    limitations: str | None = None,
) -> dict:
    """Record an immutable approval / changes-requested / rejection."""
    require_supported_state(aperio_dir)
    if outcome not in DECIDED_OUTCOMES:
        raise ValidationError(
            "review-decide: --outcome must be one of "
            + ", ".join(DECIDED_OUTCOMES)
        )
    if not isinstance(review_id, str) or not _REVIEW_ID_RE.match(review_id):
        raise ValidationError("review-decide: review id must look like REV-NNNN")
    findings_text = _check_text(findings, where="--findings")
    if outcome in ("changes-requested", "rejected") and not findings_text:
        raise ValidationError(
            f"review-decide: a {outcome} decision requires recorded findings"
        )
    limitations_text = _check_text(limitations, where="--limitations")
    if load_policy(aperio_dir) is None:
        raise StateError(
            "independent reviews require project governance; enable it with "
            "'aperio policy-init' first"
        )
    role = require_action(
        aperio_dir, actor, "review.decide", target=f"review:{review_id}"
    )
    token = acquire_lock(reviews_lock_path(aperio_dir), timeout=5.0,
                         artifact="review records")
    try:
        raw = _read_raw(aperio_dir)
        if raw is None:
            raise StateError(
                "no review records exist; nothing to decide"
            )
        document = _parse(raw, reviews_path(aperio_dir))
        record = next(
            (item for item in document["reviews"]
             if item["review_id"] == review_id),
            None,
        )
        if record is None:
            raise StateError(
                f"review {review_id} does not exist; nothing was modified"
            )
        if record["outcome"] != "pending":
            raise StateError(
                f"review {review_id} is already decided "
                f"({record['outcome']!r}); decisions are immutable. Submit a "
                "new review if further work is needed."
            )
        if actor == record["submitted_by"]:
            raise StateError(
                "separation of duties: a reviewer may not decide their own "
                "submission. Have an independent declared reviewer decide it. "
                "Nothing was modified."
            )
        require_action(
            aperio_dir, actor, "review.decide", target=f"review:{review_id}"
        )
        target = record["target"]
        implemented_by = record.get("implemented_by")
        if (
            implemented_by is None
            or "attempt" not in target
            or "task_revision" not in target
        ):
            raise StateError(
                f"review {review_id} predates attempt-bound review attribution "
                "and cannot establish which implementation or evidence it "
                "covers. Refusing to guess; submit a new review for the "
                "current attempt. Nothing was modified."
            )
        if actor == implemented_by:
            raise StateError(
                "separation of duties: a reviewer may not decide a review of "
                "their own implementation. Have an independent declared "
                "reviewer decide it. Nothing was modified."
            )
        plan, revision = _approved_plan(aperio_dir)
        if revision != target["plan_revision"]:
            raise StateError(
                f"review {review_id} targets plan revision "
                f"{target['plan_revision']}, but the current approved material "
                f"revision is {revision}. The revision changed after "
                "submission, so this decision would be stale. Nothing was "
                "modified; submit a new review after re-approval."
            )
        if target["kind"] == "task":
            task = _task(plan, target["task_id"])
            if task["status"] == "completed":
                raise StateError(
                    f"task {target['task_id']} is already completed; the "
                    "review decision cannot change that. Reopen deliberately "
                    "if new work is needed."
                )
            if (
                task.get("status") != "in-progress"
                or int(task.get("attempt", 0)) != target["attempt"]
                or int(task.get("revision", 0)) != target["task_revision"]
                or (task.get("implementer") or task.get("owner"))
                != implemented_by
            ):
                raise StateError(
                    f"review {review_id} targets work attempt "
                    f"{target['attempt']} at task revision "
                    f"{target['task_revision']}, but the task has moved on "
                    f"(attempt {int(task.get('attempt', 0))}, revision "
                    f"{int(task.get('revision', 0))}). The decision would be "
                    "stale; submit a new review for the current attempt. "
                    "Nothing was modified."
                )
        updated = dict(record)
        updated["outcome"] = outcome
        updated["reviewer"] = actor
        updated["findings"] = findings_text
        if limitations_text is not None:
            updated["limitations"] = limitations_text
        updated["decided_at"] = utc_now_iso()
        document["reviews"] = [
            updated if item["review_id"] == review_id else item
            for item in document["reviews"]
        ]
        validate_reviews_document(document, reviews_path(aperio_dir))
        _publish(aperio_dir, document, raw)
    finally:
        release_lock(reviews_lock_path(aperio_dir), token)
    append_event(
        aperio_dir,
        actor=actor,
        role=role,
        action="review.decide",
        target=f"review:{review_id}",
        revision=target["plan_revision"],
        outcome=outcome,
        review_id=review_id,
    )
    return updated


def list_reviews(aperio_dir: Path, *, task_id: str | None = None) -> list[dict]:
    reviews = read_reviews(aperio_dir)["reviews"]
    if task_id is None:
        return reviews
    return [
        review for review in reviews
        if review["target"].get("task_id") == task_id
    ]


def _latest_for_revision(
    aperio_dir: Path, task_id: str, material_revision: int
) -> dict | None:
    candidates = [
        review
        for review in list_reviews(aperio_dir, task_id=task_id)
        if review["target"]["plan_revision"] == material_revision
    ]
    return candidates[-1] if candidates else None


def approved_review(
    aperio_dir: Path,
    task_id: str,
    material_revision: int,
    *,
    attempt: int | None = None,
    task_revision: int | None = None,
) -> dict | None:
    """Latest approved review for the task/material revision.

    When ``attempt``/``task_revision`` are supplied, a review only counts
    if it targets exactly that work attempt and evidence revision. Without
    them this is a historical lookup only and must not be used to gate
    completion.
    """
    latest = _latest_for_revision(aperio_dir, task_id, material_revision)
    if latest is None or latest["outcome"] != "approved":
        return None
    target = latest["target"]
    if attempt is not None and target.get("attempt") != attempt:
        return None
    if task_revision is not None and target.get("task_revision") != task_revision:
        return None
    return latest


def review_requirement(
    aperio_dir: Path,
    task_id: str,
    material_revision: int,
    *,
    attempt: int,
    task_revision: int,
    implemented_by: str | None = None,
    lock: bool = False,
) -> dict:
    """Serialize the completion decision against concurrent review writes.

    Completion runs while holding ``plan.lock``; taking ``reviews.lock``
    (later in the fixed order) ensures a decision cannot land between the
    review check and the published completion.
    """
    if not lock:
        return _review_requirement(
            aperio_dir, task_id, material_revision,
            attempt=attempt, task_revision=task_revision,
            implemented_by=implemented_by,
        )
    token = acquire_lock(
        reviews_lock_path(aperio_dir), timeout=5.0, artifact="review records"
    )
    try:
        return _review_requirement(
            aperio_dir, task_id, material_revision,
            attempt=attempt, task_revision=task_revision,
            implemented_by=implemented_by,
        )
    finally:
        release_lock(reviews_lock_path(aperio_dir), token)


def _review_requirement(
    aperio_dir: Path,
    task_id: str,
    material_revision: int,
    *,
    attempt: int,
    task_revision: int,
    implemented_by: str | None = None,
) -> dict:
    """Return the review that satisfies verified completion, or fail closed.

    The latest review for the task at the current material revision is the
    only candidate: a newer pending/changes-requested/rejected decision
    always blocks an older approval. It must target the current work
    attempt and task revision (so reopening or revising evidence requires
    a fresh review), be approved, and have been decided by someone other
    than the recorded implementer/submitter.
    """
    latest = _latest_for_revision(aperio_dir, task_id, material_revision)
    if latest is None:
        raise StateError(
            f"task {task_id} cannot be completed: governance requires an "
            f"approved independent review for work attempt {attempt} at the "
            f"current material revision {material_revision}. Submit with "
            "'aperio review-submit' and have a different declared reviewer "
            "decide it; a successful process exit is not verification. "
            "Nothing was modified."
        )
    target = latest["target"]
    if (
        latest.get("implemented_by") is None
        or "attempt" not in target
        or "task_revision" not in target
    ):
        raise StateError(
            f"task {task_id} cannot be completed: the latest review "
            f"{latest['review_id']} predates attempt-bound attribution and "
            "cannot prove which work or evidence it covers. Submit a new "
            "review for the current attempt. Nothing was modified."
        )
    if target.get("attempt") != attempt:
        raise StateError(
            f"task {task_id} cannot be completed: the latest review "
            f"{latest['review_id']} covers work attempt {target.get('attempt')} "
            f"but the current attempt is {attempt} (the task was reopened or "
            "reclaimed). Prior approvals do not carry over; submit a new "
            "review for this attempt. Nothing was modified."
        )
    if target.get("task_revision") != task_revision:
        raise StateError(
            f"task {task_id} cannot be completed: work or evidence changed "
            f"after review {latest['review_id']} was submitted (task revision "
            f"{target.get('task_revision')} reviewed, current revision "
            f"{task_revision}). Submit a fresh review covering the current "
            "evidence. Nothing was modified."
        )
    if latest.get("outcome") != "approved":
        raise StateError(
            f"task {task_id} cannot be completed: the latest review "
            f"{latest['review_id']} is {latest.get('outcome')!r}"
            + (
                f" with findings {latest.get('findings')!r}"
                if latest.get("findings")
                else ""
            )
            + ". An older approval cannot override a newer decision; submit "
            "a new review after addressing the findings. Nothing was "
            "modified."
        )
    if latest.get("reviewer") == latest.get("implemented_by") or (
        latest.get("reviewer") == latest.get("submitted_by")
    ):
        raise StateError(
            f"task {task_id} cannot be completed: review {latest['review_id']} "
            "was decided by its own implementer/submitter and is not "
            "independent verification. Nothing was modified."
        )
    if implemented_by is not None and latest.get("implemented_by") != implemented_by:
        raise StateError(
            f"task {task_id} cannot be completed: review "
            f"{latest['review_id']} attributes implementation to "
            f"{latest.get('implemented_by')!r}, but the task's recorded "
            f"implementer is {implemented_by!r}. Ambiguous attribution is "
            "refused; submit a new review from the recorded implementer. "
            "Nothing was modified."
        )
    return latest


def review_status(aperio_dir: Path) -> dict:
    """Read-only review summary for memory/CLI (future schema degrades)."""
    try:
        document = read_reviews(aperio_dir)
        present = reviews_present(aperio_dir)
        unsupported = None
    except StateError as exc:
        return {
            "present": True,
            "unsupported": True,
            "detail": str(exc),
            "pending": [],
            "latest": {},
        }
    if not present:
        return {"present": False, "pending": [], "latest": {}}
    pending = [
        {
            "review_id": review["review_id"],
            "target": review["target"],
            "submitted_by": review["submitted_by"],
            "created_at": review["created_at"],
        }
        for review in document["reviews"]
        if review["outcome"] == "pending"
    ]
    latest: dict[str, dict] = {}
    for review in document["reviews"]:
        task_id = review["target"].get("task_id")
        key = task_id or f"plan:{review['target']['plan_revision']}"
        latest[key] = {
            "review_id": review["review_id"],
            "outcome": review["outcome"],
            "reviewer": review["reviewer"],
            "plan_revision": review["target"]["plan_revision"],
            "decided_at": review["decided_at"],
        }
    return {
        "present": True,
        "unsupported": unsupported,
        "pending": pending,
        "latest": latest,
    }


def blocking_requirements(aperio_dir: Path) -> list[dict]:
    """Completion requirements currently blocking tasks, for memory output."""
    try:
        plan = _read_plan(aperio_dir)
    except StateError:
        return []
    revision = plan["approval"].get("approved_revision")
    if plan["approval"]["status"] != "approved" or not isinstance(revision, int):
        return []
    try:
        document = read_reviews(aperio_dir)
    except StateError:
        return []
    blocking: list[dict] = []
    for task in plan["tasks"]:
        if task["status"] != "in-progress":
            continue
        try:
            _review_requirement(
                aperio_dir, task["id"], revision,
                attempt=int(task.get("attempt", 0)),
                task_revision=int(task.get("revision", 0)),
                implemented_by=task.get("implementer") or task.get("owner"),
            )
        except StateError:
            blocking.append({
                "task_id": task["id"],
                "requirement": "approved_independent_review",
                "plan_revision": revision,
            })
    return blocking
