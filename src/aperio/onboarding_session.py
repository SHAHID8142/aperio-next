"""Durable onboarding sessions (onboarding-session v1).

Step 01 onboarding must survive interruption: an operator who answered
thirty questions must not have to answer them again because a chat ended.
This module owns the onboarding session record — one validated JSON
document at ``.aperio/onboarding/session.json`` — and every rule about
how it is created, read, updated, resumed, and cancelled.

Guarantees (implemented, matching the repository's documented patterns):

- **Atomic creation**: the session file is published through an exclusive
  temp file and an ``os.link`` create-if-not-exists, so a concurrent
  starter can never clobber an existing session.
- **Lost-update protection**: every mutation runs under the advisory
  ``session.lock`` and republishes with a byte compare-and-swap against
  the snapshot that was read. A concurrent writer's changes are detected
  and refused — nothing is modified.
- **Never auto-repairs**: a corrupt, non-UTF-8, or too-new session is
  reported as an error naming the file; it is never rewritten or
  "fixed" behind the operator's back.
- **Schema versioning**: ``schema_version`` 1 today; a higher version is
  refused with an upgrade hint. Unknown extra keys are tolerated and
  preserved verbatim across updates.
- **Provenance**: answers record *who said what* — operator-confirmed,
  operator-unsure, or operator-deferred. Nothing in this module ever
  promotes an AI-generated suggestion to an operator decision; that
  promotion belongs to the interview layer and requires the operator to
  confirm the answer.

Bounds: free-text answers and decision text are size-limited (refuse,
never truncate); the event history is trimmed to the most recent
``MAX_HISTORY_EVENTS`` entries so a long onboarding cannot grow the file
without bound.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aperio.artifacts import check_artifact_dir
from aperio.errors import NotInitializedError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock
from aperio.project import require_supported_state
from aperio.onboarding_discovery import (
    CLASSIFICATIONS,
    ONBOARDING_SESSION_REL,
)
from aperio.schema import utc_now_iso

SESSION_FORMAT = "aperio-onboarding-session"
SESSION_SCHEMA_VERSION = 1
SESSION_FILE_NAME = "session.json"

ONBOARDING_DIR_NAME = "onboarding"

# Stages (stable, machine-readable; the interview layer may refine within
# a stage but the stage enum is owned here).
STAGE_DISCOVERY = "discovery"
STAGE_INTENT = "intent"
STAGE_INTERVIEW = "interview"
STAGE_RECONSTRUCTION = "reconstruction"
STAGE_GAPS = "gaps"
STAGE_STANDARDIZATION = "standardization"
STAGE_TOOLS = "tools"
STAGE_COMPLETION = "completion"
STAGE_COMPLETE = "complete"
STAGE_CANCELLED = "cancelled"

STAGES = (
    STAGE_DISCOVERY,
    STAGE_INTENT,
    STAGE_INTERVIEW,
    STAGE_RECONSTRUCTION,
    STAGE_GAPS,
    STAGE_STANDARDIZATION,
    STAGE_TOOLS,
    STAGE_COMPLETION,
    STAGE_COMPLETE,
    STAGE_CANCELLED,
)

# Answer provenance kinds.
KIND_CONFIRMED = "confirmed"  # operator answered directly
KIND_UNSURE = "unsure"  # operator said "not sure yet"
KIND_DEFERRED = "deferred"  # operator said "decide later"

ANSWER_KINDS = (KIND_CONFIRMED, KIND_UNSURE, KIND_DEFERRED)

# Severity classification for unanswered/unknown items.
SEVERITY_CRITICAL = "critical_blocker"
SEVERITY_REQUIRED_BEFORE_ACTION = "required_before_action"
SEVERITY_PENDING_DECISION = "pending_decision"
SEVERITY_OPTIONAL = "optional"

SEVERITIES = (
    SEVERITY_CRITICAL,
    SEVERITY_REQUIRED_BEFORE_ACTION,
    SEVERITY_PENDING_DECISION,
    SEVERITY_OPTIONAL,
)

# Refuse-oversize bounds (never truncate).
MAX_ANSWER_BYTES = 8 * 1024
MAX_DECISION_TEXT_BYTES = 8 * 1024
MAX_DECISION_TITLE_CHARS = 200
MAX_HISTORY_EVENTS = 500

_REQUIRED_STR_FIELDS = ("session_id", "workspace", "created_at", "updated_at", "stage")


def onboarding_dir(aperio_dir: Path) -> Path:
    """Path of the ``.aperio/onboarding/`` directory."""
    return aperio_dir / ONBOARDING_DIR_NAME


def session_path(aperio_dir: Path) -> Path:
    """Path of ``.aperio/onboarding/session.json``."""
    return onboarding_dir(aperio_dir) / SESSION_FILE_NAME


def session_tmp_path(aperio_dir: Path) -> Path:
    """Path of the reserved atomic-write temporary file."""
    return onboarding_dir(aperio_dir) / (SESSION_FILE_NAME + ".tmp")


def session_lock_path(aperio_dir: Path) -> Path:
    """Path of the advisory lock guarding session mutations."""
    return onboarding_dir(aperio_dir) / "session.lock"


def session_exists(aperio_dir: Path) -> bool:
    """Whether a session file exists (a symlink is not a usable session)."""
    path = session_path(aperio_dir)
    return path.is_file() and not os.path.islink(path)


def new_session_id() -> str:
    """Fresh opaque session identifier (``ob-`` + random hex)."""
    return f"ob-{uuid.uuid4().hex}"


def create_session(
    aperio_dir: Path,
    *,
    workspace: str,
    classification: str,
    classification_reasons: list[str] | tuple[str, ...] = (),
    discovery_summary: dict | None = None,
) -> dict:
    """Create the session record atomically. Refuses to overwrite.

    ``workspace`` is the resolved absolute workspace path (identity for
    resume). ``classification`` must be a discovery classification value.

    :raises StateError: when a session already exists or the write fails.
    :raises ValidationError: when the classification is unknown.
    """
    if classification not in CLASSIFICATIONS:
        raise ValidationError(
            f"unknown project classification {classification!r}; expected one of: "
            + ", ".join(CLASSIFICATIONS)
        )
    if session_exists(aperio_dir):
        raise StateError(
            f"an onboarding session already exists at {session_path(aperio_dir)}; "
            "resume it with 'aperio onboarding-status' instead of creating a new one"
        )
    # Onboarding sessions live only inside valid Aperio projects: creating
    # one must never be the mutation that initializes a project.
    require_supported_state(aperio_dir)

    now = utc_now_iso()
    data = {
        "format": SESSION_FORMAT,
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": new_session_id(),
        "workspace": workspace,
        "created_at": now,
        "updated_at": now,
        "stage": STAGE_DISCOVERY,
        "classification": classification,
        "classification_reasons": list(classification_reasons),
        "project_category": None,
        "interview_profile": None,
        "intent": None,
        "questions": [],
        "answers": {},
        "answer_history": {},
        "unresolved": [],
        "decisions": [],
        "discovery_summary": discovery_summary or {},
        "reconstruction_summary": None,
        "approvals": [],
        "tool_records": [],
        "blockers": [],
        "history": [
            {"at": now, "event": "session.created", "detail": classification}
        ],
        "cancelled": False,
        "cancelled_at": None,
        "aperio_version": _aperio_version(),
    }
    _validate_session(data, session_path(aperio_dir))

    check_artifact_dir(aperio_dir, ONBOARDING_DIR_NAME, create=True)
    _publish_new(aperio_dir, data)
    return data


def read_session(aperio_dir: Path) -> dict:
    """Read and validate the full session object (all keys preserved).

    :raises NotInitializedError: when no session exists.
    :raises StateError: when unreadable, symlinked, corrupt, or too new.
    """
    path = session_path(aperio_dir)
    _reject_symlink(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise NotInitializedError(
            "no onboarding session in this project; "
            "start onboarding with 'aperio onboarding-start'"
        ) from None
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc.strerror or exc}") from None
    data = parse_session_bytes(raw, path)
    _require_same_workspace(data, path)
    return data


def parse_session_bytes(raw: bytes, path: Path) -> dict:
    """Validate raw session bytes and return the full parsed dict."""
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
            "aperio will not modify it. Inspect or restore the file manually."
        ) from None
    _validate_session(data, path)
    return data


def update_session(aperio_dir: Path, mutate) -> dict:
    """Apply ``mutate(data) -> data`` to the session under lock + CAS.

    The mutator receives the validated full dict and returns the updated
    dict (it may mutate in place and return it). On any validation error
    raised by the mutator nothing is written.

    :raises NotInitializedError: when no session exists.
    :raises StateError: on concurrent modification or publish failure.
    """
    # Fast-fail on a missing session BEFORE touching the lock: without this
    # a session-less project fails inside acquire_lock with a confusing
    # "cannot create lock file" PathError instead of the actionable
    # NotInitializedError (the lock directory only exists once a session
    # has been created).
    try:
        session_path(aperio_dir).stat()
    except FileNotFoundError:
        raise NotInitializedError(
            "no onboarding session in this project; "
            "start onboarding with 'aperio onboarding-start'"
        ) from None
    lock = acquire_lock(session_lock_path(aperio_dir), artifact="onboarding session")
    try:
        path = session_path(aperio_dir)
        _reject_symlink(path)
        raw = path.read_bytes()
        data = parse_session_bytes(raw, path)
        _require_same_workspace(data, path)
        updated = mutate(data)
        _validate_session(updated, path)
        updated["updated_at"] = utc_now_iso()
        _publish_cas(aperio_dir, updated, expected_raw=raw)
        return updated
    finally:
        release_lock(session_lock_path(aperio_dir), lock)


# ---------------------------------------------------------------------------
# Structured mutations (used by the onboarding facade and CLI).
# ---------------------------------------------------------------------------


def set_stage(aperio_dir: Path, stage: str, detail: str | None = None) -> dict:
    """Transition the session stage (validated enum; event recorded)."""
    if stage not in STAGES:
        raise ValidationError(
            f"unknown onboarding stage {stage!r}; expected one of: " + ", ".join(STAGES)
        )

    def mutate(data: dict) -> dict:
        if data["stage"] == STAGE_CANCELLED:
            raise StateError(
                "onboarding session was cancelled; start a new session instead"
            )
        if data["stage"] == STAGE_COMPLETE and stage != STAGE_COMPLETE:
            raise StateError(
                "onboarding session already completed; use a fresh onboarding "
                "run for a new pass"
            )
        data["stage"] = stage
        _append_history(data, "stage.changed", detail or stage)
        return data

    return update_session(aperio_dir, mutate)


def set_intent(aperio_dir: Path, intent: str) -> dict:
    """Record the operator's stated purpose (free text, bounded)."""
    _require_text(intent, "intent", MAX_ANSWER_BYTES)
    return update_session(aperio_dir, lambda data: _set_field(data, "intent", intent))


