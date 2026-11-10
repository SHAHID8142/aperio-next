"""Onboarding orchestration facade (onboarding-protocol v1).

The CLI is the only printing layer; the host integration contract is
this module's *protocol document*: every entry point returns one JSON-safe
dict with exactly one next action required from the operator. Hosts that
cannot embed Aperio natively drive the same protocol through the CLI's
``--json`` output — the machine-readable question/answer protocol the
Step 01 directive requires.

Implemented journeys:

- **Empty workspace** (Rule 5): discovery → ask "Would you like to
  initialize a new project here?" → NO: exit, nothing modified → YES:
  init via the existing safe mechanism → session → first interview
  question ("What would you like to build or accomplish?" arrives as the
  adaptive engine's goal question).
- **Existing workspace** (Rule 8): discovery → ask "What would you like
  to do with this project?" (seven options + custom) → reconstruction
  (read-only) → gap interview → optional standardization → readiness.
- **Resume** (Rule 15): an existing session resumes exactly where it
  stopped; accepted answers are never asked again; completed side effects
  are never repeated.

No mutations happen in :func:`start` — ever. The first mutation of any
journey is operator-approved (``confirm_initialization`` / recorded
approvals downstream).
"""

from __future__ import annotations

from pathlib import Path

from aperio.errors import NotInitializedError, StateError, ValidationError
from aperio.onboarding_discovery import (
    CLASS_APERIO_PROJECT,
    CLASS_EMPTY_NEW,
    DiscoveryResult,
    discover_workspace,
)
from aperio.onboarding_interview import (
    DEFAULT_PROFILE,
    apply_answer,
    get_profile,
    interview_progress,
    next_question,
    profile_for_category,
)
from aperio.onboarding_reconstruct import ReconstructionResult, missing_information_questions
from aperio.onboarding_session import (
    STAGE_INTERVIEW,
    STAGE_RECONSTRUCTION,
    STAGE_STANDARDIZATION,
    create_session,
    read_session,
    record_approval,
    set_intent,
    set_interview_profile,
    set_project_category,
    set_reconstruction_summary,
    set_stage,
    session_exists,
    update_session,
)
from aperio.project import APERIO_DIR_NAME, aperio_dir_for, init_project
from aperio.schema import utc_now_iso

PROTOCOL_FORMAT = "aperio-onboarding-protocol"
PROTOCOL_VERSION = 1

INIT_ACTION = "onboarding.initialization"

# Rule 8 suggested responses (ids stable; text shown to the operator).
INTENT_CHOICES: tuple[dict, ...] = (
    {"id": "continue", "label": "Continue existing work"},
    {"id": "new_task", "label": "Start a new feature or task"},
    {"id": "review_health", "label": "Review project health"},
    {"id": "organize", "label": "Organize the project using Aperio"},
    {"id": "recover", "label": "Recover incomplete work"},
    {"id": "review_docs", "label": "Review documentation or architecture"},
    {"id": "other", "label": "Something else (describe it)"},
)


