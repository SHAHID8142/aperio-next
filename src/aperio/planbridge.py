"""Goal-to-plan bridge: from a descriptive work breakdown to a real plan.

A goal's ``work.tasks`` decomposition is **descriptive input for a
reader** (M2) — it is never an approved executable plan, and nothing in
this module pretends otherwise. The bridge does one narrow thing:
convert a validated goal into a *plan proposal* in the exact plan-schema
input shape, so that creating the plan goes through the normal
``plan-create`` path with its approval gate, revision rules, evidence
lifecycle, and (when governance is on) independent review — unchanged.

Design rules:

* **One direction, no duplication.** The bridge emits plan input; it
  does not create a second planning system, does not write
  ``plan.json`` itself unless the caller explicitly passes ``--create``
  (which calls the ordinary ``create_plan``, producing a **draft** plan
  that still requires approval before any execution).
* **Ids are re-numbered, order is preserved.** Goal work-task ids are
  free-form (``collect``, ``draft-1``); plan tasks must be
  ``TASK-<digits>``. The bridge assigns ``TASK-001…`` in the goal's
  declared order and rewrites ``depends_on`` through the same mapping,
  recording the mapping in the proposal so a reviewer can trace it.
* **Nothing is invented.** Missing goal fields become explicit
  ``missing`` entries in the proposal, not defaults. A goal without a
  work decomposition yields a single-task proposal built from the
  objective, clearly labeled as derived from the goal rather than
  decomposed by Aperio.
* **Authority follows provenance.** Only an operator-declared goal may
  be bridged; agent/import-declared goals are refused with an
  actionable message (review and re-declare first — the same rule as
  agent briefs). The plan the operator creates is theirs; the bridge
  never silently mutates an approved plan, and existing plan-revision
  rules continue to govern any later change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aperio.errors import ValidationError
from aperio.goals import goal_authority, goal_identity

PROPOSAL_FORMAT = "aperio-plan-proposal"
PROPOSAL_VERSION = 1
MAX_PROPOSAL_BYTES = 128 * 1024
MAX_PROPOSED_TASKS = 200

_GOAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _check_text(value: object, where: str, *, max_bytes: int,
                single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be non-empty text")
    if "\x00" in value:
        raise ValidationError(f"{where} must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(
            f"{where} must be at most {max_bytes} UTF-8 bytes"
        )
    if single_line and "\n" in value:
        raise ValidationError(f"{where} must be single-line")
    return value


def plan_proposal_from_goal(goal: dict, *, actor: str | None = None) -> dict:
    """Build a plan proposal document from a validated goal. Pure.

    The result embeds the plan-create input under ``plan_input``; the
    caller reviews it, stores it, or creates the plan through the
    ordinary gated path.
    """

    if goal_authority(goal) != "operator":
        raise ValidationError(
            "plan-bridge: only an operator-declared goal can be bridged "
            f"into a plan proposal; this goal's declared source is "
            f"{goal['provenance']['source']!r}. Review its instructions "
            "and re-declare the goal with provenance source 'operator' "
            "first"
        )
    work = goal.get("work") or {}
    goal_tasks = list(work.get("tasks") or [])

    if len(goal_tasks) > MAX_PROPOSED_TASKS:
        raise ValidationError(
            f"plan-bridge: the goal decomposes into {len(goal_tasks)} "
            f"tasks; at most {MAX_PROPOSED_TASKS} can be bridged"
        )

    id_map: dict[str, str] = {}
    for index, task in enumerate(goal_tasks, start=1):
        id_map[task["id"]] = f"TASK-{index:03d}"

    mapped_tasks: list[dict] = []
    for index, task in enumerate(goal_tasks, start=1):
        description_parts = [
            part for part in (task.get("action"),) if part
        ]
        deliverables = [
            _check_text(d, f"work task {task['id']!r} deliverable",
                        max_bytes=200, single_line=True)
            for d in (task.get("deliverables") or [])
        ] or ["Completed work for " + task["id"]]
        objective = _check_text(
            description_parts[0] if description_parts else task.get("title")
            or task["id"],
            f"work task {task['id']!r} action",
            max_bytes=4 * 1024,
        )
        acceptance = [
            _check_text(c, f"work task {task['id']!r} criterion",
                        max_bytes=200, single_line=True)
            for c in (task.get("completion_criteria") or [])
        ] or [_check_text(
            "The task's stated completion criteria hold; recorded "
            "verification evidence is required at completion.",
            f"work task {task['id']!r} default criterion",
            max_bytes=300, single_line=True,
        )]
        mapped_tasks.append({
            "id": id_map[task["id"]],
            "title": _check_text(task.get("title") or task["id"],
                                 f"work task {task['id']!r} title",
                                 max_bytes=200, single_line=True),
            "objective": objective,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
            "depends_on": sorted({
                id_map[dep]
                for dep in (task.get("depends_on") or [])
                if dep in id_map
            }),
        })

    if mapped_tasks:
        objective = goal["objective"]
        expected_outcome = goal["intended_outcome"]
        decomposition_note = (
            f"bridged from the goal's work decomposition "
            f"({len(mapped_tasks)} task(s)); ids re-numbered in declared "
            "order"
        )
    else:
        objective = goal["objective"]
        expected_outcome = goal["intended_outcome"]
        mapped_tasks = [{
            "id": "TASK-001",
            "title": _check_text(
                objective if len(objective) <= 100 else objective[:97] + "…",
                "goal objective as task title", max_bytes=200,
                single_line=True,
            ),
            "objective": objective,
            "deliverables": [
                _check_text(d, "goal deliverable", max_bytes=200,
                            single_line=True)
                for d in (goal.get("deliverables") or [])
            ] or ["The goal's intended outcome, delivered and verified."],
            "acceptance_criteria": [
                _check_text(c, "goal acceptance criterion", max_bytes=200,
                            single_line=True)
                for c in (goal.get("acceptance_criteria") or [])
            ] or ["The goal's intended outcome is achieved and verified "
                  "with recorded evidence."],
            "depends_on": [],
        }]
        decomposition_note = (
            "the goal declares no work decomposition; a single task was "
            "derived from the objective (Aperio did not decompose it)"
        )

    plan_input = {
        "objective": objective,
        "expected_outcome": expected_outcome,
        "scope": [
            _check_text(c, "goal constraint as scope", max_bytes=200,
                        single_line=True)
            for c in (goal.get("constraints") or [])
        ],
        "exclusions": [],
        "assumptions": list(goal.get("assumptions") or []),
        "risks": list(goal.get("risks") or []),
        "strategy": "small",
        "acceptance_criteria": [
            _check_text(c, "goal acceptance criterion", max_bytes=200,
                        single_line=True)
            for c in (goal.get("acceptance_criteria") or [])
        ] or ["The goal's intended outcome is achieved and verified."],
        "verification_strategy": (
            "; ".join(goal.get("evidence_requirements") or [])
            or "Per-task verification evidence recorded at completion"
        ),
        "tasks": mapped_tasks,
    }

    missing: list[str] = []
    if not goal.get("acceptance_criteria"):
        missing.append(
            "goal.acceptance_criteria: the plan uses a stated default "
            "criterion; replace it with real acceptance criteria"
        )
    if not goal.get("evidence_requirements"):
        missing.append(
            "goal.evidence_requirements: no evidence strategy was "
            "declared; the plan's verification strategy is a default"
        )
    if not goal_tasks:
        missing.append(
            "goal.work.tasks: no decomposition; the single task came from "
            "the objective and should be reviewed or decomposed"
        )

    proposal = {
        "proposal_format": PROPOSAL_FORMAT,
        "proposal_version": PROPOSAL_VERSION,
        "goal_identity": goal_identity(goal),
        "goal_sha256": goal.get("goal_sha256"),
        "created_by": actor,
        "authority": "operator",
        "decomposition_note": decomposition_note,
        "id_mapping": {source: mapped for source, mapped in id_map.items()},
        "plan_input": plan_input,
        "missing": missing,
        "warnings": [
            "a proposal is not a plan: creating the plan produces a DRAFT "
            "that still requires explicit approval before any execution",
            "the descriptive work breakdown came from the operator's "
            "goal; Aperio did not validate it as executable sequencing "
            "beyond the dependency-graph rules",
        ],
    }
    encoded = json.dumps(proposal, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValidationError(
            f"plan-bridge: the proposal exceeds {MAX_PROPOSAL_BYTES} "
            "bytes; decompose the goal into smaller units"
        )
    return proposal


def load_proposal_file(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the plan proposal file"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read plan proposal: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise ValidationError(
            f"plan proposal must be at most {MAX_PROPOSAL_BYTES} UTF-8 "
            "bytes"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"plan proposal is not valid UTF-8 JSON: {exc}"
        ) from None
    if not isinstance(data, dict) or data.get("proposal_format") != \
            PROPOSAL_FORMAT:
        raise ValidationError(
            f"plan proposal must be a {PROPOSAL_FORMAT!r} document"
        )
    if data.get("proposal_version") != PROPOSAL_VERSION:
        raise ValidationError(
            "plan proposal has unsupported proposal_version "
            f"{data.get('proposal_version')!r}"
        )
    if not isinstance(data.get("plan_input"), dict):
        raise ValidationError("plan proposal has no plan_input object")
    return data


def render_proposal_markdown(proposal: dict) -> str:
    lines = ["# Plan proposal", ""]
    lines.append(f"Goal: `{proposal['goal_identity']}` — "
                 f"{proposal['decomposition_note']}")
    lines.append("")
    lines.append("This is a **proposal**, not a plan. Creating the plan "
                 "produces a DRAFT that requires explicit approval before "
                 "any execution.")
    lines.append("")
    plan_input = proposal["plan_input"]
    lines.append(f"Objective: {plan_input['objective']}")
    lines.append(f"Expected outcome: {plan_input['expected_outcome']}")
    lines.append("")
    lines.append("## Proposed tasks")
    lines.append("")
    lines.append("| Id | Title | Priority | Depends on |")
    lines.append("| --- | --- | --- | --- |")
    for task in plan_input["tasks"]:
        lines.append(
            f"| `{task['id']}` | {task['title']} | "
            f"{task.get('priority', 'normal')} | "
            + (", ".join(f"`{d}`" for d in task["depends_on"]) or "—")
        )
    lines.append("")
    if proposal["id_mapping"]:
        lines.append("Id mapping (goal task → plan task): "
                     + ", ".join(
                         f"`{source}`→`{mapped}`"
                         for source, mapped in sorted(
                             proposal["id_mapping"].items())
                     ))
        lines.append("")
    if proposal["missing"]:
        lines.append("## Missing information (Aperio did not invent it)")
        lines.append("")
        for entry in proposal["missing"]:
            lines.append(f"- {entry}")
        lines.append("")
    for warning in proposal["warnings"]:
        lines.append(f"- *{warning}*")
    lines.append("")
    return "\n".join(lines)