def set_project_category(aperio_dir: Path, category: str | None) -> dict:
    """Record the interview category / profile selection."""
    return update_session(
        aperio_dir,
        lambda data: _set_field(data, "project_category", category),
    )


def set_interview_profile(aperio_dir: Path, profile: str | None) -> dict:
    return update_session(
        aperio_dir,
        lambda data: _set_field(data, "interview_profile", profile),
    )


def set_discovery_summary(aperio_dir: Path, summary: dict) -> dict:
    return update_session(
        aperio_dir,
        lambda data: _set_field(data, "discovery_summary", summary),
    )


def set_reconstruction_summary(aperio_dir: Path, summary: dict | None) -> dict:
    return update_session(
        aperio_dir,
        lambda data: _set_field(data, "reconstruction_summary", summary),
    )


def record_answer(
    aperio_dir: Path,
    question_id: str,
    *,
    answer: str,
    kind: str,
) -> dict:
    """Record (or revise) an answer with provenance and history.

    Revision keeps the previous answer in ``answer_history`` so nothing is
    silently lost; the audit trail records both.
    """
    if not isinstance(question_id, str) or not question_id.strip():
        raise ValidationError("question id must be a non-empty string")
    if len(question_id) > 128:
        raise ValidationError("question id exceeds 128 characters")
    _require_text(answer, "answer", MAX_ANSWER_BYTES)
    if kind not in ANSWER_KINDS:
        raise ValidationError(
            f"unknown answer kind {kind!r}; expected one of: " + ", ".join(ANSWER_KINDS)
        )

    def mutate(data: dict) -> dict:
        answers = data.setdefault("answers", {})
        history = data.setdefault("answer_history", {})
        previous = answers.get(question_id)
        if previous is not None:
            bucket = history.setdefault(question_id, [])
            bucket.append(previous)
        entry = {
            "answer": answer,
            "kind": kind,
            "recorded_at": utc_now_iso(),
            "revision": (previous.get("revision", 1) + 1) if previous else 1,
        }
        answers[question_id] = entry
        event = "answer.revised" if previous else "answer.recorded"
        _append_history(data, event, question_id)
        return data

    return update_session(aperio_dir, mutate)


