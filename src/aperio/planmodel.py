"""Plan data model: validation, dependency graph, task lifecycle.

Pure functions over plain dicts — no filesystem access (persistence lives
in :mod:`aperio.planstore`). The plan document is schema-versioned and
dict-based like project state: unknown keys found ON DISK are tolerated
and preserved, while *input* documents (``plan-create`` / ``plan-revise``)
are strictly validated — unknown or system-managed keys are rejected so a
typo can never silently land in the shared plan.

Schema v1 (plan)::

    {
      "schema_version": 1,
      "plan_version": <int, bumps on every saved change>,
      "approval": {"status": "draft"|"approved", "approved_revision": null|int,
                   "approved_at": null|ISO-Z, "approved_by": null|str},
      "objective": str, "expected_outcome": str,
      "scope": [str], "exclusions": [str], "assumptions": [str], "risks": [str],
      "strategy": str, "acceptance_criteria": [str] (>=1),
      "verification_strategy": str,
      "tasks": [<task>],
      "created_at": ISO-Z, "updated_at": ISO-Z, "aperio_version": str
    }

Task (content keys are author-supplied; lifecycle keys are
system-managed and never accepted in input documents)::

    {
      "id": "TASK-\\d+" (unique), "title": single-line str, "objective": str,
      "deliverables": [str] (>=1), "acceptance_criteria": [str] (>=1),
      "depends_on": ["TASK-..."],
      -- system-managed --
      "status": "pending"|"in-progress"|"blocked"|"completed",
      "owner": null|str, "blocked_reason": null|str,
      "verification": {"evidence": null|str, "limitations": null|str,
                       "result": null|"passed"|"failed", "verified_at": null|ISO-Z}
    }

Lifecycle rules (all transitions explicit; none silent):

- Readiness is DERIVED, never stored: a task is ready when it is
  ``pending`` and every dependency is ``completed``.
- ``pending → in-progress``: requires an ``owner`` and all dependencies
  completed (re-checked at operation time).
- ``pending/in-progress → blocked``: requires a ``blocked_reason``.
- ``blocked → pending``: unblock; owner and reason are cleared.
- ``in-progress → completed``: requires dependencies completed,
  verification ``evidence`` and ``limitations``, and ``result == "passed"``
  supplied in the same operation. A ``failed`` result records the
  verification but keeps the task in-progress.
- ``completed → in-progress``: explicit reopen; the verification block is
  cleared (durable history belongs in the decision log); refused while any
  dependent task is in-progress or completed.
- Everything else is forbidden (e.g. ``blocked → completed``).

Invariants enforced on every stored document: a ``completed`` task has
passed verification with evidence; a ``blocked`` task has a reason.

The ``tasks`` array order is not semantic; readers sort by id or
topologically. Failure records are last-write; durable failure history
belongs in the append-only decision log.
"""

from __future__ import annotations

import re

from aperio.errors import StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators

PLAN_SCHEMA_VERSION = 1

TASK_STATUSES = ("pending", "in-progress", "blocked", "completed")
VERIFICATION_RESULTS = ("passed", "failed")

_TASK_ID_RE = re.compile(r"^TASK-\d+$")

# Author-supplied task content (the only keys accepted in input documents).
TASK_CONTENT_KEYS = (
    "id",
    "title",
    "objective",
    "deliverables",
    "acceptance_criteria",
    "depends_on",
)
# Lifecycle keys managed exclusively by task-status operations.
TASK_SYSTEM_KEYS = ("status", "owner", "blocked_reason", "verification")

PLAN_SYSTEM_KEYS = (
    "schema_version",
    "plan_version",
    "approval",
    "created_at",
    "updated_at",
    "aperio_version",
)
PLAN_CONTENT_KEYS = (
    "objective",
    "expected_outcome",
    "scope",
    "exclusions",
    "assumptions",
    "risks",
    "strategy",
    "acceptance_criteria",
    "verification_strategy",
    "tasks",
)

_PLAN_LIST_FIELDS = ("scope", "exclusions", "assumptions", "risks")
_PLAN_STR_FIELDS = (
    "objective",
    "expected_outcome",
    "strategy",
    "verification_strategy",
)
_TASK_LIST_FIELDS = ("deliverables", "acceptance_criteria", "depends_on")
_TASK_STR_FIELDS = ("objective",)


