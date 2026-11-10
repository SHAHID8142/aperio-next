"""Onboarding readiness gate and structured report (onboarding-report v1).

Step 01 ends with an explicit completion evaluation (directive Rule 17),
not with a vague "all done". The gate is deterministic and derives every
conclusion from recorded session state:

- ``CANCELLED``          The operator explicitly cancelled onboarding.
- ``BLOCKED``            An open critical blocker (information, security,
                         or required-tool) prevents safe progression.
- ``IN_PROGRESS``        More required onboarding remains (questions
                         outstanding, stage not advanced past interview).
- ``READY_WITH_PENDING`` The next phase can safely proceed; nonblocking
                         decisions or optional setup remain, each recorded
                         with its severity.
- ``READY``              Required coverage is answered, no critical
                         issues, no open blockers.

READY is NOT permission to start executing development tasks: it means
onboarding is sufficiently complete for the next *authorized* phase.
Nothing in this module starts any such phase.

The report covers the twenty mandated content areas (Rule 18) and keeps
the honesty boundaries visible: documented tasks stay claims, tool
readiness distinguishes installed from verified, and unresolved
uncertainties are listed, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from aperio.onboarding_session import (
    SEVERITY_CRITICAL,
    SEVERITY_OPTIONAL,
    SEVERITY_PENDING_DECISION,
    SEVERITY_REQUIRED_BEFORE_ACTION,
    STAGE_CANCELLED,
    STAGE_COMPLETE,
    STAGE_DISCOVERY,
    STAGE_INTENT,
    read_session,
)

REPORT_FORMAT = "aperio-onboarding-report"
REPORT_VERSION = 1

OUTCOME_CANCELLED = "CANCELLED"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_IN_PROGRESS = "IN_PROGRESS"
OUTCOME_READY_WITH_PENDING = "READY_WITH_PENDING"
OUTCOME_READY = "READY"

OUTCOMES = (
    OUTCOME_CANCELLED,
    OUTCOME_BLOCKED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_READY_WITH_PENDING,
    OUTCOME_READY,
)


@dataclass(frozen=True)
class ReadinessResult:
    """The gate's verdict plus the reasons that produced it."""

    outcome: str
    reasons: tuple[str, ...]
    open_critical: tuple[str, ...]
    open_blockers: tuple[str, ...]
    open_pending: tuple[str, ...]
    outstanding_required: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "open_critical": list(self.open_critical),
            "open_blockers": list(self.open_blockers),
            "open_pending": list(self.open_pending),
            "outstanding_required": list(self.outstanding_required),
        }


def evaluate_readiness(session: dict, interview_progress: dict | None = None) -> ReadinessResult:
    """Evaluate the gate from validated session state (deterministic)."""
    reasons: list[str] = []

    if session.get("cancelled"):
        return ReadinessResult(
            outcome=OUTCOME_CANCELLED,
            reasons=("operator explicitly cancelled onboarding",),
            open_critical=(),
            open_blockers=(),
            open_pending=(),
            outstanding_required=(),
        )

    unresolved = [u for u in session.get("unresolved", []) if u.get("resolved_at") is None]
    open_critical = tuple(u["topic"] for u in unresolved if u["severity"] == SEVERITY_CRITICAL)
    open_required = tuple(
        u["topic"] for u in unresolved if u.get("severity") == SEVERITY_REQUIRED_BEFORE_ACTION
    )
    open_pending = tuple(
        u["topic"]
        for u in unresolved
        if u.get("severity") in (SEVERITY_PENDING_DECISION, SEVERITY_OPTIONAL)
    )
    blockers = tuple(
        b["description"] for b in session.get("blockers", []) if b.get("resolved_at") is None
    )

    if open_critical:
        reasons.append(
            "open critical blockers: " + ", ".join(open_critical)
        )
    if blockers:
        reasons.append("open blockers recorded: " + "; ".join(blockers[:3]))

    if interview_progress is None:
        from aperio.onboarding_interview import interview_progress as _ip

        interview_progress = _ip(session, session.get("interview_profile") or "general")
    outstanding_required = tuple(interview_progress.get("outstanding_required", []))

    stage = session.get("stage", STAGE_DISCOVERY)
    early_stage = stage in (STAGE_DISCOVERY, STAGE_INTENT)

    if open_critical or blockers:
        outcome = OUTCOME_BLOCKED
    elif early_stage or outstanding_required:
        reasons.append("required onboarding remains (stage or required questions)")
        outcome = OUTCOME_IN_PROGRESS
    elif open_required:
        reasons.append(
            "required-before-action items remain open: " + ", ".join(open_required)
        )
        outcome = OUTCOME_READY_WITH_PENDING
    elif open_pending:
        reasons.append("nonblocking decisions remain open: " + ", ".join(open_pending))
        outcome = OUTCOME_READY_WITH_PENDING
    else:
        outcome = OUTCOME_READY
        reasons.append("required coverage answered; no critical issues or open blockers")

    return ReadinessResult(
        outcome=outcome,
        reasons=tuple(reasons),
        open_critical=open_critical,
        open_blockers=blockers,
        open_pending=open_pending,
        outstanding_required=outstanding_required,
    )