def withdraw_answer(aperio_dir: Path, question_id: str) -> dict:
    """Remove a recorded answer so the interview asks it again.

    This is the ``/back`` primitive: going back must not overwrite a genuine
    answer with a placeholder. The withdrawn answer is preserved in
    ``answer_history`` and in the session audit trail, so nothing is lost and
    the operator's earlier response remains reviewable.
    """
    if not isinstance(question_id, str) or not question_id.strip():
        raise ValidationError("question id must be a non-empty string")
    if len(question_id) > 128:
        raise ValidationError("question id exceeds 128 characters")

    def mutate(data: dict) -> dict:
        answers = data.setdefault("answers", {})
        previous = answers.pop(question_id, None)
        if previous is None:
            return data
        history = data.setdefault("answer_history", {})
        history.setdefault(question_id, []).append(previous)
        _append_history(data, "answer.withdrawn", question_id)
        return data

    return update_session(aperio_dir, mutate)


def record_decision(
    aperio_dir: Path,
    *,
    title: str,
    text: str,
    source: str = "operator",
) -> dict:
    """Append a confirmed operator decision to the session.

    ``source`` records where the decision came from (e.g. ``operator``,
    ``operator:interview``, ``document:<path>``); it is provenance, not
    authority — only operator-confirmed decisions belong here.
    """
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("decision title must be a non-empty string")
    if len(title) > MAX_DECISION_TITLE_CHARS:
        raise ValidationError(
            f"decision title exceeds {MAX_DECISION_TITLE_CHARS} characters"
        )
    if "\n" in title or "\r" in title:
        raise ValidationError("decision title must be a single line")
    if title.lstrip().startswith("#"):
        raise ValidationError(
            "decision title must not start with '#': titles are rendered as "
            "quoted content, not headings"
        )
    _require_text(text, "decision text", MAX_DECISION_TEXT_BYTES)
    if not isinstance(source, str) or not source.strip():
        raise ValidationError("decision source must be a non-empty string")

    def mutate(data: dict) -> dict:
        decisions = data.setdefault("decisions", [])
        number = len(decisions) + 1
        entry = {
            "id": f"DEC-{number:04d}",
            "title": title.strip(),
            "text": text,
            "source": source.strip(),
            "recorded_at": utc_now_iso(),
        }
        decisions.append(entry)
        _append_history(data, "decision.recorded", entry["id"])
        return data

    return update_session(aperio_dir, mutate)