def _check_text(artifact: str, field: str, value: object, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{artifact}: field '{field}' must be a non-empty string")
    stripped = value.strip()
    check_no_foreign_line_separators(artifact, field, stripped)
    if single_line and "\n" in stripped:
        raise ValidationError(
            f"{artifact}: field '{field}' must be a single line"
        )
    return stripped


def _check_str_list(artifact: str, field: str, value: object, *, min_len: int = 0) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{artifact}: field '{field}' must be a list of strings")
    cleaned: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"{artifact}: field '{field}'[{index}] must be a non-empty string"
            )
        stripped = item.strip()
        check_no_foreign_line_separators(artifact, f"{field}[{index}]", stripped)
        cleaned.append(stripped)
    if len(cleaned) < min_len:
        raise ValidationError(
            f"{artifact}: field '{field}' needs at least {min_len} entr(y/ies)"
        )
    return cleaned


# --- input (create/revise) validation: strict, content-only -------------------


def validate_plan_input(data: object) -> dict:
    """Validate a ``plan-create`` / ``plan-revise`` input document.

    Accepts ONLY author-supplied content keys: system-managed keys
    (``schema_version``, ``plan_version``, ``approval``, timestamps,
    ``aperio_version``) and task lifecycle keys are rejected — they are
    owned by aperio, and a typo'd or forged key must never land in the
    shared plan. Unknown keys are rejected for the same reason.

    :returns: a normalized content dict (safe to embed into a document).
    :raises ValidationError: for any shape violation, with the offending
        key named.
    """
    if not isinstance(data, dict):
        raise ValidationError("plan input must be a JSON object")

    unknown = sorted(set(data) - set(PLAN_CONTENT_KEYS))
    if unknown:
        raise ValidationError(
            f"plan input has unknown or system-managed field(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(PLAN_CONTENT_KEYS)})"
        )
    missing = [key for key in PLAN_CONTENT_KEYS if key not in data]
    if missing:
        raise ValidationError(
            f"plan input is missing required field(s): {', '.join(missing)}"
        )

    plan: dict = {}
    for field in _PLAN_STR_FIELDS:
        plan[field] = _check_text("plan", field, data[field])
    for field in _PLAN_LIST_FIELDS:
        plan[field] = _check_str_list("plan", field, data[field])
    plan["acceptance_criteria"] = _check_str_list(
        "plan", "acceptance_criteria", data["acceptance_criteria"], min_len=1
    )

    tasks_input = data["tasks"]
    if not isinstance(tasks_input, list):
        raise ValidationError("plan: field 'tasks' must be a list of task objects")
    plan["tasks"] = [
        validate_task_input(task, where=f"tasks[{index}]")
        for index, task in enumerate(tasks_input)
    ]
    _validate_task_graph(plan["tasks"])
    return plan


def validate_task_input(task: object, *, where: str) -> dict:
    """Validate one author-supplied task (content keys only)."""
    if not isinstance(task, dict):
        raise ValidationError(f"plan: {where} must be a JSON object")
    unknown = sorted(set(task) - set(TASK_CONTENT_KEYS))
    if unknown:
        raise ValidationError(
            f"plan: {where} has unknown or system-managed field(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(TASK_CONTENT_KEYS)})"
        )
    missing = [key for key in TASK_CONTENT_KEYS if key not in task]
    if missing:
        raise ValidationError(
            f"plan: {where} is missing required field(s): {', '.join(missing)}"
        )
    cleaned: dict = {"id": _check_task_id(task["id"], where)}
    cleaned["title"] = _check_text("plan", f"{where}.title", task["title"], single_line=True)
    for field in _TASK_STR_FIELDS:
        cleaned[field] = _check_text("plan", f"{where}.{field}", task[field])
    for field in _TASK_LIST_FIELDS:
        minimum = 1 if field in ("deliverables", "acceptance_criteria") else 0
        cleaned[field] = _check_str_list(
            "plan", f"{where}.{field}", task[field], min_len=minimum
        )
    return cleaned


