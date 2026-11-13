"""Consolidated work-status view (work-status v1) and the read-only UI feed.

One derived document answering the operator's standing question — "where
am I, what's waiting, what needs my decision?" — from durable state
only:

* current work (from the checkpoint/plan: active task, approval state);
* brain-dump items pending review, ideas noted, conflicts preserved;
* approval requests (pending independent reviews, when governance is
  on) and blockers;
* execution status (active runs, last outcomes) and verification
  evidence (plan-lifecycle recorded evidence — never agent claims);
* recovery status (checkpoint freshness and resume eligibility);
* available agents (adapter registry) and approved skills (from an
  optional skill store).

The document is read-only and derived; nothing here mutates state. It
feeds both the `aperio work-status` CLI rendering and the optional
read-only loopback UI (`aperio serve-ui`), which exposes exactly this
document over HTTP with no write endpoints.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from aperio.checkpoint import recover_report
from aperio.errors import StateError, ValidationError
from aperio.intake import IntakeLog
from aperio.planstore import read_plan
from aperio.runtime import read_executions

WORK_STATUS_FORMAT = "aperio-work-status"
WORK_STATUS_VERSION = 1


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_work_status(aperio_dir: Path, *,
                      skill_store: Path | None = None) -> dict:
    """Derive the consolidated work-status document. Read-only."""

    recovery = recover_report(aperio_dir)
    intake = IntakeLog(aperio_dir).read()
    resolved_keys = {
        (r["capture_id"], r["item_index"]) for r in intake["resolves"]
    }

    pending_items: list[dict] = []
    ideas: list[dict] = []
    conflicts: list[dict] = []
    urgent: list[dict] = []
    for capture_event in intake["captures"]:
        for index, item in enumerate(capture_event.get("items", [])):
            if (capture_event["capture_id"], index) in resolved_keys:
                continue
            entry = {
                "capture_id": capture_event["capture_id"],
                "item_index": index,
                "text": item["text"],
                "category": item["category"],
                "routing": item["routing"],
                "classification_basis": item["classification_basis"],
                "authority": item["authority"],
            }
            if capture_event.get("operator_urgent"):
                urgent.append(entry)
            if item["routing"] == "idea-note":
                ideas.append(entry)
            if item["category"] == "correction":
                conflicts.append(entry)
            pending_items.append(entry)

    plan = None
    plan_error = None
    try:
        plan = read_plan(aperio_dir)
    except StateError as exc:
        plan_error = str(exc)
    approval_requests: list[dict] = []
    reviews_summary = {"present": False}
    reviews_path = aperio_dir / "reviews.json"
    if reviews_path.exists():
        try:
            reviews_doc = json.loads(
                reviews_path.read_text(encoding="utf-8")
            )
            reviews_summary = {"present": True}
            pending_reviews = [
                r for r in reviews_doc.get("reviews", [])
                if r.get("outcome") == "pending"
            ]
            for review in pending_reviews:
                approval_requests.append({
                    "review_id": review.get("review_id"),
                    "task_id": (review.get("target") or {}).get("task_id"),
                    "submitted_by": review.get("submitted_by"),
                })
        except (json.JSONDecodeError, UnicodeDecodeError):
            reviews_summary = {"present": True, "damaged": True}

    executions = None
    active_runs: list[dict] = []
    last_outcomes: list[dict] = []
    try:
        executions = read_executions(aperio_dir)
    except StateError:
        executions = None
    if executions:
        for run in executions.get("runs", []):
            entry = {
                "run_id": run.get("run_id"),
                "task_id": run.get("task_id"),
                "status": run.get("status"),
                "adapter": run.get("adapter_id"),
            }
            if run.get("status") in ("running", "awaiting-external",
                                     "prepared"):
                active_runs.append(entry)
            elif run.get("status") in ("completed", "failed", "cancelled",
                                       "interrupted"):
                last_outcomes.append(entry)
    last_outcomes.sort(key=lambda r: r.get("run_id") or "", reverse=True)
    last_outcomes = last_outcomes[:10]

    from aperio.adapters import builtin_adapters

    agents = []
    for adapter in builtin_adapters():
        available, reason = adapter.available()
        agents.append({
            "id": adapter.id,
            "available": bool(available) or adapter.id == "manual",
            "note": None if available else (reason or "")[:160],
        })

    skills: list[dict] = []
    skill_store_error = None
    if skill_store is not None and Path(skill_store).exists():
        from aperio.skills import SkillStore

        try:
            for skill in SkillStore(skill_store).list_skills():
                skills.append({
                    "name": skill["name"],
                    "version": skill["version"],
                    "scope": skill["scope"],
                    "state": skill["state"]["state"],
                })
        except (StateError, ValidationError) as exc:
            skill_store_error = str(exc)[:200]

    verified = recovery.get("verified_completed", []) if "verified_completed" in recovery else []
    content = {
        "kind": "work-status",
        "generated_at": _utc_now(),
        "current_work": {
            "active_task": recovery.get("active_task"),
            "active_runs": active_runs,
            "approval_status": (plan or {}).get("approval", {}).get(
                "status"
            ) if plan else None,
            "plan_damaged": plan_error is not None,
            "next_action": recovery.get(
                "next_recommended_action",
                "no plan present: capture or plan work first",
            ),
        },
        "brain_dump": {
            "pending_items": pending_items,
            "urgent": urgent,
            "ideas": ideas,
            "possible_conflicts": conflicts,
            "honesty_note": (
                "classification is deterministic keyword triage, not "
                "language understanding"
            ),
        },
        "approvals": {
            "pending_reviews": approval_requests,
            "reviews_file": reviews_summary,
        },
        "execution": {
            "last_outcomes": last_outcomes,
            "verification_evidence": verified,
            "unverified_signals": recovery.get("unverified_signals", []),
        },
        "recovery": {
            "status": recovery.get("status", "no-checkpoint"),
            "resume_eligible": recovery.get(
                "resume_eligibility", {}
            ).get("eligible", False),
            "reasons": recovery.get("resume_eligibility", {}).get(
                "reasons", []
            ),
        },
        "agents": agents,
        "skills": skills,
        "skill_store_error": skill_store_error,
    }
    return {
        "format": WORK_STATUS_FORMAT,
        "format_version": WORK_STATUS_VERSION,
        "content": content,
    }


def render_work_status_markdown(document: dict) -> str:
    content = document["content"]
    work = content["current_work"]
    lines = ["# Work status", ""]
    if work["active_task"]:
        lines.append(
            f"**Current task:** {work['active_task']['task_id']} — "
            f"{work['active_task'].get('title') or ''}"
        )
    else:
        lines.append("**Current task:** none")
    if work["active_runs"]:
        lines.append(
            "Active runs: " + ", ".join(
                f"`{r['run_id']}` ({r['status']})" for r in work["active_runs"]
            )
        )
    lines.append(f"Plan approval: **{work['approval_status']}**"
                 if work["approval_status"] else "No plan present.")
    lines.append(f"Next: {work['next_action']}")
    lines.append("")

    brain = content["brain_dump"]
    lines.append("## Brain dump (pending review)")
    lines.append("")
    if brain["urgent"]:
        lines.append("**Operator-urgent items** (checkpointed):")
        for item in brain["urgent"]:
            lines.append(f"- `{item['capture_id']}[{item['item_index']}]` "
                         f"{item['text'][:100]}")
    if brain["pending_items"]:
        for item in brain["pending_items"]:
            lines.append(
                f"- `{item['capture_id']}[{item['item_index']}]` "
                f"{item['category']} → {item['routing']}: "
                f"{item['text'][:100]}"
            )
        lines.append("")
        lines.append(f"*{brain['honesty_note']}*")
    else:
        lines.append("Nothing pending.")
    lines.append("")

    approvals = content["approvals"]
    if approvals["pending_reviews"]:
        lines.append("## Awaiting independent review")
        lines.append("")
        for review in approvals["pending_reviews"]:
            lines.append(
                f"- `{review['review_id']}` on {review['task_id']} "
                f"(submitted by {review.get('submitted_by')})"
            )
        lines.append("")

    execution = content["execution"]
    if execution["verification_evidence"]:
        lines.append("## Verified completions (recorded evidence)")
        lines.append("")
        for task in execution["verification_evidence"]:
            lines.append(
                f"- `{task['task_id']}` — evidence: {task.get('evidence')}"
            )
        lines.append("")
    if execution["unverified_signals"]:
        lines.append("Unverified signals (not evidence):")
        for signal in execution["unverified_signals"]:
            lines.append(f"- {signal}")
        lines.append("")
    if execution["last_outcomes"]:
        lines.append("Recent run outcomes:")
        lines.append("")
        for run in execution["last_outcomes"]:
            lines.append(
                f"- `{run['run_id']}` {task_id_of(run)} — {run['status']}"
            )
        lines.append("")

    lines.append("## Recovery")
    lines.append("")
    lines.append(
        f"- checkpoint: **{content['recovery']['status']}**; resume: "
        f"**{'eligible' if content['recovery']['resume_eligible'] else 'gated'}**"
    )
    for reason in content["recovery"]["reasons"]:
        lines.append(f"  - {reason}")
    lines.append("")

    lines.append("## Agents and skills")
    lines.append("")
    for agent in content["agents"]:
        note = f" ({agent['note']})" if agent["note"] else ""
        lines.append(
            f"- `{agent['id']}` — "
            f"{'available' if agent['available'] else 'unavailable'}"
            f"{note}"
        )
    for skill in content["skills"]:
        lines.append(
            f"- skill `{skill['name']}` v{skill['version']} "
            f"[{skill['state']}]"
        )
    if content.get("skill_store_error"):
        lines.append(f"- skill store error: {content['skill_store_error']}")
    lines.append("")
    return "\n".join(lines)


def task_id_of(run: dict) -> str:
    return f"task {run.get('task_id') or '?'}"