# ---------------------------------------------------------------------------
# Structured report (Rule 18 content areas).
# ---------------------------------------------------------------------------

def _answers_by_category(session: dict) -> dict[str, list[dict]]:
    from aperio.onboarding_interview import get_question

    buckets: dict[str, list[dict]] = {}
    for qid, entry in session.get("answers", {}).items():
        try:
            category = get_question(qid).category
        except Exception:
            category = "other"
        buckets.setdefault(category, []).append(
            {
                "question": qid,
                "answer": entry.get("answer"),
                "kind": entry.get("kind"),
            }
        )
    return buckets


def _map_block(reconstruction, tools_report, recommendations) -> dict:
    return {
        "reconstruction": reconstruction.to_dict() if reconstruction else None,
        "tools": tools_report.to_dict() if tools_report else None,
        "recommendations": list(recommendations or []),
    }


def build_report(
    session: dict,
    *,
    discovery=None,
    reconstruction=None,
    tools_report=None,
    recommendations=None,
    readiness: ReadinessResult | None = None,
) -> dict:
    """Assemble the 20-area onboarding report from validated inputs.

    Missing optional inputs (discovery/reconstruction/tools) are reported
    as ``None`` sections rather than fabricated.
    """
    if readiness is None:
        readiness = evaluate_readiness(session)
    answers = _answers_by_category(session)

    def _category_answer(qid: str):
        entry = session.get("answers", {}).get(qid)
        return entry.get("answer") if entry else None

    reconstruction_dict = reconstruction.to_dict() if reconstruction is not None else None
    pending_tasks = []
    implementation_status = {
        "aperio_plan_tasks": [],
        "documented_complete_claims": [],
        "note": "documented claims are not verified implementation status",
    }
    if reconstruction_dict:
        pending_tasks = reconstruction_dict.get("pending_tasks", [])
        implementation_status = {
            "aperio_plan_tasks": reconstruction_dict.get("aperio_plan_tasks", []),
            "documented_complete_claims": reconstruction_dict.get("completed_claims", []),
            "note": reconstruction_dict["limitations"][0],
        }

    verified_tools = []
    if tools_report is not None:
        verified_tools = [
            t.to_dict()
            for t in tools_report.executables
            if t.status == "verified_by_probe"
        ]

    next_actions = _suggest_next_actions(readiness, session)

    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "generated_at": _utc_now(),
        "1_project_overview": {
            "workspace": session.get("workspace"),
            "classification": session.get("classification"),
            "classification_reasons": session.get("classification_reasons", []),
            "project_category": session.get("project_category"),
            "session_id": session.get("session_id"),
        },
        "2_objective_and_outcome": {
            "intent": session.get("intent"),
            "goal": _category_answer("foundation.goal"),
            "success_criteria": _category_answer("foundation.success"),
        },
        "3_product_requirements": answers.get("product", []),
        "4_constraints": [
            a for a in answers.get("foundations", []) + answers.get("business", [])
        ],
        "5_platform_and_technical": answers.get("technical", []),
        "6_ui_ux_requirements": answers.get("design", []),
        "7_architecture_decisions": [
            {
                "id": d["id"],
                "title": d["title"],
                "text": d["text"],
                "source": d["source"],
            }
            for d in session.get("decisions", [])
        ],
        "8_quality_criteria": answers.get("quality", []),
        "9_confirmed_operator_decisions": [
            {
                "id": d["id"],
                "title": d["title"],
                "source": d["source"],
            }
            for d in session.get("decisions", [])
        ],
        "10_known_assumptions": [
            {"question": qid, "answer": entry.get("answer")}
            for qid, entry in session.get("answers", {}).items()
            if entry.get("kind") in ("unsure", "deferred")
        ],
        "11_unresolved_decisions": readiness.open_critical
        + readiness.open_pending
        + readiness.outstanding_required,
        "12_identified_risks": {
            "operator_stated": _category_answer("quality.risks"),
            "content_risk_findings": (
                reconstruction_dict.get("risk_findings", []) if reconstruction_dict else []
            ),
        },
        "13_existing_implementation_status": implementation_status,
        "14_pending_task_inventory": pending_tasks,
        "15_documentation_inconsistencies": (
            reconstruction_dict.get("claim_conflicts", []) if reconstruction_dict else []
        ),
        "16_tool_discovery_results": tools_report.to_dict() if tools_report else None,
        "17_recommended_mcps_skills": list(recommendations or []),
        "18_verified_tool_readiness": {
            "verified_by_probe": verified_tools,
            "note": "installed/configured tools are NOT listed as verified",
        },
        "19_onboarding_completion_status": readiness.to_dict(),
        "20_suggested_next_actions": next_actions,
    }