def record_approval(
    aperio_dir: Path,
    *,
    action: str,
    scope: str,
    method: str = "cli",
    fingerprint: str | None = None,
) -> dict:
    """Append an explicit operator approval record.

    An approval is scoped: ``scope`` names exactly what was approved
    (e.g. ``standardization.apply:README.md``). Approval for one action is
    never read as approval for another.

    ``fingerprint`` optionally binds the approval to the exact artifact that
    was reviewed (for example an install plan's command/source/permission
    hash). Callers that bind a fingerprint refuse to act on anything whose
    fingerprint differs, so an approved plan cannot be swapped for a
    different executable.
    """
    for name, value in (("action", action), ("scope", scope)):
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"approval {name} must be a non-empty string")
        if len(value) > 300:
            raise ValidationError(f"approval {name} exceeds 300 characters")
    if "\n" in action or "\r" in action or "\n" in scope or "\r" in scope:
        raise ValidationError("approval action and scope must be single-line strings")
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValidationError("approval fingerprint must be a non-empty string")
        if len(fingerprint) > 128:
            raise ValidationError("approval fingerprint exceeds 128 characters")

    def mutate(data: dict) -> dict:
        approvals = data.setdefault("approvals", [])
        entry = {
            "id": f"APR-{len(approvals) + 1:04d}",
            "action": action.strip(),
            "scope": scope.strip(),
            "granted_at": utc_now_iso(),
            "method": method,
        }
        if fingerprint is not None:
            entry["fingerprint"] = fingerprint.strip()
        approvals.append(entry)
        _append_history(data, "approval.recorded", f"{action}:{scope}")
        return data

    return update_session(aperio_dir, mutate)


def has_approval(aperio_dir: Path, action: str, scope: str) -> bool:
    try:
        data = read_session(aperio_dir)
    except NotInitializedError:
        return False
    return any(
        a.get("action") == action and a.get("scope") == scope
        for a in data.get("approvals", [])
        if isinstance(a, dict)
    )


