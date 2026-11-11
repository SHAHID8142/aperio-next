"""Bounded-task assessment for goals (finding A05).

Aperio's product rule: large project or feature work must be split into
small, independently verifiable execution units. A goal that describes
large work but declares no decomposition must not silently become a single
derived task — that would let one huge, self-certified task stand in for
the whole project.

This module answers one question deterministically:

    Given a goal, is its declared decomposition proportionate to the work
    the goal describes?

The assessment is deliberately a *heuristic over declared text and structure*,
not an understanding of the work. It can therefore say "this looks large and
undecomposed — stop for review". It must never claim to know that a task is
actually atomic in the real world.

Two outcomes matter:

- ``allow``   The goal is either small, or it is large *and* decomposed.
- ``block``   The goal looks large but has no (or too little) decomposition.
              Bridging such a goal into a plan is refused; the operator is
              told exactly what to add.

Genuinely small one-off work (a typo fix, a documentation sentence) stays
``allow`` with no extra steps, so the gate does not punish legitimate atomic
tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that indicate project- or feature-scale work rather than a single
# bounded change. Matched case-insensitively against objective + title.
_LARGE_SCOPE_TERMS = (
    "website", "web site", "webapp", "web app", "application", "app",
    "homepage", "landing page", "hero section",
    "platform", "system", "portal", "storefront", "e-commerce", "ecommerce",
    "marketplace", "dashboard", "cms", "backend", "frontend", "full-stack",
    "fullstack", "api", "service", "microservice", "infrastructure",
    "migration", "redesign", "overhaul", "rewrite", "rebuild", "integrate",
    "integration", "authentication", "auth", "payment", "checkout",
    "onboarding flow", "admin panel", "multi-tenant",
)

# Words indicating a bounded, single-purpose change. When present without any
# large-scope term, the goal is treated as atomic and allowed undecomposed.
_SMALL_SCOPE_TERMS = (
    "typo", "fix a typo", "one-line", "single file", "rename", "bump",
    "add a test", "update a test", "comment", "readme sentence", "docstring",
    "lint", "format", "small fix", "hotfix", "one button", "one label",
)

# Verbs/nouns that describe a *vertical slice* deliverable. Used to judge
# whether declared tasks actually look independently verifiable.
_EVIDENCE_TERMS = (
    "test", "tests", "verify", "verification", "screenshot", "measure",
    "evidence", "assert", "benchmark", "curl", "response", "output",
    "build passes", "build succeeds", "compiles", "lint passes",
)

MAX_TASKS_WITHOUT_EVIDENCE = 1

# A goal is "large" when its objective is long OR it hits a large-scope term.
LARGE_OBJECTIVE_CHARS = 240


@dataclass
class BoundedTaskAssessment:
    """The verdict of a bounded-task assessment.

    ``verdict`` is one of ``allow`` or ``block``. ``reason`` is human text for
    the operator. ``signals`` are the concrete findings that drove the verdict
    so the operator can act on them rather than guess.
    """

    verdict: str
    reason: str
    signals: list[str] = field(default_factory=list)
    inferred_scale: str = "unknown"  # small | large | unknown
    task_count: int = 0
    tasks_with_evidence: int = 0

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "signals": list(self.signals),
            "inferred_scale": self.inferred_scale,
            "task_count": self.task_count,
            "tasks_with_evidence": self.tasks_with_evidence,
        }


def _mentions(text: str, terms: tuple[str, ...]) -> list[str]:
    lowered = text.casefold()
    hits = []
    for term in terms:
        # Word-ish boundary match so "app" does not match "happen".
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            hits.append(term)
    return hits


def _task_has_evidence(task: dict) -> bool:
    """Whether a declared task carries something a reviewer could check."""
    blob_parts = [
        str(task.get("title") or ""),
        str(task.get("action") or ""),
    ]
    blob_parts.extend(str(c) for c in (task.get("completion_criteria") or []))
    blob_parts.extend(str(d) for d in (task.get("deliverables") or []))
    return bool(_mentions(" ".join(blob_parts), _EVIDENCE_TERMS))


def assess_goal_boundedness(goal: dict) -> BoundedTaskAssessment:
    """Classify whether a goal's decomposition matches its apparent scale.

    The function is pure and reads only the goal document.
    """
    objective = str(goal.get("objective") or "")
    title = str(goal.get("title") or "")
    combined = f"{title} {objective}".strip()

    large_hits = _mentions(combined, _LARGE_SCOPE_TERMS)
    small_hits = _mentions(combined, _SMALL_SCOPE_TERMS)

    work = goal.get("work") or {}
    tasks = list(work.get("tasks") or [])
    task_count = len(tasks)
    tasks_with_evidence = sum(1 for t in tasks if _task_has_evidence(t))

    # Scale inference. An explicit small-scope signal wins over a long
    # objective, so "fix a typo in the long README paragraph" stays small.
    if small_hits and not large_hits:
        scale = "small"
    elif large_hits or len(objective) >= LARGE_OBJECTIVE_CHARS:
        scale = "large"
    else:
        scale = "unknown"

    signals: list[str] = []
    if large_hits:
        signals.append("project/feature-scale terms: " + ", ".join(sorted(set(large_hits))[:6]))

    # A decomposition exists on paper but one (or more) of its tasks is itself
    # a whole-project task. Check declared tasks before trusting the count.
    oversized_tasks: list[str] = []
    for index, task in enumerate(tasks, start=1):
        is_oversized, task_signals = assess_task_boundeness(task)
        if is_oversized:
            label = task.get("id") or f"tasks[{index}]"
            oversized_tasks.extend(f"{label}: {s}" for s in task_signals)
    if oversized_tasks:
        return BoundedTaskAssessment(
            verdict="block",
            reason=(
                "this goal is decomposed, but at least one declared task is a "
                "whole-project task. Split that task into small, independently "
                "verifiable units and bridge it again."
            ),
            signals=signals + oversized_tasks,
            inferred_scale="large",
            task_count=task_count,
            tasks_with_evidence=tasks_with_evidence,
        )

    if scale == "small":
        return BoundedTaskAssessment(
            verdict="allow",
            reason="bounded change; decomposition is optional",
            signals=signals,
            inferred_scale="small",
            task_count=task_count,
            tasks_with_evidence=tasks_with_evidence,
        )

    if scale == "unknown":
        # Not clearly large. Without a decomposition this becomes one derived
        # task; that is acceptable for genuinely small work but the operator
        # should still see the honest note (emitted by planbridge).
        return BoundedTaskAssessment(
            verdict="allow",
            reason="scope is not clearly project-scale; a single task is acceptable",
            signals=signals,
            inferred_scale="unknown",
            task_count=task_count,
            tasks_with_evidence=tasks_with_evidence,
        )

    # scale == "large"
    if task_count >= 2 and tasks_with_evidence >= min(task_count, 2):
        return BoundedTaskAssessment(
            verdict="allow",
            reason=(
                f"large goal decomposed into {task_count} tasks, "
                f"{tasks_with_evidence} carrying verification evidence"
            ),
            signals=signals,
            inferred_scale="large",
            task_count=task_count,
            tasks_with_evidence=tasks_with_evidence,
        )

    if task_count >= 2 and tasks_with_evidence < 2:
        signals.append(
            f"{task_count} tasks declared but fewer than 2 state verifiable "
            "evidence (tests, measurements, screenshots)"
        )
    if task_count <= 1:
        signals.append(
            "no work decomposition declared; a single derived task would "
            "represent project-scale work"
        )

    return BoundedTaskAssessment(
        verdict="block",
        reason=(
            "this goal describes project- or feature-scale work without a "
            "proportionate decomposition. Split it into small, independently "
            "verifiable tasks (each with completion criteria) and bridge it "
            "again, or narrow the goal to a single bounded change."
        ),
        signals=signals,
        inferred_scale="large",
        task_count=task_count,
        tasks_with_evidence=tasks_with_evidence,
    )


# ---------------------------------------------------------------------------
# Task-level and plan-level boundedness.
#
# The goal-level assessment above answers "is this goal decomposed enough?".
# These two answer the same question for a plan that is created or approved
# directly, and for the individual tasks in it. They are deliberately
# conservative so that legitimate slices (a Hero section, "build the
# frontend", "configure the CMS") stay valid while a whole-product task
# ("build an entire website") is refused.
# ---------------------------------------------------------------------------

#: A single atomic task's objective should not be a document-sized blob.
MAX_ATOMIC_TASK_CHARS = 1000

#: Whole-product objects. Component slices (frontend, backend, cms, api,
#: database) are intentionally absent: they are valid atomic units of a
#: larger, already-decomposed goal.
_COMPOUND_PROJECT_OBJECTS = (
    "website", "web site", "webapp", "web app", "application", "app",
    "system", "platform", "product", "service", "site", "homepage",
)

_COMPOUND_PREFIX_RE = re.compile(
    r"(?i)\b(?:entire|whole|complete|full|from\s+scratch|build\s+(?:a|an|the))\b"
)


def _task_text(task: dict) -> str:
    parts = [str(task.get("title") or ""), str(task.get("action") or "")]
    parts.append(str(task.get("objective") or ""))
    parts.extend(str(d) for d in (task.get("deliverables") or []))
    parts.extend(str(c) for c in (task.get("completion_criteria") or []))
    return " ".join(parts)


def assess_task_boundeness(task: dict) -> tuple[bool, list[str]]:
    """Whether one task looks like a whole project instead of a unit of work.

    Returns ``(oversized, signals)``.
    """
    signals: list[str] = []
    text = _task_text(task)

    # A whole-product object preceded (within a short window) by a
    # "whole/complete/entire/build a ..." marker means the task is the project.
    for match in _COMPOUND_PREFIX_RE.finditer(text):
        window = text[match.end(): match.end() + 48]
        hit = _mentions(window, _COMPOUND_PROJECT_OBJECTS)
        if hit:
            signals.append(
                f"task describes a whole project ({match.group(0).strip()} … {hit[0]})"
            )
            break

    objective = str(task.get("objective") or task.get("action") or "")
    if len(objective) > MAX_ATOMIC_TASK_CHARS:
        signals.append(
            f"task objective is {len(objective)} characters (limit "
            f"{MAX_ATOMIC_TASK_CHARS}); split it into smaller units"
        )
    return bool(signals), signals


def assess_plan_boundeness(plan: dict) -> BoundedTaskAssessment:
    """Whether a plan's task list is made of bounded, independently verifiable work.

    Used by plan-create, plan-approve, and execution eligibility so the rule
    cannot be bypassed by building a plan directly instead of bridging a goal.
    A plan whose tasks are all legitimate slices is allowed; a plan that is
    one (or more) whole-project tasks is blocked.
    """
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return BoundedTaskAssessment(
            verdict="block",
            reason=(
                "a plan must contain at least one bounded task; an empty plan "
                "cannot represent decomposable work"
            ),
            signals=["no tasks"],
            inferred_scale="unknown",
            task_count=0,
            tasks_with_evidence=0,
        )

    oversized: list[str] = []
    for index, task in enumerate(tasks, start=1):
        is_oversized, signals = assess_task_boundeness(task)
        if is_oversized:
            label = task.get("id") or f"tasks[{index}]"
            oversized.extend(f"{label}: {s}" for s in signals)

    if oversized:
        return BoundedTaskAssessment(
            verdict="block",
            reason=(
                "the plan contains task(s) that describe a whole project rather "
                "than a bounded unit of work. Split each into small, "
                "independently verifiable tasks before creating or approving "
                "the plan."
            ),
            signals=oversized,
            inferred_scale="large",
            task_count=len(tasks),
            tasks_with_evidence=0,
        )
    return BoundedTaskAssessment(
        verdict="allow",
        reason=f"{len(tasks)} bounded task(s)",
        signals=[],
        inferred_scale="small",
        task_count=len(tasks),
        tasks_with_evidence=0,
    )
