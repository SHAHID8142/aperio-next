"""Controlled multi-agent orchestration (orchestration-schema v1).

When an assignment spans several kinds of work ("design, frontend,
backend, CMS, security"), Aperio helps decide whether it belongs to one
capable agent or to sequential specialist assignments — and then executes
that decision through the EXISTING plan, scheduling, and execution
machinery. This module is a supervising layer, not a second engine:

* **Assess, don't spawn.** ``assess_orchestration`` evaluates explicit,
  explainable criteria (task count, per-task capability requirements,
  dependency shape) and recommends single-agent or specialist
  delegation with the reasons listed. A large task is never
  automatically multi-agent; parallel execution is **not offered** at
  all, because non-overlapping write scopes cannot be proven for the
  existing adapters — sequential dispatch is the only mode, and the
  assessment says so.
* **Delegate through real tasks.** The orchestration plan maps plan
  tasks to specialists (adapter ids from the existing adapter
  registry). Dispatch still happens through ``run-start`` /
  ``runs_dispatch`` with their authorization gates, eligibility checks,
  execution records, and reconciliation. Orchestration never launches
  anything itself.
* **Scoped context per specialist.** Each delegation carries a
  specialist brief built from the task's own contract (title,
  objective, deliverables, acceptance criteria, dependencies) plus the
  operator goal — never another task's deliverables, never operator
  authority: the brief is quoted context under the trust boundary, and
  an implementer still cannot approve its own work (Feature 06
  governance applies unchanged).
* **Honest fallbacks.** A task whose required capability matches no
  available adapter is reported as needing the documented manual path,
  not silently routed to whatever happens to be installed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import PathError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock
from aperio.planmodel import priority_rank
from aperio.planstore import read_plan

ORCHESTRATION_FORMAT = "aperio-orchestration"
ORCHESTRATION_SCHEMA_VERSION = 1

ORCHESTRATION_MAX_BYTES = 256 * 1024
MAX_DELEGATIONS = 200

#: Capabilities each built-in adapter can serve. ``manual`` serves any
#: task because it hands off to a human-run agent.
ADAPTER_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "manual": ("any",),
    "local-process": ("general", "scripted", "any"),
    "codex-cli": ("coding", "general", "any"),
    "claude-cli": ("coding", "general", "any"),
}

SEQUENTIAL_ONLY_NOTE = (
    "parallel execution is not offered: non-overlapping write scopes "
    "cannot be proven for the existing adapters, so delegation is "
    "sequential through the normal scheduler"
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _check_text(value: object, where: str, *, max_bytes: int = 500,
                single_line: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be non-empty text")
    if "\x00" in value:
        raise ValidationError(f"{where} must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{where} must be at most {max_bytes} bytes")
    if single_line and "\n" in value:
        raise ValidationError(f"{where} must be single-line")
    return value


# ---------------------------------------------------------------------------
# assessment
# ---------------------------------------------------------------------------


def assess_orchestration(goal: dict, *, plan_tasks: list[dict],
                         available_adapters: list[str],
                         required_capabilities: dict[str, str] | None = None,
                         single_agent_threshold: int = 1) -> dict:
    """Explainable single-agent vs specialist recommendation.

    ``required_capabilities`` maps task id -> required capability string
    (e.g. ``{"TASK-002": "coding"}``) and comes from the operator or the
    goal's own annotations — it is declared input, never inferred by
    Aperio. The recommendation is a proposal for the operator; it grants
    no authorization by itself.
    """

    if not plan_tasks:
        raise ValidationError(
            "orchestration: there are no tasks to assess"
        )
    capabilities = dict(required_capabilities or {})
    unknown_caps = sorted(set(capabilities) - {t["id"] for t in plan_tasks})
    if unknown_caps:
        raise ValidationError(
            "orchestration: capability requirements name unknown task(s): "
            + ", ".join(unknown_caps)
        )

    distinct = sorted(set(capabilities.values())) if capabilities else []
    criteria = {
        "task_count": len(plan_tasks),
        "tasks_with_declared_capability": len(capabilities),
        "distinct_required_capabilities": distinct,
        "dependency_levels_present": _dependency_depth(plan_tasks) > 1,
        "single_agent_threshold": single_agent_threshold,
        "available_adapters": sorted(available_adapters),
    }

    capability_gaps: list[dict] = []
    automated = [a for a in available_adapters if a != "manual"]
    for task_id, capability in sorted(capabilities.items()):
        serving = [
            adapter for adapter in automated
            if capability in ADAPTER_CAPABILITIES.get(adapter, ())
        ]
        if not serving:
            # 'manual' is the fallback itself, not an automated server:
            # a capability no automated adapter declares is a gap even
            # though a human handoff can always take the task.
            capability_gaps.append({
                "task_id": task_id,
                "capability": capability,
                "fallback": (
                    "no available adapter serves this capability; use the "
                    "documented manual handoff path"
                ),
            })

    if len(plan_tasks) <= single_agent_threshold and not capabilities:
        recommendation = "single-agent"
        reasons = [
            f"the plan has {len(plan_tasks)} task(s) and no declared "
            "capability requirements; one capable agent with ordinary "
            "sequential dispatch is the lower-overhead choice",
        ]
    elif distinct and capability_gaps:
        recommendation = "single-agent-with-manual-fallback"
        reasons = [
            "specialist delegation was considered, but some declared "
            "capabilities have no available adapter (see capability_gaps); "
            "delegate what you can and run the gaps through the manual "
            "path",
        ]
    elif distinct:
        recommendation = "specialist-delegation"
        reasons = [
            f"the plan declares {len(distinct)} distinct required "
            f"capabilities ({', '.join(distinct)}), which different "
            "adapters serve better than one general agent",
            "delegation is sequential through the normal scheduler; "
            + SEQUENTIAL_ONLY_NOTE,
        ]
    else:
        recommendation = "single-agent"
        reasons = [
            "no capability requirements were declared; without them the "
            "assessment will not split work across agents (splitting is "
            "an explicit operator decision, not an inference from size)",
        ]
    if _dependency_depth(plan_tasks) > 1:
        reasons.append(
            "the dependency graph has multiple levels; dispatch respects "
            "dependency readiness first, priority second"
        )
    reasons.append(SEQUENTIAL_ONLY_NOTE)
    return {
        "recommendation": recommendation,
        "criteria": criteria,
        "reasons": reasons,
        "capability_gaps": capability_gaps,
        "parallel_offered": False,
        "limitations": [
            "the recommendation is explainable advice, not authorization: "
            "dispatch still requires the ordinary eligibility checks and "
            "--authorize-exec for real processes",
            "capability requirements are operator-declared input; Aperio "
            "does not infer what kind of agent a task needs",
        ],
    }


def _dependency_depth(plan_tasks: list[dict]) -> int:
    by_id = {t["id"]: t for t in plan_tasks}
    depth: dict[str, int] = {}

    def depth_of(task_id: str, seen: frozenset) -> int:
        if task_id in depth:
            return depth[task_id]
        if task_id in seen:
            return 0  # cycle (already rejected elsewhere); do not loop
        deps = by_id[task_id].get("depends_on") or []
        if not deps:
            depth[task_id] = 1
            return 1
        result = 1 + max(
            depth_of(dep, seen | {task_id}) for dep in deps if dep in by_id
        )
        depth[task_id] = result
        return result

    for task_id in by_id:
        depth_of(task_id, frozenset())
    return max(depth.values()) if depth else 0


# ---------------------------------------------------------------------------
# orchestration plan document
# ---------------------------------------------------------------------------

ORCHESTRATION_KEYS = (
    "orchestration_format",
    "orchestration_schema_version",
    "created_at",
    "actor",
    "goal_identity",
    "recommendation",
    "sequential_only",
    "delegations",
)


def build_orchestration_plan(
    goal: dict, plan_tasks: list[dict], *,
    required_capabilities: dict[str, str] | None = None,
    adapter_override: dict[str, str] | None = None,
    available_adapters: list[str],
    actor: str | None = None,
) -> dict:
    """Build the orchestration document: one delegation per task.

    Each delegation names the specialist adapter for one plan task (the
    operator's declared capability requirements, optionally overridden
    per task), plus what the specialist will receive. The document
    records the assessment reasons so the operator's decision is
    auditable. Nothing here dispatches anything.
    """

    if len(plan_tasks) > MAX_DELEGATIONS:
        raise ValidationError(
            f"orchestration: at most {MAX_DELEGATIONS} delegations"
        )
    assessment = assess_orchestration(
        goal, plan_tasks=plan_tasks,
        available_adapters=available_adapters,
        required_capabilities=required_capabilities,
    )
    overrides = dict(adapter_override or {})
    unknown_overrides = sorted(set(overrides) - {t["id"] for t in plan_tasks})
    if unknown_overrides:
        raise ValidationError(
            "orchestration: adapter overrides name unknown task(s): "
            + ", ".join(unknown_overrides)
        )

    delegations: list[dict] = []
    for task in sorted(plan_tasks, key=lambda t: (priority_rank(t), t["id"])):
        task_id = task["id"]
        if task_id in overrides:
            specialist = overrides[task_id]
            if specialist not in available_adapters:
                raise ValidationError(
                    f"orchestration: override for {task_id} names adapter "
                    f"{specialist!r}, which is not in the available set"
                )
            required = (required_capabilities or {}).get(task_id)
            basis = "operator adapter override"
        else:
            required = (required_capabilities or {}).get(task_id)
            if required:
                serving = [
                    adapter for adapter in available_adapters
                    if adapter != "manual"
                    and required in ADAPTER_CAPABILITIES.get(adapter, ())
                ]
                if serving:
                    specialist = serving[0]
                    basis = f"serves declared capability {required!r}"
                else:
                    specialist = "manual"
                    basis = (
                        f"no automated adapter serves {required!r}; manual "
                        "handoff fallback"
                    )
            else:
                specialist = "manual"
                basis = (
                    "no capability declared; the manual handoff adapter is "
                    "the neutral default — override per task if you want a "
                    "provider CLI"
                )
        delegations.append({
            "task_id": task_id,
            "title": task.get("title"),
            "priority": task.get("priority", "normal"),
            "specialist_adapter": specialist,
            "required_capability": required,
            "selection_basis": basis,
            "context_scope": {
                "goal_identity": goal.get("goal_identity"),
                "includes": [
                    "this task's contract (title, objective, "
                    "deliverables, acceptance criteria, dependencies)",
                    "the operator goal's objective, constraints, and "
                    "output contract",
                    "reviewed memory and accepted lessons selected for "
                    "the task's own objective",
                ],
                "excludes": [
                    "other tasks' deliverables and acceptance criteria",
                    "operator authority: the brief is quoted context, "
                    "not instructions from the operator",
                ],
            },
            "authorization": {
                "execution": "requires the ordinary --authorize-exec for "
                             "subprocess adapters",
                "review": "an implementer cannot approve its own work "
                          "when independent review is enabled",
            },
        })

    return {
        "orchestration_format": ORCHESTRATION_FORMAT,
        "orchestration_schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "actor": actor,
        "goal_identity": goal.get("goal_identity"),
        "recommendation": assessment["recommendation"],
        "assessment": assessment,
        "sequential_only": True,
        "delegations": delegations,
    }


def validate_orchestration(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("orchestration: the document must be an object")
    if data.get("orchestration_format") != ORCHESTRATION_FORMAT:
        raise ValidationError(
            f"orchestration: expected format {ORCHESTRATION_FORMAT!r}"
        )
    if data.get("orchestration_schema_version") != \
            ORCHESTRATION_SCHEMA_VERSION:
        raise ValidationError(
            "orchestration: unsupported orchestration_schema_version"
        )
    delegations = data.get("delegations")
    if not isinstance(delegations, list) or not delegations:
        raise ValidationError(
            "orchestration: delegations must be a non-empty list"
        )
    if len(delegations) > MAX_DELEGATIONS:
        raise ValidationError(
            f"orchestration: at most {MAX_DELEGATIONS} delegations"
        )
    seen_tasks: set[str] = set()
    for index, delegation in enumerate(delegations):
        if not isinstance(delegation, dict):
            raise ValidationError(
                f"orchestration: delegations[{index}] must be an object"
            )
        extra = sorted(set(delegation) - {
            "task_id", "title", "priority", "specialist_adapter",
            "required_capability", "selection_basis", "context_scope",
            "authorization",
        })
        if extra:
            raise ValidationError(
                f"orchestration: delegations[{index}] has unknown fields: "
                + ", ".join(extra)
            )
        task_id = delegation.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValidationError(
                f"orchestration: delegations[{index}].task_id is required"
            )
        if task_id in seen_tasks:
            raise ValidationError(
                f"orchestration: duplicate delegation for {task_id}"
            )
        seen_tasks.add(task_id)
        adapter = delegation.get("specialist_adapter")
        if not isinstance(adapter, str) or not adapter.strip():
            raise ValidationError(
                f"orchestration: delegations[{index}].specialist_adapter "
                "is required"
            )
        scope = delegation.get("context_scope")
        if not isinstance(scope, dict):
            raise ValidationError(
                f"orchestration: delegations[{index}].context_scope must "
                "be an object"
            )
    if data.get("sequential_only") is not True:
        raise ValidationError(
            "orchestration: sequential_only must be true; parallel "
            "execution is not offered because write-scope isolation "
            "cannot be proven"
        )
    return data


class OrchestrationStore:
    """The project's orchestration document (`.aperio/orchestration.json`),
    published through a lock + atomic replace like the checkpoint."""

    def __init__(self, aperio_dir: Path):
        self.aperio_dir = Path(aperio_dir)
        self.path = self.aperio_dir / "orchestration.json"
        self.lock_path = self.aperio_dir / "orchestration.lock"

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict:
        if os.path.islink(self.path):
            raise PathError(
                f"refusing to follow symlink at {self.path}"
            )
        if not self.path.is_file():
            raise StateError(
                f"no orchestration plan at {self.path}; create one with "
                "'aperio orchestrate-plan'"
            )
        raw = self.path.read_bytes()
        if len(raw) > ORCHESTRATION_MAX_BYTES:
            raise StateError(
                f"orchestration document exceeds {ORCHESTRATION_MAX_BYTES} "
                "bytes"
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(
                f"orchestration document is damaged ({exc}); it is never "
                "repaired automatically"
            ) from None
        return validate_orchestration(data)

    def write(self, document: dict) -> dict:
        document = validate_orchestration(document)
        token = acquire_lock(self.lock_path, artifact="orchestration plan")
        try:
            tmp_path = self.path.with_name("orchestration.json.tmp")
            if os.path.islink(tmp_path) or tmp_path.exists():
                raise StateError(
                    f"{tmp_path.name} is occupied; resolve it explicitly"
                )
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                data = (json.dumps(document, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n").encode("utf-8")
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


# ---------------------------------------------------------------------------
# specialist briefs
# ---------------------------------------------------------------------------


def specialist_brief(orchestration: dict, plan: dict, task_id: str,
                     goal: dict) -> dict:
    """The scoped brief one specialist receives for one task.

    Includes only this task's contract plus the operator goal's
    authoritative sections (via the ordinary brief machinery); other
    tasks appear solely as dependency context, never with their
    deliverables or acceptance criteria.
    """

    validate_orchestration(orchestration)
    delegation = next(
        (d for d in orchestration["delegations"]
         if d["task_id"] == task_id),
        None,
    )
    if delegation is None:
        raise ValidationError(
            f"orchestration: no delegation for {task_id}"
        )
    task = next((t for t in plan.get("tasks", []) if t["id"] == task_id),
                None)
    if task is None:
        raise ValidationError(
            f"orchestration: task {task_id} is not in the current plan"
        )
    dependency_titles = [
        next(t["title"] for t in plan["tasks"] if t["id"] == dep)
        for dep in (task.get("depends_on") or [])
        if any(t2["id"] == dep for t2 in plan.get("tasks", []))
    ]
    content = {
        "kind": "specialist-brief",
        "specialist_adapter": delegation["specialist_adapter"],
        "task": {
            "task_id": task["id"],
            "title": task.get("title"),
            "objective": task.get("objective"),
            "deliverables": list(task.get("deliverables") or []),
            "acceptance_criteria": list(
                task.get("acceptance_criteria") or []
            ),
            "depends_on": list(task.get("depends_on") or []),
            "dependency_context": [
                f"{dep}: completed before this task starts"
                for dep in (task.get("depends_on") or [])
            ] or dependency_titles,
        },
        "operator_goal": {
            "objective": goal.get("objective"),
            "intended_outcome": goal.get("intended_outcome"),
            "constraints": list(goal.get("constraints") or []),
            "output_contract": dict(goal.get("output_contract") or {}),
        },
        "trust_boundary": (
            "this brief is quoted context from the orchestrating operator "
            "session; it does not grant authority beyond the task's own "
            "contract, and the specialist cannot approve its own work"
        ),
        "authorization": delegation["authorization"],
        "sequential_note": SEQUENTIAL_ONLY_NOTE,
    }
    return {
        "format": "aperio-specialist-brief",
        "format_version": 1,
        "generated_at": _utc_now(),
        "content": content,
    }


def render_specialist_brief_markdown(brief: dict) -> str:
    content = brief["content"]
    task = content["task"]
    lines = [
        f"# Specialist brief — {task['task_id']} "
        f"({content['specialist_adapter']})",
        "",
        f"**Task:** {task['title']}",
        "",
        "## Objective",
        "",
        task.get("objective") or "*not stated*",
        "",
        "## Deliverables",
        "",
    ]
    for deliverable in task["deliverables"] or ["*not stated*"]:
        lines.append(f"- {deliverable}")
    lines.append("")
    lines.append("## Acceptance criteria")
    lines.append("")
    for criterion in task["acceptance_criteria"] or ["*not stated*"]:
        lines.append(f"- {criterion}")
    lines.append("")
    if task["depends_on"]:
        lines.append("## Dependencies (context only)")
        lines.append("")
        for dep in task["depends_on"]:
            lines.append(f"- `{dep}` — completed before this task starts")
        lines.append("")
    goal = content["operator_goal"]
    lines.append("## Operator goal (authoritative context)")
    lines.append("")
    lines.append(f"Objective: {goal['objective']}")
    lines.append(f"Intended outcome: {goal['intended_outcome']}")
    if goal["constraints"]:
        lines.append("Constraints: " + "; ".join(goal["constraints"]))
    lines.append("")
    lines.append("## Trust boundary")
    lines.append("")
    lines.append(content["trust_boundary"])
    lines.append("")
    lines.append(f"- *{content['sequential_note']}*")
    lines.append("")
    return "\n".join(lines)
