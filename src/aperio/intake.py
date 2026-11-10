"""Brain Dump Guard: durable intake and classification (intake-schema v1).

The operator thinks faster than an agent works. Mid-task messages — bug
reports, new ideas, side questions, corrections — arrive while a task is
active, and an unguarded agent may lose focus, switch tasks, or treat a
passing thought as an instruction. This module is the runtime-enforced
part of the Brain Dump Guard:

* **Capture preserves everything.** Each `aperio capture` appends one
  immutable, chain-hashed event to the project's intake log
  (`.aperio/inbox.jsonl`) holding the raw message and every classified
  item. Nothing is lost, nothing is executed, and the active task is
  untouched.
* **Classification is a deterministic heuristic, and says so.** Lexicon
  cues route each item into a category (idea, question, bug-report,
  correction, feature-request, task) and keyword overlap against the
  active task proposes related/unrelated routing. Every item records
  `classification_basis: heuristic-keyword` and the cue that matched;
  no-cue items are `ambiguous`. This is conservative triage for a human
  reviewer — it is **not** language understanding, and the output says
  so wherever it is rendered.
* **Emergency authority never comes from message content.** Phrases like
  "urgent" or "ASAP" inside the message are *detected and reported* with
  `authority: none` — quoted text, imported documents, and agent output
  can never grant interruption. Only the operator's explicit `--urgent`
  flag on the capture invocation (the trusted operator channel) records
  `authority: operator-requested`.
* **Nothing captured executes.** Every item carries
  `execution_authorization: none`. Related items are noted for a safe
  checkpoint-then-scope-update; unrelated items queue for after the
  active task; ideas are ideas. Turning a pending item into executable
  work is the separate, explicitly reviewed promotion path (see the
  plan bridge), which keeps inbox capture strictly separate from
  approved-plan mutation.

The log replays like the memory store: current state is derived, never
rewritten; damaged lines are reported and skipped; a broken chain
refuses further appends. The host-integration honesty rule applies: on
hosts where the agent itself cooperates (the same-chat instruction
protocol, documented in docs/brain-dump.md), protection is cooperative
and weaker than this runtime path — the two are never presented as
equivalent guarantees.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.locks import acquire_lock, release_lock
from aperio.memstore import line_sha256

INTAKE_FORMAT = "aperio-intake"
INTAKE_SCHEMA_VERSION = 1

MAX_MESSAGE_BYTES = 16 * 1024
MAX_ITEMS_PER_MESSAGE = 20
MAX_ITEM_TEXT_BYTES = 4 * 1024
MAX_ITEMS = 2000
MAX_ACTOR_BYTES = 200
MAX_NOTE_BYTES = 1000

ITEM_CATEGORIES = (
    "related-task",
    "unrelated-task",
    "bug-report",
    "feature-request",
    "idea",
    "question",
    "correction",
    "emergency",
    "ambiguous",
)

RESOLVE_ACTIONS = ("acknowledge", "dismiss")

_CHAIN_FIELD = "chain_sha256"

# Deterministic lexicon cues. Order matters: idea/question/correction cues
# outrank imperative cues, because "it would be nice to add dark mode" is
# an idea, not a task. Matching is case-insensitive word-boundary regex —
# deliberately shallow, deliberately labeled heuristic.
_IDEA_CUES = (
    r"would\s+be\s+nice", r"someday", r"might\s+be\s+nice",
    r"what\s+if", r"perhaps", r"i\s+wish", r"\bidea\b", r"maybe\s+we",
    r"nice\s+to\s+have", r"future", r"eventually",
)
_QUESTION_CUES = (
    r"\?\s*$", r"^(what|when|where|why|how|should|can|could|is|are|do|does|did|who)\b",
)
_CORRECTION_CUES = (
    r"^actually\b", r"\binstead\b", r"^no[,.]?\s", r"\btypo\b",
    r"should\s+be\b", r"^correction\b", r"i\s+said\b", r"not\s+x\b",
)
_BUG_CUES = (
    r"\bbroken\b", r"\bbug\b", r"\bcrash", r"\berror\b", r"\bfails?\b",
    r"does\s*n[o']t\s+work", r"not\s+working", r"\bwrong\b", r"\bregression\b",
    r"\bexception\b", r"\bstack\s?trace\b",
)
_IMPERATIVE_CUES = (
    r"^(please\s+)?(add|fix|implement|create|build|remove|update|change|make|write|deploy|refactor|rename|delete|install|configure)\b",
    r"\bwe\s+should\s+(add|fix|implement|create|build|remove|update|change)\b",
    r"\bneed(s|ed)?\s+to\s+(add|fix|implement|create|build|remove|update|change)\b",
)
_URGENCY_LANGUAGE_CUES = (
    r"\burgent(ly)?\b", r"\basap\b", r"right\s+now", r"immediately",
    r"emergency", r"drop\s+everything", r"stop\s+everything",
)

_RELATED_TERM_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "our",
    "your", "their", "have", "has", "was", "were", "are", "will", "would",
    "should", "could", "does", "did", "its", "it's", "not", "but", "all",
    "any", "can", "out", "use", "when", "then", "than", "them", "they",
})

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _check_text(value: object, where: str, *, max_bytes: int,
                single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be non-empty text")
    if "\x00" in value:
        raise ValidationError(f"{where} must not contain NUL characters")
    artifact, _, field = where.rpartition(".")
    check_no_foreign_line_separators(artifact or "intake", field or where,
                                     value)
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(
            f"{where} must be at most {max_bytes} UTF-8 bytes"
        )
    if single_line and "\n" in value:
        raise ValidationError(f"{where} must be single-line text")
    return value


def _terms(text: str) -> set[str]:
    return {
        term for term in _RELATED_TERM_RE.findall(text.casefold())
        if term not in _STOPWORDS
    }


def _split_items(message: str) -> list[str]:
    """Conservative split of one message into candidate items.

    Sentences and lines are candidate items; consecutive fragments of the
    same sentence are never merged or rewritten. A message with no
    sentence punctuation stays one item.
    """

    fragments = [
        fragment.strip()
        for fragment in _SENTENCE_SPLIT_RE.split(message)
        if fragment.strip()
    ]
    return fragments or [message.strip()]


def _match_cue(cues: tuple[str, ...], text: str) -> str | None:
    lowered = text.casefold()
    for cue in cues:
        if re.search(cue, lowered):
            return cue
    return None


def classify_item(text: str, *, active_task_terms: set[str] | None,
                  operator_urgent: bool) -> dict:
    """Classify one captured item. Deterministic, shallow, honest.

    The emergency category is reachable **only** through
    ``operator_urgent`` — the trusted operator channel. Urgency language
    inside the text is detected and reported with ``authority: none``;
    it never grants an interruption.
    """

    text = _check_text(text, "item.text", max_bytes=MAX_ITEM_TEXT_BYTES)
    cue: str | None = None
    if operator_urgent:
        category = "emergency"
        authority = "operator-requested"
    else:
        authority = "none"
        idea = _match_cue(_IDEA_CUES, text)
        if idea:
            category, cue = "idea", idea
        elif _match_cue(_QUESTION_CUES, text):
            category, cue = "question", "question-form"
        elif _match_cue(_CORRECTION_CUES, text):
            category, cue = "correction", _match_cue(_CORRECTION_CUES, text)
        elif _match_cue(_BUG_CUES, text):
            category, cue = "bug-report", _match_cue(_BUG_CUES, text)
        elif _match_cue(_IMPERATIVE_CUES, text):
            category, cue = "feature-request", _match_cue(_IMPERATIVE_CUES,
                                                          text)
        else:
            category, cue = "ambiguous", None

    routing = "queued"
    routing_basis = "no-active-task"
    related_basis = None
    if active_task_terms is not None:
        item_terms = _terms(text)
        if not item_terms:
            routing, routing_basis = "ambiguous", "no-significant-terms"
        else:
            overlap = len(item_terms & active_task_terms) / len(item_terms)
            related_basis = round(overlap, 4)
            if category == "emergency":
                # operator-requested urgency: checkpoint and surface now
                routing, routing_basis = ("urgent-operator-requested",
                                          "operator-flag")
            elif category == "idea":
                routing, routing_basis = "idea-note", "idea-cue"
            elif overlap >= 1 / 3 or (len(item_terms) <= 5 and overlap > 0):
                # a third of the item's terms match, or a short, pointed
                # item mentions the active task's subject at all
                routing, routing_basis = (
                    "related-checkpoint-first",
                    "keyword-overlap>=1/3"
                    if overlap >= 1 / 3
                    else "keyword-match-short-item",
                )
            elif overlap == 0 and category == "ambiguous":
                # no cue and no subject match: routing is itself unclear
                routing, routing_basis = "ambiguous", "no-cue-no-overlap"
            elif overlap == 0:
                routing, routing_basis = ("queue-after-current",
                                          "keyword-overlap=0")
            else:
                routing, routing_basis = "ambiguous", "keyword-overlap-partial"

    return {
        "text": text,
        "category": category,
        "routing": routing,
        "routing_basis": routing_basis,
        "related_overlap": related_basis,
        "matched_cue": cue,
        "classification_basis": "heuristic-keyword",
        "execution_authorization": "none",
        "authority": authority,
        "urgency_language_detected": bool(
            _match_cue(_URGENCY_LANGUAGE_CUES, text)
        ) and not operator_urgent,
    }


def _split_and_classify(message: str, *, operator_urgent: bool,
                        active_task_terms: set[str] | None) -> list[dict]:
    fragments = _split_items(message)
    if len(fragments) > MAX_ITEMS_PER_MESSAGE:
        raise ValidationError(
            f"a capture holds at most {MAX_ITEMS_PER_MESSAGE} candidate "
            "items; send the remaining thoughts in another capture"
        )
    return [
        classify_item(fragment, active_task_terms=active_task_terms,
                      operator_urgent=operator_urgent)
        for fragment in fragments
    ]


# ---------------------------------------------------------------------------
# durable intake log
# ---------------------------------------------------------------------------

_INTAKE_LINE_KEYS = (
    "record", "capture_id", "at", "actor", "channel", "operator_urgent",
    "message", "items", "active_task",
)
_RESOLVE_LINE_KEYS = (
    "record", "resolve_id", "at", "actor", "capture_id", "item_index",
    "action", "note",
)


class IntakeLog:
    """One project's durable brain-dump intake log.

    Appended events are immutable and chain-hashed; state is replayed.
    The file lives inside the project's `.aperio/` state, is created
    atomically, opened without following symlinks, written in complete
    single-syscall appends, and flushed before success is reported.
    """

    def __init__(self, aperio_dir: Path):
        self.aperio_dir = Path(aperio_dir)
        self.path = self.aperio_dir / "inbox.jsonl"
        self.lock_path = self.aperio_dir / "inbox.lock"

    # -- reading -----------------------------------------------------------
    def read(self) -> dict:
        """Replay the log. Damaged lines are reported, never repaired."""

        if not self.path.exists():
            return {
                "captures": [], "resolves": [], "damaged": [],
                "counts": {"captures": 0, "resolves": 0, "pending_items": 0},
            }
        if os.path.islink(self.path):
            raise PathError(
                f"refusing to follow symlink at {self.path} for the "
                "intake log"
            )
        if not self.path.is_file():
            raise PathError(f"intake log {self.path} is not a regular file")
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise StateError(
                f"intake log exceeds 8 MiB; archive or start a new project"
            )
        captures: list[dict] = []
        resolves: list[dict] = []
        damaged: list[dict] = []
        previous: str | None = None
        broken = False
        with self.path.open("r", encoding="utf-8") as handle:
            for number, raw in enumerate(handle, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    damaged.append({"line": number, "reason": "not JSON"})
                    broken = True
                    continue
                if not isinstance(line, dict):
                    damaged.append({"line": number, "reason": "not an object"})
                    broken = True
                    continue
                if line.get(_CHAIN_FIELD) != line_sha256(line, previous):
                    damaged.append({"line": number, "reason": "chain break"})
                    broken = True
                    continue
                if broken:
                    # a line that continues past damage is unverifiable
                    damaged.append({
                        "line": number,
                        "reason": "follows damaged line",
                    })
                    continue
                record = line.get("record")
                if record == "capture":
                    captures.append(line)
                elif record == "resolve":
                    resolves.append(line)
                else:
                    damaged.append({"line": number,
                                    "reason": f"unknown record {record!r}"})
                previous = line.get(_CHAIN_FIELD)
        pending = self._pending_items(captures, resolves)
        return {
            "captures": captures,
            "resolves": resolves,
            "damaged": damaged,
            "counts": {
                "captures": len(captures),
                "resolves": len(resolves),
                "pending_items": sum(len(c["items"]) for c in pending),
            },
        }

    @staticmethod
    def _pending_items(captures: list[dict], resolves: list[dict]) -> list[dict]:
        resolved = {
            (r["capture_id"], r["item_index"]) for r in resolves
            if isinstance(r.get("capture_id"), str)
            and isinstance(r.get("item_index"), int)
        }
        pending = []
        for capture in captures:
            items = []
            for index, item in enumerate(capture.get("items", [])):
                if (capture["capture_id"], index) not in resolved:
                    items.append(item)
            if items:
                pending.append({**capture, "items": items})
        return pending

    # -- writing -----------------------------------------------------------
    def append_decided(self, build_event) -> dict:
        """Decide and append atomically: the whole decision (ids, counts,
        duplicate checks, chain head) is computed inside the lock from the
        validated file, then one complete event is appended. This is the
        only write path."""

        token = acquire_lock(self.lock_path, artifact="intake log")
        try:
            state = self.read()
            if state["damaged"]:
                raise StateError(
                    "the intake log has damaged or unverifiable lines; "
                    "inspect .aperio/inbox.jsonl and repair it by hand "
                    "before capturing more"
                )
            previous = None
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as handle:
                    for raw in handle:
                        raw = raw.rstrip("\n")
                        if raw:
                            previous = json.loads(raw).get(_CHAIN_FIELD)
            event = build_event(state)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                payload = dict(event)
                payload[_CHAIN_FIELD] = line_sha256(payload, previous)
                data = (json.dumps(payload, ensure_ascii=False)
                        + "\n").encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
                if written == 0:
                    raise StateError(
                        "intake append wrote zero bytes; treating as "
                        "damage rather than success"
                    )
                os.fsync(fd)
            finally:
                os.close(fd)
            return event
        finally:
            release_lock(self.lock_path, token)

    def _append(self, line: dict) -> None:
        """Validate, chain, and append entirely inside the lock.

        The chain head is recomputed from the file *after* the lock is
        held; computing it earlier would let two concurrent appends both
        chain from the same head and corrupt each other.
        """

        token = acquire_lock(self.lock_path, artifact="intake log")
        try:
            existing = None
            previous = None
            if self.path.exists():
                existing = self.read()
                if existing["damaged"]:
                    raise StateError(
                        "the intake log has damaged or unverifiable lines; "
                        "inspect .aperio/inbox.jsonl and repair it by hand "
                        "before capturing more"
                    )
                # recompute the chain head from the file itself
                with self.path.open("r", encoding="utf-8") as handle:
                    for raw in handle:
                        raw = raw.rstrip("\n")
                        if raw:
                            previous = json.loads(raw).get(_CHAIN_FIELD)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                if existing is not None and os.fstat(fd).st_size == 0:
                    raise StateError(
                        "the intake log was replaced while capturing; "
                        "retry"
                    )
                payload = dict(line)
                payload[_CHAIN_FIELD] = line_sha256(payload, previous)
                data = (json.dumps(payload, ensure_ascii=False)
                        + "\n").encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
                if written == 0:
                    raise StateError(
                        "intake append wrote zero bytes; treating as "
                        "damage rather than success"
                    )
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            release_lock(self.lock_path, token)


def _active_task(aperio_dir: Path) -> dict | None:
    """The currently active task, from durable state only.

    An active run's claimed task wins; otherwise an in-progress task on
    the approved plan. Purely derived from existing records.
    """

    from aperio.planstore import read_plan
    from aperio.runtime import read_executions

    try:
        exec_state = read_executions(aperio_dir)
    except StateError:
        exec_state = None
    if exec_state:
        for run in exec_state.get("runs", []):
            if run.get("status") in ("running", "awaiting-external"):
                return {
                    "task_id": run.get("task_id"),
                    "source": "active-run",
                }
    try:
        plan = read_plan(aperio_dir)
    except StateError:
        return None
    if plan:
        in_progress = [
            task for task in plan.get("tasks", [])
            if task.get("status") == "in-progress"
        ]
        if in_progress:
            task = sorted(in_progress, key=lambda t: t["id"])[0]
            # Task identity is durable across plan revisions even though a
            # revision resets approval: execution stays gated by the plan
            # lifecycle, but captures and checkpoints keep referring to
            # the task the operator is actually mid-way through.
            return {
                "task_id": task["id"],
                "source": "in-progress-task",
                "title": task.get("title"),
                "plan_approval": plan.get("approval", {}).get("status"),
                "plan_version": plan.get("plan_version"),
            }
    return None


def _active_task_terms(aperio_dir: Path, active: dict | None) -> set[str] | None:
    if not active or not active.get("task_id"):
        return None
    from aperio.planstore import read_plan

    try:
        plan = read_plan(aperio_dir)
    except StateError:
        return None
    if not plan:
        return None
    task = next((t for t in plan.get("tasks", [])
                 if t["id"] == active["task_id"]), None)
    if not task:
        return None
    text = " ".join(
        filter(None, [task.get("title"), task.get("objective"),
                      *(task.get("deliverables") or [])])
    )
    return _terms(text)


def capture(
    aperio_dir: Path,
    message: str,
    *,
    operator_urgent: bool = False,
    actor: str | None = None,
    channel: str = "cli",
) -> dict:
    """Capture one operator brain-dump message; nothing executes.

    Returns the stored capture event. The confirmation text for the
    operator is derived from it by :func:`confirmation_line`.
    """

    message = _check_text(message, "capture.message",
                          max_bytes=MAX_MESSAGE_BYTES)
    if actor is not None:
        actor = _check_text(actor, "capture.actor",
                            max_bytes=MAX_ACTOR_BYTES, single_line=True)
    _check_text(channel, "capture.channel", max_bytes=40, single_line=True)
    if not isinstance(operator_urgent, bool):
        raise ValidationError("capture.operator_urgent must be a boolean")

    active = _active_task(aperio_dir)
    terms = _active_task_terms(aperio_dir, active)
    items = _split_and_classify(message, operator_urgent=operator_urgent,
                                active_task_terms=terms)
    log = IntakeLog(aperio_dir)

    def build_event(state: dict) -> dict:
        serial = len(state["captures"]) + len(state["resolves"]) + 1
        return {
            "record": "capture",
            "capture_id": f"INT-{serial:04d}",
            "at": _utc_now(),
            "actor": actor,
            "channel": channel,
            "operator_urgent": operator_urgent,
            "message": message,
            "items": items,
            "active_task": active,
        }

    return log.append_decided(build_event)


def confirmation_line(event: dict) -> str:
    """The short capture confirmation (directive §2.K)."""

    counts: dict[str, int] = {}
    for item in event["items"]:
        key = item["routing"] if item["routing"] != "queued" \
            else "queued"
        counts[key] = counts.get(key, 0) + 1
    parts = []
    ordered = ("urgent-operator-requested", "related-checkpoint-first",
               "queue-after-current", "queued", "idea-note", "ambiguous")
    labels = {
        "urgent-operator-requested": "urgent (operator-requested: checkpoint now)",
        "related-checkpoint-first": "related (checkpoint first)",
        "queue-after-current": "queued for after the current task",
        "queued": "queued",
        "idea-note": "idea (not a task)",
        "ambiguous": "ambiguous (needs your classification)",
    }
    for key in ordered:
        if counts.get(key):
            parts.append(f"{counts[key]} {labels[key]}")
    urgent_note = ""
    if event["operator_urgent"]:
        urgent_note = " Operator-requested urgency recorded: checkpoint the current task."
    elif any(item["urgency_language_detected"] for item in event["items"]):
        urgent_note = (
            " Urgency language was detected in the text but carries no "
            "authority; pass --urgent yourself to request an interruption."
        )
    routing_active = (
        f" Current task {event['active_task']['task_id']} continues."
        if event["active_task"] else ""
    )
    return (
        f"Captured {len(event['items'])} item(s): "
        + (", ".join(parts) if parts else "no items")
        + urgent_note
        + routing_active
        + " Nothing executed."
    )


def resolve(
    aperio_dir: Path,
    capture_id: str,
    item_index: int,
    *,
    action: str,
    actor: str,
    note: str | None = None,
) -> dict:
    """Acknowledge or dismiss one captured item. Nothing executes."""

    if action not in RESOLVE_ACTIONS:
        raise ValidationError(
            "intake resolve action must be one of: "
            + ", ".join(RESOLVE_ACTIONS)
        )
    actor = _check_text(actor, "resolve.actor", max_bytes=MAX_ACTOR_BYTES,
                        single_line=True)
    if note is not None:
        note = _check_text(note, "resolve.note", max_bytes=MAX_NOTE_BYTES)
    log = IntakeLog(aperio_dir)

    def build_event(state: dict) -> dict:
        capture_event = next(
            (c for c in state["captures"] if c["capture_id"] == capture_id),
            None,
        )
        if capture_event is None:
            raise ValidationError(
                f"no capture {capture_id!r} in the intake log"
            )
        if not isinstance(item_index, int) or not (
            0 <= item_index < len(capture_event["items"])
        ):
            raise ValidationError(
                f"item index {item_index!r} is outside capture {capture_id}"
            )
        if (capture_id, item_index) in {
            (r["capture_id"], r["item_index"]) for r in state["resolves"]
        }:
            raise ValidationError(
                f"item {capture_id}[{item_index}] is already resolved; "
                "intake history is append-only"
            )
        serial = len(state["captures"]) + len(state["resolves"]) + 1
        return {
            "record": "resolve",
            "resolve_id": f"RES-{serial:04d}",
            "at": _utc_now(),
            "actor": actor,
            "capture_id": capture_id,
            "item_index": item_index,
            "action": action,
            "note": note,
        }

    return log.append_decided(build_event)


__all__ = [
    "INTAKE_FORMAT",
    "INTAKE_SCHEMA_VERSION",
    "ITEM_CATEGORIES",
    "IntakeLog",
    "capture",
    "classification_is_heuristic_note",
    "classify_item",
    "confirmation_line",
    "resolve",
]


def classification_is_heuristic_note() -> str:
    """The standing honesty label for rendered intake output."""

    return (
        "classification is deterministic keyword triage, not language "
        "understanding; review items before acting on them"
    )