def _check_task_id(value: object, where: str) -> str:
    if not isinstance(value, str) or not _TASK_ID_RE.match(value.strip()):
        raise ValidationError(
            f"plan: {where}.id must match 'TASK-<digits>' (e.g. TASK-001)"
        )
    return value.strip()


def _validate_task_graph(tasks: list[dict]) -> None:
    """Uniqueness, dependency existence, self-dependency, and cycles."""
    seen: dict[str, int] = {}
    for index, task in enumerate(tasks):
        task_id = task["id"]
        if task_id in seen:
            raise ValidationError(
                f"plan: duplicate task id {task_id} (tasks[{seen[task_id]}] "
                f"and tasks[{index}])"
            )
        seen[task_id] = index

    ids = set(seen)
    for task in tasks:
        for dep in task["depends_on"]:
            if dep == task["id"]:
                raise ValidationError(
                    f"plan: task {task['id']} depends on itself"
                )
            if dep not in ids:
                raise ValidationError(
                    f"plan: task {task['id']} depends on unknown task {dep}"
                )

    # Cycle detection with the cycle path in the error (iterative DFS).
    state: dict[str, int] = {}  # 0 = visiting, 1 = done
    for task in tasks:
        root = task["id"]
        if state.get(root) == 1:
            continue
        stack = [(root, iter(tasks[seen[root]]["depends_on"]))]
        state[root] = 0
        path: list[str] = [root]
        while stack:
            node, deps_iter = stack[-1]
            advanced = False
            for dep in deps_iter:
                if state.get(dep) == 0:
                    cycle = path[path.index(dep):] + [dep]
                    raise ValidationError(
                        "plan: dependency cycle detected: "
                        + " -> ".join(cycle)
                    )
                if state.get(dep) is None:
                    state[dep] = 0
                    path.append(dep)
                    dep_task = next(t for t in tasks if t["id"] == dep)
                    stack.append((dep, iter(dep_task["depends_on"])))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                state[node] = 1
                path.pop()


# --- stored-document validation: tolerant of unknown keys ---------------------