def _suggest_next_actions(readiness: ReadinessResult, session: dict) -> list[str]:
    """Deterministic, honest suggestions derived from the gate result."""
    actions: list[str] = []
    stage = session.get("stage")
    if readiness.outcome == OUTCOME_BLOCKED:
        actions.append(
            "resolve the open critical blockers above before proceeding to any "
            "downstream phase"
        )
    if readiness.outcome == OUTCOME_IN_PROGRESS:
        if readiness.outstanding_required:
            actions.append(
                "answer the outstanding required questions (aperio onboarding-answer)"
            )
        actions.append("continue onboarding from the current stage")
    if readiness.outcome in (OUTCOME_READY, OUTCOME_READY_WITH_PENDING):
        actions.append(
            "review the readiness report; READY means onboarding is complete for "
            "the next authorized phase, not that development may start automatically"
        )
        actions.append(
            "generate a goal contract from confirmed decisions when starting "
            "planning (aperio goal-check / preflight)"
        )
    if readiness.outcome == OUTCOME_READY_WITH_PENDING:
        actions.append(
            "track the pending decisions above; each gates a specific future action, "
            "not onboarding itself"
        )
    if stage != STAGE_COMPLETE:
        actions.append("re-run 'aperio onboarding-readiness' after changes to re-evaluate")
    return actions


