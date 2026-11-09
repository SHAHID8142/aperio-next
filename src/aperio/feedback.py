"""Controlled feedback and improvement (M5, feedback-schema v1).

Feedback turns a quality finding or an operator observation into a
**reviewable proposal**, and — only after an explicit operator decision —
into an actionable lesson stored through the M3 memory contract. The design
borrows every authority rule from M3 instead of inventing a second one:

* A feedback document starts as a ``proposed`` record. Nothing it says is a
  fact because it was written down, and repetition changes nothing.
* An explicit ``feedback-review`` decision (``accepted`` or ``rejected``,
  with a declared reviewer) is the only path to ``accepted``.
* ``feedback-lesson`` writes an *accepted* feedback's lesson into an
  operator-named M3 store **as a ``proposed`` memory**. The M3 review
  contract (``mem-review accept`` / ``--verify``) remains the only way that
  lesson gains authority. No evaluator, and no feedback workflow step, ever
  promotes memory by itself.
* Agent-origin feedback and agent self-reported evaluations are attributed
  and flagged — an agent's report about its own work is never an
  independent evaluation.

Improvement is proposed, never applied silently: an improvement proposal
(improvement-proposal v1) is a versioned, reviewable, reversible document.
Applying one writes a **new** file through the shared atomic publication
path and leaves the operator's original untouched. The profile registry is
code-owned data and cannot be changed by a proposal — a proposal may target
goal instruction fields and quality-request fields only.

Measuring whether a lesson helped is a deterministic comparison of two
quality reports for the *same goal* (``feedback-compare``): resolved
findings, new findings (regressions included, always reported), and
unchanged ones. It reports what changed; claiming that the *lesson* caused
the change stays the operator's judgment.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from aperio.errors import ValidationError
from aperio.goals import validate_goal
from aperio.memstore import MEMORY_TYPES, normalize_scope
from aperio.quality import (
    QUALITY_REPORT_FORMAT,
    SEVERITY_LEVELS,
)

FEEDBACK_SCHEMA_VERSION = 1
FEEDBACK_FORMAT = "aperio-feedback"
FEEDBACK_FILE_MAX_BYTES = 64 * 1024
MAX_FEEDBACK_TEXT_BYTES = 8 * 1024
MAX_EVIDENCE_ITEMS = 12

FEEDBACK_ORIGINS = ("operator", "agent", "import")
FEEDBACK_STATES = ("proposed", "accepted", "rejected")
FEEDBACK_DECISIONS = ("accepted", "rejected")
EVALUATION_SOURCES = ("quality-report", "external-review", "agent-self-report",
                      "human")
TASK_REF_KINDS = ("goal", "plan-task", "none")
OUTPUT_REF_KINDS = ("quality-report", "deliverable", "exchange", "other")

PROPOSAL_FORMAT = "aperio-improvement-proposal"
PROPOSAL_VERSION = 1
PROPOSAL_TARGETS = ("goal", "quality-request")

_LESSON_DEFAULT_TYPE = "lesson"
_LESSON_ALLOWED_TYPES = (
    "lesson", "convention", "requirement", "limitation", "decision",
    "observation",
)

_ISO_UTC_Z_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _check_text(value: object, where: str, *, max_bytes: int = MAX_FEEDBACK_TEXT_BYTES,
                single_line: bool = False) -> str:
    from aperio.inputs import check_no_foreign_line_separators

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be non-empty text")
    if "\x00" in value:
        raise ValidationError(f"{where} must not contain NUL characters")
    artifact, _, field = where.rpartition(".")
    check_no_foreign_line_separators(artifact or "feedback", field or where,
                                     value)
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValidationError(
            f"{where} must be at most {max_bytes} UTF-8 bytes"
        )
    if single_line and "\n" in value:
        raise ValidationError(f"{where} must be a single line")
    return value


def _optional_text(value: object, where: str, *, max_bytes: int = MAX_FEEDBACK_TEXT_BYTES) -> str | None:
    if value is None:
        return None
    return _check_text(value, where, max_bytes=max_bytes)


def _unknown_keys(data: Mapping, allowed: Sequence[str], where: str) -> None:
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise ValidationError(f"{where}: unknown fields: " + ", ".join(extra))


# ---------------------------------------------------------------------------
# feedback identity
# ---------------------------------------------------------------------------


def feedback_content_sha256(feedback: dict) -> str:
    """Content-derived identity: the problem, its context, and the lesson.

    Review history, reviewer identifiers, timestamps, and the accepted state
    are excluded on purpose — deciding about feedback never changes what the
    feedback says, so identity survives review and re-checking.
    """

    canonical = {
        "task_ref": feedback.get("task_ref"),
        "output_ref": feedback.get("output_ref"),
        "evaluation": feedback.get("evaluation"),
        "problem": feedback.get("problem"),
        "evidence": list(feedback.get("evidence") or []),
        "severity": feedback.get("severity"),
        "proposed_correction": feedback.get("proposed_correction"),
        "lesson": feedback.get("lesson"),
        "memory_type": feedback.get("memory_type"),
        "scope": normalize_scope(feedback.get("scope")),
    }
    return hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def feedback_identity(feedback: dict) -> str:
    return "FB-" + feedback_content_sha256(feedback)[:12]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

FEEDBACK_KEYS = (
    "feedback_schema_version",
    "feedback_id",
    "task_ref",
    "output_ref",
    "evaluation",
    "problem",
    "evidence",
    "origin",
    "actor",
    "severity",
    "proposed_correction",
    "correction_accepted",
    "scope",
    "lesson",
    "memory_type",
    "quality_rules_version",
    "created_at",
    "state",
    "reviews",
)


def validate_feedback(data: object) -> dict:
    """Validate a feedback document (feedback-schema v1) into normalized
    form. Unknown fields, bad enums, and oversized text are refused — never
    repaired silently."""

    if not isinstance(data, dict):
        raise ValidationError("feedback: the document must be a JSON object")
    _unknown_keys(data, FEEDBACK_KEYS, "feedback")
    version = data.get("feedback_schema_version", FEEDBACK_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError(
            "feedback: 'feedback_schema_version' must be an integer"
        )
    if version != FEEDBACK_SCHEMA_VERSION:
        raise ValidationError(
            f"feedback: unsupported feedback_schema_version {version}; this "
            f"aperio supports version {FEEDBACK_SCHEMA_VERSION}"
        )

    declared_id = data.get("feedback_id")
    if declared_id is not None and not (
        isinstance(declared_id, str) and declared_id.startswith("FB-")
    ):
        raise ValidationError(
            "feedback: 'feedback_id' must be an 'FB-' identity or absent"
        )

    task_ref = _normalize_ref(
        data.get("task_ref"), ("goal", "plan-task", "none"), "feedback.task_ref"
    )
    output_ref = _normalize_ref(
        data.get("output_ref"),
        OUTPUT_REF_KINDS,
        "feedback.output_ref",
    )

    evaluation = _normalize_evaluation(data.get("evaluation"))

    problem = _check_text(data.get("problem"), "feedback.problem")
    evidence_raw = data.get("evidence", [])
    if not isinstance(evidence_raw, list) or len(evidence_raw) > MAX_EVIDENCE_ITEMS:
        raise ValidationError(
            f"feedback: 'evidence' must be a list of at most "
            f"{MAX_EVIDENCE_ITEMS} references"
        )
    evidence = [
        _check_text(item, f"feedback.evidence[{index}]", max_bytes=512,
                    single_line=True)
        for index, item in enumerate(evidence_raw)
    ]

    origin = data.get("origin", "operator")
    if origin not in FEEDBACK_ORIGINS:
        raise ValidationError(
            "feedback: 'origin' must be one of: " + ", ".join(FEEDBACK_ORIGINS)
        )
    actor = _optional_text(data.get("actor"), "feedback.actor",
                           max_bytes=200)
    severity = data.get("severity", "medium")
    if severity not in SEVERITY_LEVELS:
        raise ValidationError(
            "feedback: 'severity' must be one of: " + ", ".join(SEVERITY_LEVELS)
        )
    proposed_correction = _optional_text(
        data.get("proposed_correction"), "feedback.proposed_correction"
    )
    scope = normalize_scope(data.get("scope"))
    lesson = _optional_text(data.get("lesson"), "feedback.lesson")
    memory_type = data.get("memory_type", _LESSON_DEFAULT_TYPE)
    if memory_type not in _LESSON_ALLOWED_TYPES:
        raise ValidationError(
            "feedback: 'memory_type' must be one of: "
            + ", ".join(_LESSON_ALLOWED_TYPES)
        )
    rules_version = data.get("quality_rules_version", 1)
    if isinstance(rules_version, bool) or not isinstance(rules_version, int):
        raise ValidationError(
            "feedback: 'quality_rules_version' must be an integer"
        )
    created_at = (
        _check_iso_utc(data.get("created_at"), "feedback.created_at")
        if data.get("created_at") is not None
        else None
    )
    state = data.get("state", "proposed")
    if state not in FEEDBACK_STATES:
        raise ValidationError(
            "feedback: 'state' must be one of: " + ", ".join(FEEDBACK_STATES)
        )

    reviews_raw = data.get("reviews", [])
    if not isinstance(reviews_raw, list) or len(reviews_raw) > 32:
        raise ValidationError(
            "feedback: 'reviews' must be a list of at most 32 review entries"
        )
    reviews = [_normalize_review(entry, index)
               for index, entry in enumerate(reviews_raw)]

    feedback = {
        "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
        "feedback_id": declared_id,
        "task_ref": task_ref,
        "output_ref": output_ref,
        "evaluation": evaluation,
        "problem": problem,
        "evidence": evidence,
        "origin": origin,
        "actor": actor,
        "severity": severity,
        "proposed_correction": proposed_correction,
        "correction_accepted": data.get("correction_accepted"),
        "scope": scope,
        "lesson": lesson,
        "memory_type": memory_type,
        "quality_rules_version": rules_version,
        "created_at": created_at,
        "state": state,
        "reviews": reviews,
    }
    _check_state_consistency(feedback)
    if declared_id is not None and declared_id != feedback_identity(feedback):
        raise ValidationError(
            "feedback: 'feedback_id' does not match the document's content "
            "identity; recompute it or remove the field"
        )
    return feedback


def _normalize_ref(value: object, kinds: Sequence[str], where: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be an object or null")
    _unknown_keys(value, ("kind", "id", "note"), where)
    kind = value.get("kind", "none")
    if kind not in kinds:
        raise ValidationError(
            f"{where}.kind must be one of: " + ", ".join(kinds)
        )
    return {
        "kind": kind,
        "id": _optional_text(value.get("id"), f"{where}.id", max_bytes=200),
        "note": _optional_text(value.get("note"), f"{where}.note",
                               max_bytes=1000),
    }


def _normalize_evaluation(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("feedback.evaluation must be an object or null")
    _unknown_keys(value, ("source", "result", "report_sha256", "note"),
                  "feedback.evaluation")
    source = value.get("source", "human")
    if source not in EVALUATION_SOURCES:
        raise ValidationError(
            "feedback.evaluation.source must be one of: "
            + ", ".join(EVALUATION_SOURCES)
        )
    result = value.get("result", "unknown")
    if result not in ("passed", "failed", "unknown"):
        raise ValidationError(
            "feedback.evaluation.result must be one of: passed, failed, "
            "unknown"
        )
    return {
        "source": source,
        "result": result,
        "report_sha256": _optional_text(
            value.get("report_sha256"), "feedback.evaluation.report_sha256",
            max_bytes=64),
        "note": _optional_text(value.get("note"),
                               "feedback.evaluation.note", max_bytes=1000),
    }


def _check_iso_utc(value: object, where: str) -> str:
    """Calendar-valid ISO-8601 UTC (``YYYY-MM-DDTHH:MM:SSZ``), as the plan
    lifecycle uses."""

    from datetime import datetime

    text = _check_text(value, where, max_bytes=40, single_line=True)
    if not _ISO_UTC_Z_RE.match(text):
        raise ValidationError(
            f"{where} must be ISO-8601 UTC of the form "
            "'YYYY-MM-DDTHH:MM:SSZ'"
        )
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValidationError(
            f"{where} is not a calendar-valid UTC timestamp: {text}"
        ) from None
    return text


def _normalize_review(entry: object, index: int) -> dict:
    if not isinstance(entry, dict):
        raise ValidationError(f"feedback.reviews[{index}] must be an object")
    _unknown_keys(entry, ("decision", "reviewer", "at", "note"),
                  f"feedback.reviews[{index}]")
    decision = entry.get("decision")
    if decision not in FEEDBACK_DECISIONS:
        raise ValidationError(
            f"feedback.reviews[{index}].decision must be one of: "
            + ", ".join(FEEDBACK_DECISIONS)
        )
    reviewer = _check_text(entry.get("reviewer"),
                           f"feedback.reviews[{index}].reviewer",
                           max_bytes=200, single_line=True)
    at = _check_iso_utc(entry.get("at"), f"feedback.reviews[{index}].at")
    note = _optional_text(entry.get("note"),
                          f"feedback.reviews[{index}].note", max_bytes=1000)
    return {"decision": decision, "reviewer": reviewer, "at": at, "note": note}


def _check_state_consistency(feedback: dict) -> None:
    reviews = feedback["reviews"]
    if not reviews:
        if feedback["state"] != "proposed":
            raise ValidationError(
                "feedback: a document without review entries must be "
                "'proposed'"
            )
        if feedback["correction_accepted"] is not None:
            raise ValidationError(
                "feedback: 'correction_accepted' can only be set by a "
                "recorded review"
            )
        return
    last = reviews[-1]
    if feedback["state"] != last["decision"]:
        raise ValidationError(
            "feedback: 'state' must match the last recorded review decision"
        )
    feedback["correction_accepted"] = last["decision"] == "accepted"


def feedback_revision(feedback: dict) -> int:
    """1 + the number of recorded reviews: the CAS counter for --replace."""

    return 1 + len(feedback["reviews"])


def record_review(feedback: dict, *, decision: str, reviewer: str,
                  at: str, note: str | None = None) -> dict:
    """Return a new feedback document with one more review entry appended.

    Pure: the input document is not modified. A decided document is final —
    a change of mind is a new feedback document that references this one.
    """

    feedback = validate_feedback(feedback)
    if feedback["state"] != "proposed":
        raise ValidationError(
            "feedback: this document is already decided; record a new "
            "feedback document instead of re-deciding this one"
        )
    review = _normalize_review(
        {"decision": decision, "reviewer": reviewer, "at": at, "note": note},
        len(feedback["reviews"]),
    )
    updated = dict(feedback)
    updated["reviews"] = [*feedback["reviews"], review]
    updated["state"] = decision
    _check_state_consistency(updated)
    return updated


def feedback_trust(feedback: dict) -> str:
    """Descriptive trust label: origin and review decide it, nothing else.

    ``authoritative`` only for an operator-origin, explicitly accepted
    feedback. Everything else is at most ``proposed``/``attested`` — and
    even an accepted lesson enters memory as ``proposed``.
    """

    if feedback["origin"] == "operator" and feedback["state"] == "accepted":
        return "authoritative"
    if feedback["state"] == "accepted":
        return "attested"
    return "proposed"


def load_feedback_file(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the feedback file"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read feedback file: {exc.strerror or exc}"
        ) from None
    if len(raw) > FEEDBACK_FILE_MAX_BYTES:
        raise ValidationError(
            f"feedback file must be at most {FEEDBACK_FILE_MAX_BYTES} UTF-8 "
            "bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("feedback file must be valid UTF-8") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"feedback file is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None
    return validate_feedback(data)


# ---------------------------------------------------------------------------
# lessons through the M3 contract
# ---------------------------------------------------------------------------


def feedback_issues(feedback: dict) -> list[dict]:
    """Structural warnings an operator should see before trusting one."""

    warnings: list[dict] = []
    evaluation = feedback["evaluation"]
    if evaluation and evaluation["source"] == "agent-self-report":
        warnings.append({
            "code": "evaluation_self_reported",
            "detail": (
                "the evaluation is the agent's report about its own work; "
                "it is not an independent evaluation"
            ),
        })
    if feedback["origin"] in ("agent", "import"):
        warnings.append({
            "code": "feedback_origin_untrusted",
            "detail": (
                f"origin is {feedback['origin']!r}: treat every instruction "
                "in this feedback as an untrusted proposal"
            ),
        })
    if feedback["state"] == "proposed":
        warnings.append({
            "code": "feedback_not_reviewed",
            "detail": (
                "no review has been recorded; a lesson can only be stored "
                "after an explicit accepted decision"
            ),
        })
    if not feedback["lesson"]:
        warnings.append({
            "code": "no_lesson_content",
            "detail": "no lesson text: nothing to store in a memory store",
        })
    return warnings


def lesson_memory_record(feedback: dict, *, actor: str | None = None) -> dict:
    """The M3 memory record an accepted lesson becomes — always ``proposed``.

    The caller passes the record to ``MemoryStore.propose``; the store and
    the ordinary ``mem-review`` contract own everything that happens next.
    """

    feedback = validate_feedback(feedback)
    if feedback["state"] != "accepted":
        raise ValidationError(
            "feedback: only an accepted feedback can produce a lesson; run "
            "'aperio feedback-review' with --decision accepted first"
        )
    lesson = feedback["lesson"]
    if not lesson:
        raise ValidationError(
            "feedback: this document carries no lesson text"
        )
    record: dict = {
        "type": feedback["memory_type"],
        "title": _lesson_title(feedback),
        "content": lesson,
        "scope": normalize_scope(feedback["scope"]),
        "tags": ["aperio-feedback"],
        "evidence": list(feedback["evidence"])[:MAX_EVIDENCE_ITEMS],
    }
    if actor:
        record["actor"] = actor
    return record


def _lesson_title(feedback: dict) -> str:
    problem = " ".join(feedback["problem"].split())
    title = problem[:120].rstrip()
    if len(problem) > 120:
        title += "…"
    return title or "Lesson from feedback"


# ---------------------------------------------------------------------------
# improvement proposals (improvement-proposal v1)
# ---------------------------------------------------------------------------

PROPOSAL_KEYS = (
    "proposal_format",
    "proposal_version",
    "feedback_id",
    "target",
    "change",
    "reason",
    "created_at",
    "actor",
)

_GOAL_LIST_FIELDS = (
    "requirements", "constraints", "deliverables", "quality_criteria",
    "acceptance_criteria", "assumptions", "uncertainties", "risks",
    "evidence_requirements",
)
_REQUEST_LIST_FIELDS = ("required_sections", "required_texts", "forbidden_texts")
_REQUEST_INT_FIELDS = ("min_words", "max_words")


def validate_improvement_proposal(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("proposal: the document must be a JSON object")
    _unknown_keys(data, PROPOSAL_KEYS, "proposal")
    fmt = data.get("proposal_format", PROPOSAL_FORMAT)
    if fmt != PROPOSAL_FORMAT:
        raise ValidationError(
            f"proposal: unsupported proposal_format {fmt!r}; expected "
            f"{PROPOSAL_FORMAT!r}"
        )
    version = data.get("proposal_version", PROPOSAL_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != PROPOSAL_VERSION:
        raise ValidationError(
            "proposal: unsupported proposal_version; this aperio supports "
            f"version {PROPOSAL_VERSION}"
        )
    target = data.get("target")
    if target not in PROPOSAL_TARGETS:
        raise ValidationError(
            "proposal: 'target' must be one of: " + ", ".join(PROPOSAL_TARGETS)
        )
    change = data.get("change")
    if not isinstance(change, dict):
        raise ValidationError("proposal: 'change' must be an object")
    _unknown_keys(change, ("action", "field", "value"), "proposal.change")
    action = change.get("action")
    if action not in ("add", "remove", "set"):
        raise ValidationError(
            "proposal.change.action must be one of: add, remove, set"
        )
    field = change.get("field")
    if not isinstance(field, str) or not field:
        raise ValidationError("proposal.change.field must be a field name")
    value = change.get("value")
    if action in ("add", "set") and value is None:
        raise ValidationError(
            "proposal.change.value is required for add/set"
        )
    if action == "add" and target == "goal":
        if field not in _GOAL_LIST_FIELDS or not isinstance(value, str):
            raise ValidationError(
                "proposal: goal 'add' targets an instruction list field "
                f"({', '.join(_GOAL_LIST_FIELDS)}) with a text value"
            )
    if action == "remove" and target == "goal":
        if field not in _GOAL_LIST_FIELDS or not isinstance(value, str):
            raise ValidationError(
                "proposal: goal 'remove' targets an instruction list field "
                f"({', '.join(_GOAL_LIST_FIELDS)}) with the exact text to "
                "remove"
            )
    if action == "set" and target == "goal":
        allowed_set = _GOAL_LIST_FIELDS + ("audience",)
        if field not in allowed_set or not isinstance(value, str):
            raise ValidationError(
                "proposal: goal 'set' supports single-text fields only ("
                + ", ".join(allowed_set) + ")"
            )
    if target == "quality-request":
        if action in ("add", "remove"):
            if field not in _REQUEST_LIST_FIELDS or not isinstance(value, str):
                raise ValidationError(
                    "proposal: quality-request 'add'/'remove' targets "
                    f"{', '.join(_REQUEST_LIST_FIELDS)} with a text value"
                )
        else:
            if field not in _REQUEST_INT_FIELDS or isinstance(value, bool) \
                    or not isinstance(value, int) or value < 0:
                raise ValidationError(
                    "proposal: quality-request 'set' targets "
                    f"{', '.join(_REQUEST_INT_FIELDS)} with a non-negative "
                    "integer"
                )
    reason = _check_text(data.get("reason"), "proposal.reason")
    feedback_id = _optional_text(data.get("feedback_id"),
                                 "proposal.feedback_id", max_bytes=64)
    return {
        "proposal_format": PROPOSAL_FORMAT,
        "proposal_version": PROPOSAL_VERSION,
        "feedback_id": feedback_id,
        "target": target,
        "change": {"action": action, "field": field, "value": value},
        "reason": reason,
        "created_at": _optional_text(data.get("created_at"),
                                     "proposal.created_at", max_bytes=40),
        "actor": _optional_text(data.get("actor"), "proposal.actor",
                                max_bytes=200),
    }


def load_proposal_file(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError("refusing to follow a symlink for the proposal file")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read proposal file: {exc.strerror or exc}"
        ) from None
    if len(raw) > FEEDBACK_FILE_MAX_BYTES:
        raise ValidationError(
            f"proposal file must be at most {FEEDBACK_FILE_MAX_BYTES} UTF-8 "
            "bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("proposal file must be valid UTF-8") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"proposal file is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None
    return validate_improvement_proposal(data)


def apply_proposal_to_goal(proposal: dict, goal_data: dict) -> dict:
    """Return a *new* goal document with the proposal applied.

    Pure and reversible by construction: the caller's dict is untouched, the
    result is written by the caller to a file the operator chose. Refuses
    no-op applications (adding text that is already present, removing text
    that is not) so a proposal cannot silently change nothing or duplicate
    instructions.
    """

    proposal = validate_improvement_proposal(proposal)
    if proposal["target"] != "goal":
        raise ValidationError(
            "proposal: this proposal targets the quality request, not a goal"
        )
    goal = validate_goal(goal_data)
    change = proposal["change"]
    action, field, value = change["action"], change["field"], change["value"]
    updated = dict(goal)
    if action == "add":
        items = list(goal[field])
        if any(item.strip().casefold() == value.strip().casefold()
               for item in items):
            raise ValidationError(
                f"proposal: '{value}' is already present in goal.{field}; "
                "nothing to add"
            )
        items.append(value)
        updated[field] = items
    elif action == "remove":
        items = list(goal[field])
        matches = [
            item for item in items
            if item.strip().casefold() == value.strip().casefold()
        ]
        if not matches:
            raise ValidationError(
                f"proposal: '{value}' is not present in goal.{field}; "
                "nothing to remove"
            )
        updated[field] = [
            item for item in items
            if item.strip().casefold() != value.strip().casefold()
        ]
    else:
        if goal.get(field) == value:
            raise ValidationError(
                f"proposal: goal.{field} already has that value"
            )
        updated[field] = value
    return validate_goal(updated)


def apply_proposal_to_request(proposal: dict, request_data: dict) -> dict:
    """Return a *new* quality request with the proposal applied (pure)."""

    from aperio.quality import validate_quality_request

    proposal = validate_improvement_proposal(proposal)
    if proposal["target"] != "quality-request":
        raise ValidationError(
            "proposal: this proposal targets a goal, not the quality request"
        )
    request = validate_quality_request(request_data)
    change = proposal["change"]
    action, field, value = change["action"], change["field"], change["value"]
    updated = dict(request)
    if action in ("add", "remove"):
        items = list(request[field])
        present = any(item.strip().casefold() == value.strip().casefold()
                      for item in items)
        if action == "add":
            if present:
                raise ValidationError(
                    f"proposal: '{value}' is already in "
                    f"request.{field}; nothing to add"
                )
            items.append(value)
        else:
            if not present:
                raise ValidationError(
                    f"proposal: '{value}' is not in request.{field}; "
                    "nothing to remove"
                )
            items = [item for item in items
                     if item.strip().casefold() != value.strip().casefold()]
        updated[field] = items
    else:
        if request[field] == value:
            raise ValidationError(
                f"proposal: request.{field} already has that value"
            )
        updated[field] = value
    return validate_quality_request(updated)


# ---------------------------------------------------------------------------
# improvement measurement: compare two quality reports for the same goal
# ---------------------------------------------------------------------------

COMPARISON_FORMAT = "aperio-feedback-comparison"
COMPARISON_VERSION = 1


def _load_quality_report(path: str | Path, label: str) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            f"refusing to follow a symlink for the {label} report"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read {label} report: {exc.strerror or exc}"
        ) from None
    if len(raw) > FEEDBACK_FILE_MAX_BYTES * 16:
        raise ValidationError(f"{label} report is oversized")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"{label} report is not valid UTF-8 JSON: {exc}"
        ) from None
    if not isinstance(data, dict) or data.get("format") != QUALITY_REPORT_FORMAT:
        raise ValidationError(
            f"{label} report is not an {QUALITY_REPORT_FORMAT} document"
        )
    if data.get("format_version") != 1:
        raise ValidationError(
            f"{label} report has unsupported format_version "
            f"{data.get('format_version')!r}"
        )
    content = data.get("content")
    if not isinstance(content, dict):
        raise ValidationError(f"{label} report has no content object")
    return data


def compare_quality_reports(before_path: str | Path,
                            after_path: str | Path) -> dict:
    """Deterministic before/after comparison for the same goal.

    Resolved = findings present before and absent after. New = findings
    present after and absent before — reported as potential regressions
    whatever their status. The comparison never claims *why* anything
    changed; attributing the difference to a lesson is an operator judgment.
    """

    before = _load_quality_report(before_path, "before")
    after = _load_quality_report(after_path, "after")
    before_content = before["content"]
    after_content = after["content"]
    before_goal = before_content.get("goal") or {}
    after_goal = after_content.get("goal") or {}
    if before_goal.get("goal_sha256") != after_goal.get("goal_sha256"):
        raise ValidationError(
            "comparison: the two reports describe different goals (content "
            "hashes differ); a before/after comparison is only meaningful "
            "for the same goal"
        )

    def by_id(content: dict) -> dict:
        return {
            issue["issue_id"]: issue for issue in content.get("issues", [])
        }

    before_issues = by_id(before_content)
    after_issues = by_id(after_content)
    resolved = sorted(set(before_issues) - set(after_issues))
    new = sorted(set(after_issues) - set(before_issues))
    unchanged = sorted(set(before_issues) & set(after_issues))

    before_confirmed = sum(
        1 for issue in before_issues.values() if issue["status"] == "confirmed"
    )
    after_confirmed = sum(
        1 for issue in after_issues.values() if issue["status"] == "confirmed"
    )
    new_confirmed = sorted(
        issue["issue_id"] for issue in after_issues.values()
        if issue["issue_id"] in set(new) and issue["status"] == "confirmed"
    )
    # Regressions are new findings that demand attention (confirmed failures
    # and warnings). A new `unknown`/`recommendation`/info finding is
    # informational: it is listed, but it does not call the change a
    # regression.
    regression_ids = sorted(
        issue["issue_id"] for issue in after_issues.values()
        if issue["issue_id"] in set(new)
        and issue["status"] in ("confirmed", "warning")
    )
    informational_ids = sorted(
        issue_id for issue_id in new if issue_id not in set(regression_ids)
    )
    # Symmetric honesty on the resolution side: only findings that demanded
    # attention (confirmed/warning) count as resolved. An `unknown` finding
    # disappearing says nothing about better work.
    resolved_attention = sorted(
        issue["issue_id"] for issue in before_issues.values()
        if issue["issue_id"] in set(resolved)
        and issue["status"] in ("confirmed", "warning")
    )
    if resolved_attention and not regression_ids:
        outcome = "improved"
    elif regression_ids and not resolved_attention:
        outcome = "regressed"
    elif resolved_attention and regression_ids:
        outcome = "mixed"
    else:
        outcome = "unchanged"
    content = {
        "kind": "feedback-comparison",
        "goal": {
            "goal_identity": after_goal.get("goal_identity"),
            "goal_sha256": after_goal.get("goal_sha256"),
        },
        "before": {
            "report_sha256": _report_digest(before),
            "counts": before_content["counts"],
            "confirmed_count": before_confirmed,
        },
        "after": {
            "report_sha256": _report_digest(after),
            "counts": after_content["counts"],
            "confirmed_count": after_confirmed,
        },
        "resolved_issue_ids": resolved,
        "resolved_attention_issue_ids": resolved_attention,
        "new_issue_ids": new,
        "new_confirmed_issue_ids": new_confirmed,
        "regression_issue_ids": regression_ids,
        "informational_issue_ids": informational_ids,
        "unchanged_issue_ids": unchanged,
        "outcome": outcome,
        "limitations": [
            "this comparison reports differences between two quality "
            "reports for the same goal; it cannot attribute a difference "
            "to any lesson, change, or cause",
            "a resolved warning may simply reflect different deliverable "
            "text, not better work",
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    from datetime import datetime, timezone

    return {
        "format": COMPARISON_FORMAT,
        "format_version": COMPARISON_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_sha256": digest,
        "content": content,
    }


def _report_digest(report: dict) -> str:
    sha = report.get("content_sha256")
    if isinstance(sha, str) and len(sha) == 64:
        return sha
    return hashlib.sha256(
        json.dumps(
            report.get("content"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def render_comparison_markdown(comparison: dict) -> str:
    content = comparison["content"]
    lines = ["# Feedback comparison", ""]
    goal = content["goal"]
    lines.append(f"Goal: `{goal['goal_identity']}`")
    lines.append("")
    before = content["before"]
    after = content["after"]
    lines.append(
        f"- Before: {before['counts']['total']} finding(s), "
        f"{before['confirmed_count']} confirmed"
    )
    lines.append(
        f"- After: {after['counts']['total']} finding(s), "
        f"{after['confirmed_count']} confirmed"
    )
    lines.append(f"- Outcome: **{content['outcome']}**")
    lines.append("")
    lines.append(
        f"Resolved findings: {len(content['resolved_issue_ids'])}; "
        f"new findings needing review (regressions): "
        f"{len(content['regression_issue_ids'])}; "
        f"new informational findings: "
        f"{len(content['informational_issue_ids'])}; "
        f"unchanged: {len(content['unchanged_issue_ids'])}."
    )
    lines.append("")
    if content["regression_issue_ids"]:
        lines.append("New findings needing review (potential regressions):")
        lines.append("")
        for issue_id in content["regression_issue_ids"]:
            lines.append(f"- `{issue_id}`")
        lines.append("")
    if content["informational_issue_ids"]:
        lines.append("New informational findings (unknown/recommendation — "
                     "not counted as regressions):")
        lines.append("")
        for issue_id in content["informational_issue_ids"]:
            lines.append(f"- `{issue_id}`")
        lines.append("")
    for limitation in content["limitations"]:
        lines.append(f"- *{limitation}*")
    lines.append("")
    return "\n".join(lines)