def approved_plan_fingerprint(
    aperio_dir: Path, action: str, scope: str
) -> str | None:
    """Fingerprint bound to the most recent approval for (action, scope).

    Returns ``None`` when the approval predates fingerprint binding, in which
    case callers fall back to the historical (action, scope) check only.
    """
    try:
        data = read_session(aperio_dir)
    except NotInitializedError:
        return None
    bound = None
    for entry in data.get("approvals", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("action") == action and entry.get("scope") == scope:
            value = entry.get("fingerprint")
            bound = value if isinstance(value, str) else None
    return bound


def add_unresolved(
    aperio_dir: Path,
    *,
    topic: str,
    severity: str,
    reason: str = "",
    question_id: str | None = None,
) -> dict:
    """Record an unresolved item with its severity classification."""
    if not isinstance(topic, str) or not topic.strip():
        raise ValidationError("unresolved topic must be a non-empty string")
    if severity not in SEVERITIES:
        raise ValidationError(
            f"unknown severity {severity!r}; expected one of: " + ", ".join(SEVERITIES)
        )

    def mutate(data: dict) -> dict:
        unresolved = data.setdefault("unresolved", [])
        existing = next(
            (u for u in unresolved if u.get("topic") == topic and u.get("resolved_at") is None),
            None,
        )
        if existing is not None:
            existing["severity"] = severity
            existing["reason"] = reason
            if question_id:
                existing["question_id"] = question_id
            existing["updated_at"] = utc_now_iso()
            _append_history(data, "unresolved.updated", topic)
            return data
        entry = {
            "id": f"UNS-{len(unresolved) + 1:04d}",
            "topic": topic.strip(),
            "severity": severity,
            "reason": reason,
            "question_id": question_id,
            "recorded_at": utc_now_iso(),
            "resolved_at": None,
        }
        unresolved.append(entry)
        _append_history(data, "unresolved.recorded", topic)
        return data

    return update_session(aperio_dir, mutate)


def resolve_unresolved(aperio_dir: Path, topic: str, *, note: str = "") -> dict:
    """Mark an unresolved item resolved (it stays in the record)."""

    def mutate(data: dict) -> dict:
        unresolved = data.setdefault("unresolved", [])
        target = next(
            (u for u in unresolved if u.get("topic") == topic and u.get("resolved_at") is None),
            None,
        )
        if target is None:
            raise ValidationError(
                f"no open unresolved item with topic {topic!r}"
            )
        target["resolved_at"] = utc_now_iso()
        if note:
            target["resolution_note"] = note
        _append_history(data, "unresolved.resolved", topic)
        return data

    return update_session(aperio_dir, mutate)


def add_blocker(aperio_dir: Path, *, description: str, source: str = "onboarding") -> dict:
    """Record a current blocker preventing safe progression."""
    if not isinstance(description, str) or not description.strip():
        raise ValidationError("blocker description must be a non-empty string")
    _require_text(description, "blocker description", MAX_DECISION_TEXT_BYTES)

    def mutate(data: dict) -> dict:
        blockers = data.setdefault("blockers", [])
        if description.strip() not in {b.get("description") for b in blockers if b.get("resolved_at") is None}:
            blockers.append(
                {
                    "id": f"BLK-{len(blockers) + 1:04d}",
                    "description": description.strip(),
                    "source": source,
                    "recorded_at": utc_now_iso(),
                    "resolved_at": None,
                }
            )
            _append_history(data, "blocker.recorded", source)
        return data

    return update_session(aperio_dir, mutate)


def resolve_blocker(aperio_dir: Path, blocker_id: str) -> dict:
    def mutate(data: dict) -> dict:
        blockers = data.setdefault("blockers", [])
        target = next((b for b in blockers if b.get("id") == blocker_id), None)
        if target is None:
            raise ValidationError(f"no blocker with id {blocker_id!r}")
        if target.get("resolved_at") is None:
            target["resolved_at"] = utc_now_iso()
            _append_history(data, "blocker.resolved", blocker_id)
        return data

    return update_session(aperio_dir, mutate)


def add_tool_record(aperio_dir: Path, record: dict) -> dict:
    """Append a tool-discovery / installation record (verbatim dict)."""
    if not isinstance(record, dict):
        raise ValidationError("tool record must be a JSON object")
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("tool record requires a non-empty 'name'")

    def mutate(data: dict) -> dict:
        records = data.setdefault("tool_records", [])
        records.append(record)
        _append_history(data, "tool.recorded", name.strip())
        return data

    return update_session(aperio_dir, mutate)


def cancel_session(aperio_dir: Path) -> dict:
    """Explicit operator cancellation (state preserved, marked cancelled)."""

    def mutate(data: dict) -> dict:
        if data["stage"] == STAGE_CANCELLED:
            raise StateError("onboarding session is already cancelled")
        data["cancelled"] = True
        data["cancelled_at"] = utc_now_iso()
        data["stage"] = STAGE_CANCELLED
        _append_history(data, "session.cancelled", "operator")
        return data

    return update_session(aperio_dir, mutate)


def complete_session(aperio_dir: Path) -> dict:
    """Mark onboarding complete (readiness gate decides separately)."""

    def mutate(data: dict) -> dict:
        if data["cancelled"]:
            raise StateError("onboarding session was cancelled")
        data["stage"] = STAGE_COMPLETE
        _append_history(data, "session.completed", "operator")
        return data

    return update_session(aperio_dir, mutate)


# ---------------------------------------------------------------------------
# Validation and publication internals.
# ---------------------------------------------------------------------------


def _reject_symlink(path: Path) -> None:
    """A symlinked session (or lock) is never read or written through."""
    if os.path.islink(path):
        raise StateError(
            f"refusing to follow symlink at {path}; "
            "inspect the link and remove it manually if it is not Aperio content"
        )


def _require_same_workspace(data: dict, path: Path) -> None:
    """Sessions are workspace-bound: a moved/copyied file is refused.

    The stored workspace path is compared against the actual location of
    the ``.aperio`` directory's parent; a mismatch means the session file
    was copied between projects, which is exactly the kind of silent
    context transplant the trust model forbids.
    """
    workspace = data.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return
    actual = str(path.parent.parent.parent)
    if os.path.realpath(workspace) != os.path.realpath(actual):
        raise StateError(
            f"{path} records workspace {workspace} but was found under {actual}; "
            "sessions are not portable between projects. Inspect the file "
            "manually; aperio will not modify it."
        )


def _set_field(data: dict, field: str, value) -> dict:
    data[field] = value
    _append_history(data, "session.updated", field)
    return data


def _append_history(data: dict, event: str, detail: str) -> None:
    history = data.setdefault("history", [])
    history.append({"at": utc_now_iso(), "event": event, "detail": str(detail)[:300]})
    if len(history) > MAX_HISTORY_EVENTS:
        # Keep the most recent events only; the file must not grow without
        # bound over a long onboarding.
        data["history"] = history[-MAX_HISTORY_EVENTS:]


def _require_text(value, name: str, max_bytes: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(
            f"{name} exceeds the {max_bytes}-byte limit; aperio refuses to "
            "truncate operator input"
        )


def _validate_session(data: object, path: Path) -> dict:
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: session must be a JSON object")

    fmt = data.get("format")
    if fmt is not None and fmt != SESSION_FORMAT:
        raise StateError(
            f"{path} declares format {fmt!r}, expected {SESSION_FORMAT!r}; "
            "aperio will not modify it"
        )
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError(f"{path} is corrupt: 'schema_version' must be an integer")
    if version > SESSION_SCHEMA_VERSION:
        raise StateError(
            f"{path} was written with schema version {version}, but this aperio "
            f"supports at most version {SESSION_SCHEMA_VERSION}. Upgrade aperio "
            "or inspect the session manually; aperio will not modify it."
        )
    if version < 1:
        raise StateError(f"{path} uses unrecognized schema version {version}")

    for field in _REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise StateError(f"{path} is corrupt: '{field}' must be a non-empty string")

    if data["stage"] not in STAGES:
        raise StateError(f"{path} is corrupt: unknown stage {data['stage']!r}")
    classification = data.get("classification")
    if classification is not None and classification not in CLASSIFICATIONS:
        raise StateError(f"{path} is corrupt: unknown classification {classification!r}")

    for field in ("created_at", "updated_at"):
        try:
            datetime.fromisoformat(str(data[field]).replace("Z", "+00:00"))
        except ValueError:
            raise StateError(
                f"{path} is corrupt: '{field}' is not an ISO-8601 timestamp"
            ) from None

    answers = data.get("answers")
    if answers is not None:
        if not isinstance(answers, dict):
            raise StateError(f"{path} is corrupt: 'answers' must be an object")
        for qid, entry in answers.items():
            if not isinstance(qid, str) or not qid:
                raise StateError(f"{path} is corrupt: answer ids must be strings")
            if not isinstance(entry, dict) or entry.get("kind") not in ANSWER_KINDS:
                raise StateError(f"{path} is corrupt: answer {qid!r} has unknown kind")

    for list_field, item_check in (
        ("unresolved", _check_unresolved_item),
        ("decisions", _check_decision_item),
        ("approvals", _check_approval_item),
        ("blockers", _check_blocker_item),
    ):
        items = data.get(list_field)
        if items is None:
            continue
        if not isinstance(items, list):
            raise StateError(f"{path} is corrupt: '{list_field}' must be a list")
        for item in items:
            item_check(item, path)

    cancelled = data.get("cancelled")
    if cancelled is not None and not isinstance(cancelled, bool):
        raise StateError(f"{path} is corrupt: 'cancelled' must be a boolean")

    return data


def _check_unresolved_item(item: object, path: Path) -> None:
    if not isinstance(item, dict):
        raise StateError(f"{path} is corrupt: unresolved entries must be objects")
    severity = item.get("severity")
    if severity not in SEVERITIES:
        raise StateError(f"{path} is corrupt: unresolved entry has unknown severity {severity!r}")


def _check_decision_item(item: object, path: Path) -> None:
    if not isinstance(item, dict):
        raise StateError(f"{path} is corrupt: decision entries must be objects")
    if not isinstance(item.get("title"), str) or not item.get("title"):
        raise StateError(f"{path} is corrupt: decision entry missing title")


def _check_approval_item(item: object, path: Path) -> None:
    if not isinstance(item, dict):
        raise StateError(f"{path} is corrupt: approval entries must be objects")
    if not isinstance(item.get("action"), str) or not isinstance(item.get("scope"), str):
        raise StateError(f"{path} is corrupt: approval entry missing action/scope")


def _check_blocker_item(item: object, path: Path) -> None:
    if not isinstance(item, dict):
        raise StateError(f"{path} is corrupt: blocker entries must be objects")
    if not isinstance(item.get("description"), str) or not item.get("description"):
        raise StateError(f"{path} is corrupt: blocker entry missing description")


def _publish_new(aperio_dir: Path, data: dict) -> None:
    """First write: exclusive temp + os.link create-if-not-exists.

    A session that appeared between the existence check and this publish
    is never destroyed — the link fails with EEXIST and the write aborts.
    """
    target = session_path(aperio_dir)
    tmp = session_tmp_path(aperio_dir)
    for candidate in (target, tmp):
        if os.path.islink(candidate):
            raise StateError(
                f"refusing to follow symlink at {candidate}; remove it and "
                "inspect the directory manually"
            )
    if target.exists():
        raise StateError(
            f"refusing to overwrite existing session file {target}"
        )
    payload = json.dumps(data, indent=2) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot write onboarding session at {target}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    try:
        os.link(tmp, target)
    except FileExistsError:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"refusing to overwrite existing session file {target}: a session "
            "appeared while the write was in progress"
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish onboarding session at {target}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    try:
        os.unlink(tmp)
    except OSError:
        pass


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    """Remove a temp file this same call created; never touch anything else."""
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _publish_cas(aperio_dir: Path, data: dict, *, expected_raw: bytes) -> None:
    """Deliberate update: exclusive temp, byte compare, then os.replace.

    The compare protects against writers whose changes completed before
    the compare; callers performing concurrent updates are serialized by
    the session lock, so this is a second layer, not the only one.
    """
    target = session_path(aperio_dir)
    tmp = session_tmp_path(aperio_dir)
    for candidate in (target, tmp):
        if os.path.islink(candidate):
            raise StateError(
                f"refusing to follow symlink at {candidate}; remove it and "
                "inspect the directory manually"
            )
    payload = json.dumps(data, indent=2) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot prepare session update at {tmp}: {exc.strerror or exc}; "
            "nothing was modified"
        ) from None
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot update session: {target} disappeared during the update; "
            "nothing was modified"
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot read {target} for the update check: {exc.strerror or exc}; "
            "nothing was modified"
        ) from None
    if current != expected_raw:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"session at {target} changed while the update was being prepared "
            "(concurrent modification); nothing was modified"
        )
    try:
        os.replace(tmp, target)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish session update at {target}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None


def _aperio_version() -> str:
    from aperio import __version__

    return __version__