def render_report_markdown(report: dict) -> str:
    """Readable Markdown rendering of the structured report."""
    lines = [
        "# Project readiness report",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Session: `{report['1_project_overview'].get('session_id')}`",
        f"- Workspace: `{report['1_project_overview'].get('workspace')}`",
        f"- Classification: {report['1_project_overview'].get('classification')}",
        f"- Category: {report['1_project_overview'].get('project_category') or '(not set)'}",
        "",
        "## Objective and intended outcome",
        "",
        f"- Intent: {report['2_objective_and_outcome'].get('intent') or '(not recorded)'}",
        f"- Goal: {report['2_objective_and_outcome'].get('goal') or '(not answered)'}",
        f"- Success criteria: {report['2_objective_and_outcome'].get('success_criteria') or '(not answered)'}",
        "",
        "## Product requirements",
        "",
    ]
    for item in report["3_product_requirements"]:
        marker = "" if item["kind"] == "confirmed" else f" [{item['kind']}]"
        lines.append(f"- {item['question']}: {item['answer']}{marker}")
    if not report["3_product_requirements"]:
        lines.append("- (none recorded)")

    lines += ["", "## Constraints", ""]
    for item in report["4_constraints"]:
        lines.append(f"- {item['question']}: {item['answer']}")
    if not report["4_constraints"]:
        lines.append("- (none recorded)")

    for title, key in (
        ("Platform and technical requirements", "5_platform_and_technical"),
        ("UI/UX requirements", "6_ui_ux_requirements"),
        ("Quality criteria", "8_quality_criteria"),
    ):
        lines += ["", f"## {title}", ""]
        for item in report[key]:
            lines.append(f"- {item['question']}: {item['answer']}")
        if not report[key]:
            lines.append("- (none recorded)")

    lines += [
        "",
        "## Architecture decisions",
        "",
    ]
    for d in report["7_architecture_decisions"]:
        lines.append(f"- {d['id']}: {d['title']} — {d['text']} (source: {d['source']})")
    if not report["7_architecture_decisions"]:
        lines.append("- (none recorded)")

    lines += ["", "## Known assumptions", ""]
    for item in report["10_known_assumptions"]:
        lines.append(f"- {item['question']}: {item['answer']} (not confirmed)")
    if not report["10_known_assumptions"]:
        lines.append("- (none)")

    lines += ["", "## Unresolved decisions", ""]
    for topic in report["11_unresolved_decisions"]:
        lines.append(f"- {topic}")
    if not report["11_unresolved_decisions"]:
        lines.append("- (none)")

    lines += ["", "## Identified risks", ""]
    stated = report["12_identified_risks"].get("operator_stated")
    lines.append(f"- Operator-stated: {stated or '(none recorded)'}")
    for finding in report["12_identified_risks"].get("content_risk_findings", []):
        lines.append(
            f"- Content risk ({finding['kind']}) at {finding['source']}:{finding['line_no']}"
        )

    lines += [
        "",
        "## Existing implementation status",
        "",
        f"- {report['13_existing_implementation_status'].get('note', '')}",
    ]
    for task in report["13_existing_implementation_status"].get("aperio_plan_tasks", []):
        lines.append(f"- Aperio plan task {task.get('id')}: {task.get('title')} [{task.get('status')}]")
    for claim in report["13_existing_implementation_status"].get("documented_complete_claims", []):
        lines.append(f"- Documented claim (unverified): {claim['text']} ({claim['source']})")

    lines += ["", "## Pending task inventory (documented, not approved)", ""]
    for task in report["14_pending_task_inventory"]:
        lines.append(f"- {task['text']} ({task['source']}:{task['line_no']})")
    if not report["14_pending_task_inventory"]:
        lines.append("- (none documented)")

    lines += ["", "## Documentation inconsistencies", ""]
    for conflict in report["15_documentation_inconsistencies"]:
        values = ", ".join(f"{c['value']} ({c['source']})" for c in conflict["claims"])
        lines.append(f"- {conflict['topic']}: {values}")
    if not report["15_documentation_inconsistencies"]:
        lines.append("- (none detected)")

    lines += [
        "",
        "## Tool discovery and recommendations",
        "",
    ]
    tools = report["16_tool_discovery_results"]
    if tools:
        lines.append("- Discovery ran; see the JSON report for the full inventory.")
    else:
        lines.append("- (discovery not run in this session)")
    for rec in report["17_recommended_mcps_skills"]:
        lines.append(
            f"- {rec['name']} ({rec['tool_id']}): {rec['compatibility']} — {rec['purpose']}"
        )
    if not report["17_recommended_mcps_skills"]:
        lines.append("- (no recommendations)")

    lines += ["", "## Verified tool readiness", ""]
    verified = report["18_verified_tool_readiness"].get("verified_by_probe", [])
    for tool in verified:
        lines.append(f"- {tool['tool_id']}: verified by probe ({tool.get('version') or 'no version output'})")
    if not verified:
        lines.append("- (nothing verified by probe; installed/configured tools are not verified)")

    lines += [
        "",
        "## Onboarding completion status",
        "",
        f"- **{report['19_onboarding_completion_status']['outcome']}**",
    ]
    for reason in report["19_onboarding_completion_status"]["reasons"]:
        lines.append(f"- {reason}")

    lines += ["", "## Suggested next actions", ""]
    for action in report["20_suggested_next_actions"]:
        lines.append(f"- {action}")

    lines += [
        "",
        "---",
        "",
        "READY means onboarding is sufficiently complete for the next authorized "
        "phase. It is not permission to start executing development tasks.",
        "",
    ]
    return "\n".join(lines)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