def _protocol(
    workspace: str,
    discovery: DiscoveryResult | None,
    action_required: str,
    prompt: str,
    *,
    question: dict | None = None,
    session_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    doc = {
        "format": PROTOCOL_FORMAT,
        "version": PROTOCOL_VERSION,
        "workspace": workspace,
        "classification": discovery.classification if discovery else None,
        "classification_reasons": list(discovery.classification_reasons) if discovery else [],
        "action_required": action_required,
        "prompt": prompt,
        "question": question,
        "session_id": session_id,
        "choices": None,
        "progress": None,
    }
    if extra:
        doc.update(extra)
    return doc


# ---------------------------------------------------------------------------
# Entry points (start is always read-only).
# ---------------------------------------------------------------------------

def start(raw_path: str) -> dict:
    """Universal entry point: classify the workspace and ask ONE thing."""
    discovery = discover_workspace(raw_path)
    workspace_str = str(discovery.workspace)

    if discovery.classification == CLASS_APERIO_PROJECT and session_exists(
        discovery.workspace / APERIO_DIR_NAME
    ):
        return resume(str(discovery.workspace), discovery)

    if discovery.classification == CLASS_EMPTY_NEW:
        return _protocol(
            workspace_str,
            discovery,
            "confirm_initialization",
            "Would you like to initialize a new project here? "
            "(Approving creates only Aperio's .aperio context scaffolding; "
            "declining changes nothing.)",
            extra={
                "choices": [
                    {"id": "yes", "label": "Yes, initialize"},
                    {"id": "no", "label": "No, exit"},
                ]
            },
        )

    if discovery.classification == "ambiguous":
        return _protocol(
            workspace_str,
            discovery,
            "resolve_state",
            "The workspace state is ambiguous (corrupt, unsupported, symlinked, "
            "or unreadable project state). Nothing will be modified; resolve the "
            "state manually first. See classification_reasons.",
        )

    # Existing project (with or without previous orchestration evidence).
    return _protocol(
        workspace_str,
        discovery,
        "choose_intent",
        "What would you like to do with this project?",
        extra={"choices": list(INTENT_CHOICES)},
    )


def resume(raw_path: str, discovery: DiscoveryResult | None = None) -> dict:
    """Resume an existing onboarding session (never repeats side effects)."""
    if discovery is None:
        discovery = discover_workspace(raw_path)
    apdir = aperio_dir_for(discovery.workspace)
    try:
        session = read_session(apdir)
    except NotInitializedError:
        return start(raw_path)
    if session.get("cancelled"):
        return _protocol(
            str(discovery.workspace), discovery, "none",
            "The previous onboarding session was cancelled; start a new "
            "onboarding run if you want to begin again.",
            session_id=session.get("session_id"),
        )
    return _next_step(discovery, session)


def confirm_initialization(raw_path: str, intent: str | None = None) -> dict:
    """Operator-approved initialization (the only sanctioned first mutation).

    For an empty workspace this runs the existing safe init mechanism and
    opens the interview. For an existing project it creates Aperio's
    non-destructive scaffolding (create-missing-only) so onboarding state
    can persist, records the stated intent, and proceeds to reconstruction.
    A repeated call never destroys data: init is idempotent and session
    creation refuses duplicates.
    """
    discovery = discover_workspace(raw_path)
    workspace = discovery.workspace
    apdir = aperio_dir_for(workspace)

    if discovery.classification == CLASS_EMPTY_NEW:
        init_project(str(workspace))
        session = create_session(
            apdir,
            workspace=str(workspace),
            classification=discovery.classification,
            classification_reasons=list(discovery.classification_reasons),
            discovery_summary=discovery.to_dict(),
        )
        # Approval is recorded after the session exists (approvals live in
        # the session); initialization itself was the explicitly approved
        # action via the CLI flag, not via repository content.
        record_approval(apdir, action=INIT_ACTION, scope="empty-workspace")
        set_stage(apdir, STAGE_INTERVIEW, detail="new project initialized")
        session = read_session(apdir)
        return _next_step(discovery, session)

    if discovery.classification == "ambiguous":
        raise StateError(
            "workspace state is ambiguous; resolve it manually before "
            "initializing (nothing was modified)"
        )

    # Existing project: init is non-destructive; only missing scaffolding
    # is created. An existing valid state is never rewritten.
    init_result = init_project(str(workspace))
    if not session_exists(apdir):
        create_session(
            apdir,
            workspace=str(workspace),
            classification=discovery.classification,
            classification_reasons=list(discovery.classification_reasons),
            discovery_summary=discovery.to_dict(),
        )
        record_approval(apdir, action=INIT_ACTION, scope="existing-project")
    if intent:
        set_intent(apdir, intent)
    set_stage(apdir, STAGE_RECONSTRUCTION, detail="existing project onboarding")
    session = read_session(apdir)
    return _protocol(
        str(workspace),
        discovery,
        "run_reconstruction",
        "Project context scaffolded (nothing existing was overwritten). Next: "
        "read-only reconstruction — run 'aperio onboarding-reconstruct'.",
        session_id=session.get("session_id"),
        extra={"init_created": list(init_result.created or [])},
    )


def decline_initialization(raw_path: str) -> dict:
    """Rule 5 NO branch: exit gracefully; nothing is installed or modified."""
    discovery = discover_workspace(raw_path)
    return _protocol(
        str(discovery.workspace),
        discovery,
        "none",
        "Onboarding declined. Nothing was initialized, installed, or modified.",
    )


# ---------------------------------------------------------------------------
# Intent + reconstruction + answers.
# ---------------------------------------------------------------------------

def record_intent(apdir: Path, choice_id: str, custom_text: str | None = None) -> dict:
    """Record the Rule 8 intent choice and route to the next stage."""
    known = {c["id"] for c in INTENT_CHOICES}
    if choice_id not in known:
        raise ValidationError(
            f"unknown intent choice {choice_id!r}; expected one of: " + ", ".join(sorted(known))
        )
    label = next(c["label"] for c in INTENT_CHOICES if c["id"] == choice_id)
    intent_text = custom_text.strip() if custom_text else label
    if not intent_text:
        intent_text = label
    set_intent(apdir, intent_text)
    stage = STAGE_STANDARDIZATION if choice_id == "organize" else STAGE_RECONSTRUCTION
    set_stage(apdir, stage, detail=f"intent: {choice_id}")
    session = read_session(apdir)
    workspace = session["workspace"]
    discovery = discover_workspace(workspace)
    return _protocol(
        workspace,
        discovery,
        "run_reconstruction",
        "Intent recorded. Next: read-only reconstruction — "
        "'aperio onboarding-reconstruct'.",
        session_id=session.get("session_id"),
    )


def store_reconstruction(apdir: Path, result: ReconstructionResult) -> dict:
    """Persist a bounded reconstruction summary + stage transition."""
    summary = {
        "documents": len(result.documents),
        "pending_tasks": len(result.pending_tasks),
        "completed_claims": len(result.completed_claims),
        "claim_conflicts": [c.topic for c in result.claim_conflicts],
        "risk_findings": len(result.risk_findings),
        "missing_topics": list(result.missing_topics),
        "aperio_decisions_count": result.aperio_decisions_count,
    }
    set_reconstruction_summary(apdir, summary)
    set_stage(apdir, STAGE_INTERVIEW, detail="gap interview from reconstruction")
    return summary


def _gap_questions(session: dict):
    """Extra interview questions derived from stored reconstruction gaps."""
    summary = session.get("reconstruction_summary") or {}
    topics = summary.get("missing_topics") or []

    class _Stub:
        __slots__ = ("missing_topics",)

    stub = _Stub()
    stub.missing_topics = topics
    from aperio.onboarding_reconstruct import missing_information_questions as _miq

    return _miq(stub)  # type: ignore[arg-type]


def _next_step(discovery: DiscoveryResult, session: dict) -> dict:
    """One next action from a session: question, stage action, or readiness."""
    apdir = aperio_dir_for(discovery.workspace)
    profile_id = session.get("interview_profile") or DEFAULT_PROFILE
    progress = interview_progress(session, profile_id, _gap_questions(session))
    question = next_question(session, profile_id, _gap_questions(session))
    if question is not None:
        return _protocol(
            str(discovery.workspace),
            discovery,
            "answer_question",
            f"[{question['category']}] {question['prompt']}",
            question=question,
            session_id=session.get("session_id"),
            extra={"progress": progress},
        )
    # Coverage met: what the operator does next depends on the stage.
    stage = session.get("stage")
    if stage == STAGE_STANDARDIZATION:
        action, prompt = "propose_standardization", (
            "Interview coverage met. Next for this intent: build the "
            "standardization proposal — 'aperio onboarding-propose-standardization'."
        )
    else:
        action, prompt = "evaluate_readiness", (
            "Interview coverage met. Next: evaluate onboarding readiness — "
            "'aperio onboarding-readiness'."
        )
    return _protocol(
        str(discovery.workspace),
        discovery,
        action,
        prompt,
        session_id=session.get("session_id"),
        extra={"progress": progress},
    )


def answer(apdir: Path, question_id: str, raw_answer: str) -> dict:
    """Record one operator answer; returns the next protocol step."""
    session_before = read_session(apdir)
    gap_questions = _gap_questions(session_before)
    result = apply_answer(apdir, question_id, raw_answer, extra_questions=gap_questions)
    session = result
    # Category answers retarget the interview profile immediately.
    if question_id == "project.category":
        profile = profile_for_category(raw_answer)
        set_interview_profile(apdir, profile.profile_id)
        set_project_category(apdir, profile.profile_id)
        session = read_session(apdir)
    discovery = discover_workspace(session["workspace"])
    return _next_step(discovery, session)


def revise(apdir: Path, question_id: str, raw_answer: str) -> dict:
    """Revise a previous answer; same contract as answering."""
    return answer(apdir, question_id, raw_answer)


def pause(apdir: Path) -> dict:
    """Pause: state is already durable; returns resume instructions."""
    session = read_session(apdir)
    return {
        "format": PROTOCOL_FORMAT,
        "version": PROTOCOL_VERSION,
        "session_id": session.get("session_id"),
        "stage": session.get("stage"),
        "action_required": "none",
        "prompt": (
            "Onboarding paused. State is persisted at "
            ".aperio/onboarding/session.json — resume any time with "
            "'aperio onboarding-start'; answered questions will not be repeated."
        ),
        "answers_recorded": len(session.get("answers", {})),
    }


def cancel(apdir: Path) -> dict:
    """Explicit operator cancellation (state preserved, marked cancelled)."""
    from aperio.onboarding_session import cancel_session

    session = cancel_session(apdir)
    return {
        "format": PROTOCOL_FORMAT,
        "version": PROTOCOL_VERSION,
        "session_id": session.get("session_id"),
        "action_required": "none",
        "prompt": "Onboarding cancelled. Session state is preserved and marked cancelled.",
    }