def validate_plan_document(data: object, path) -> dict:
    """Validate a parsed plan.json document (unknown keys tolerated).

    :raises StateError: for shape/schema damage (the caller converts
        future schema versions to their own message before this runs).
    """
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: plan must be a JSON object")

    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise StateError(f"{path} is corrupt: 'schema_version' must be an integer")
    if schema_version != PLAN_SCHEMA_VERSION:
        raise StateError(
            f"{path} declares plan schema version {schema_version}, but this "
            f"aperio supports exactly version {PLAN_SCHEMA_VERSION}; aperio "
            "will not modify it."
        )
    plan_version = data.get("plan_version")
    if isinstance(plan_version, bool) or not isinstance(plan_version, int) or plan_version < 1:
        raise StateError(f"{path} is corrupt: 'plan_version' must be a positive integer")

    approval = data.get("approval")
    if not isinstance(approval, dict):
        raise StateError(f"{path} is corrupt: 'approval' must be an object")
    status = approval.get("status")
    if status not in ("draft", "approved"):
        raise StateError(f"{path} is corrupt: 'approval.status' must be 'draft' or 'approved'")
    rev = approval.get("approved_revision")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        raise StateError(
            f"{path} is corrupt: 'approval.approved_revision' must be null or an integer"
        )
    for field in ("approved_at", "approved_by"):
        value = approval.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise StateError(
                f"{path} is corrupt: 'approval.{field}' must be null or a non-empty string"
            )
    if status == "approved":
        if not isinstance(rev, int) or rev < 1:
            raise StateError(
                f"{path} is corrupt: approved plans must record a positive "
                "'approval.approved_revision'"
            )
        if not isinstance(approval.get("approved_at"), str) or not approval["approved_at"]:
            raise StateError(
                f"{path} is corrupt: approved plans must record 'approved_at'"
            )

    for field in ("created_at", "updated_at", "aperio_version"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise StateError(f"{path} is corrupt: '{field}' must be a non-empty string")

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise StateError(f"{path} is corrupt: 'tasks' must be a list")

    ids: set[str] = set()
    for index, task in enumerate(tasks):
        _validate_stored_task(task, f"tasks[{index}]", path)
        if task["id"] in ids:
            raise StateError(
                f"{path} is corrupt: duplicate task id {task['id']}"
            )
        ids.add(task["id"])
    for task in tasks:
        for dep in task["depends_on"]:
            if dep not in ids:
                raise StateError(
                    f"{path} is corrupt: task {task['id']} depends on "
                    f"unknown task {dep}"
                )
    return data


def _validate_stored_task(task: object, where: str, path) -> None:
    if not isinstance(task, dict):
        raise StateError(f"{path} is corrupt: {where} must be an object")
    for key in TASK_CONTENT_KEYS + TASK_SYSTEM_KEYS:
        if key not in task:
            raise StateError(f"{path} is corrupt: {where} is missing '{key}'")
    if not _TASK_ID_RE.match(str(task["id"])):
        raise StateError(f"{path} is corrupt: {where}.id must match TASK-<digits>")
    for field in ("title", "objective"):
        if not isinstance(task[field], str) or not task[field].strip():
            raise StateError(f"{path} is corrupt: {where}.{field} must be a non-empty string")
    for field in ("deliverables", "acceptance_criteria"):
        if not isinstance(task[field], list) or not task[field]:
            raise StateError(
                f"{path} is corrupt: {where}.{field} must be a non-empty list"
            )
    if not isinstance(task["depends_on"], list):
        raise StateError(f"{path} is corrupt: {where}.depends_on must be a list")
    status = task["status"]
    if status not in TASK_STATUSES:
        raise StateError(f"{path} is corrupt: {where}.status is invalid")
    owner = task["owner"]
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise StateError(f"{path} is corrupt: {where}.owner must be null or a non-empty string")
    reason = task["blocked_reason"]
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise StateError(f"{path} is corrupt: {where}.blocked_reason must be null or a non-empty string")
    if status == "blocked" and not (isinstance(reason, str) and reason.strip()):
        raise StateError(
            f"{path} is corrupt: blocked task {task['id']} needs a blocked_reason"
        )
    verification = task["verification"]
    if not isinstance(verification, dict):
        raise StateError(f"{path} is corrupt: {where}.verification must be an object")
    for key in ("evidence", "limitations", "verified_at"):
        value = verification.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise StateError(
                f"{path} is corrupt: {where}.verification.{key} must be null or a non-empty string"
            )
    result = verification.get("result")
    if result is not None and result not in VERIFICATION_RESULTS:
        raise StateError(
            f"{path} is corrupt: {where}.verification.result must be null, 'passed' or 'failed'"
        )
    if status == "completed":
        if result != "passed" or not (isinstance(verification.get("evidence"), str)
                                      and verification["evidence"].strip()):
            raise StateError(
                f"{path} is corrupt: completed task {task['id']} needs passed "
                "verification with evidence"
            )


# --- derived readiness --------------------------------------------------------


def task_is_ready(plan: dict, task_id: str) -> bool:
    """Deterministic readiness: pending with every dependency completed."""
    tasks = {task["id"]: task for task in plan["tasks"]}
    task = tasks[task_id]
    if task["status"] != "pending":
        return False
    return all(tasks[dep]["status"] == "completed" for dep in task["depends_on"])


def ready_task_ids(plan: dict) -> list[str]:
    """All ready task ids, sorted for deterministic output."""
    return sorted(
        task["id"] for task in plan["tasks"] if task_is_ready(plan, task["id"])
    )


def _dependencies_satisfied(plan: dict, task: dict) -> bool:
    tasks = {t["id"]: t for t in plan["tasks"]}
    return all(tasks[dep]["status"] == "completed" for dep in task["depends_on"])


# --- lifecycle transitions -----------------------------------------------------

_ALLOWED: dict[str, set[str]] = {
    "pending": {"in-progress", "blocked"},
    "in-progress": {"blocked", "completed"},
    "blocked": {"pending"},
    "completed": {"in-progress"},  # explicit reopen
}


def validate_transition(
    plan: dict,
    task_id: str,
    new_status: str,
    *,
    owner: str | None,
    reason: str | None,
    evidence: str | None,
    limitations: str | None,
    result: str | None,
    reopen: bool = False,
) -> dict:
    """Validate one task-status transition against the lifecycle rules and
    the current plan. Returns the task dict on success (unmutated).

    ``completed → in-progress`` (reopen) additionally requires
    ``reopen=True`` — a deliberate, explicit affordance, so a delayed or
    retried claim can never silently reopen a completed task and destroy
    its verification evidence.

    :raises ValidationError: for forbidden transitions, missing fields, or
        unsatisfied dependencies.
    """
    if new_status not in TASK_STATUSES:
        raise ValidationError(
            f"task {task_id}: unknown status {new_status!r} "
            f"(allowed: {', '.join(TASK_STATUSES)})"
        )
    tasks = {task["id"]: task for task in plan["tasks"]}
    if task_id not in tasks:
        raise ValidationError(
            f"plan has no task {task_id} (known: {', '.join(sorted(tasks)) or 'none'})"
        )
    task = tasks[task_id]
    old_status = task["status"]

    if new_status == old_status:
        if (
            old_status == "in-progress"
            and isinstance(evidence, str) and evidence.strip()
            and isinstance(limitations, str) and limitations.strip()
            and result in VERIFICATION_RESULTS
        ):
            # Recording a verification attempt on the in-progress task:
            # allowed for passed and failed results (a failed result keeps
            # the task in-progress; completion requires a passed result).
            return task
        raise ValidationError(
            f"task {task_id}: status is already {old_status!r}; "
            "no transition requested"
        )
    if old_status == "completed" and not reopen:
        raise StateError(
            f"task {task_id} is completed; a delayed or retried claim must "
            "not silently reopen it (that would destroy its recorded "
            "verification evidence). Reopen deliberately with --reopen "
            "(the current verification block will be cleared — record any "
            "durable history with 'aperio decision-add' first). Nothing "
            "was modified."
        )
    if new_status not in _ALLOWED[old_status]:
        raise ValidationError(
            f"task {task_id}: transition {old_status!r} -> {new_status!r} "
            "is not allowed (allowed from "
            f"{old_status!r}: {', '.join(sorted(_ALLOWED[old_status])) or 'none'})"
        )

    if new_status == "in-progress":
        if old_status == "pending":
            if not isinstance(owner, str) or not owner.strip():
                raise ValidationError(
                    f"task {task_id}: starting work requires --owner "
                    "(the agent claiming the task)"
                )
            if not _dependencies_satisfied(plan, task):
                pending_deps = [
                    dep
                    for dep in task["depends_on"]
                    if _task_status(plan, dep) != "completed"
                ]
                raise StateError(
                    f"task {task_id}: dependencies not satisfied "
                    f"({', '.join(pending_deps)} not completed); "
                    "dependencies are re-checked at operation time"
                )
        # completed -> in-progress (reopen) is handled below.
    if new_status == "blocked":
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                f"task {task_id}: blocking requires --reason"
            )
    if new_status == "completed":
        if not _dependencies_satisfied(plan, task):
            pending_deps = [
                dep
                for dep in task["depends_on"]
                if _task_status(plan, dep) != "completed"
            ]
            raise StateError(
                f"task {task_id}: dependencies not satisfied "
                f"({', '.join(pending_deps)} not completed)"
            )
        for field, value in (("evidence", evidence), ("limitations", limitations)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"task {task_id}: completing requires {field} "
                    "(verification evidence and its limitations)"
                )
        if result != "passed":
            raise ValidationError(
                f"task {task_id}: completion requires --result passed; "
                "a failed verification cannot be recorded as successful "
                "completion (record it on the in-progress task instead)"
            )
    return task


def _task_status(plan: dict, task_id: str) -> str:
    return next(t["status"] for t in plan["tasks"] if t["id"] == task_id)


def check_reopen_allowed(plan: dict, task_id: str) -> None:
    """Reopening is refused while any dependent task is in-progress or
    completed: their progress was based on this task's completion."""
    dependents = [
        task["id"]
        for task in plan["tasks"]
        if task_id in task["depends_on"]
        and task["status"] in ("in-progress", "completed")
    ]
    if dependents:
        raise StateError(
            f"task {task_id} cannot be reopened while dependent task(s) "
            f"{', '.join(sorted(dependents))} are in-progress or completed; "
            "reopen those dependents first (deliberately, in reverse "
            "dependency order)"
        )
