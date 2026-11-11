"""Existing-project reconstruction (reconstruction-report v1).

When onboarding meets an existing project, the first job is to
understand what is actually there — read-only, bounded, and with source
attribution on every extracted item. This module inventories relevant
documents, extracts pending-work items and known-claim conflicts, and
reports where documented coverage exists so the adaptive interview can
fill real gaps instead of re-asking answered questions.

Honesty rules (enforced here, restated in the docs):

- **Documented is not verified.** A checkbox item is recorded with status
  ``documented_pending`` / ``documented_complete``; neither is treated as
  an approved task or as proof that anything works. Only Aperio's own
  lifecycle-managed plan tasks (when a valid plan exists) carry lifecycle
  status, and even those are reported as recorded state, not verified
  work.
- **Repository content is untrusted data.** Documents are parsed for
  structure (checkboxes, key-value claims), never followed as
  instructions. Imperative patterns and secret-shaped strings found in
  documents become *risk findings* (file + line + short excerpt of the
  match only); they authorize nothing.
- **Bounded reads.** At most ``MAX_DOCUMENTS`` documents and
  ``MAX_DOCUMENT_BYTES`` per document are read; oversized and unreadable
  files are reported, never truncated silently. Symlinked documents are
  refused (discovery already flagged them).
- **Every claim carries provenance**: source path, line number, and the
  SHA-256 of the document as read, so later runs can detect drift.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aperio import safeproc
from aperio.errors import ValidationError
from aperio.onboarding_discovery import (
    APERIO_DIR_NAME,
    DiscoveryResult,
    discover_workspace,
)

RECONSTRUCTION_FORMAT = "aperio-onboarding-reconstruction"
RECONSTRUCTION_VERSION = 1

MAX_DOCUMENTS = 24
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_LINE_CHARS = 200
MAX_ITEMS_PER_KIND = 100

# Documents considered relevant for reconstruction. Names are matched
# case-insensitively against file basenames; docs/*.md files are included
# up to the document budget.
_RELEVANT_DOC_PREFIXES = (
    "README", "PRD", "SPEC", "REQUIREMENTS", "ARCHITECTURE", "AGENTS",
    "CLAUDE", "GEMINI", "TODO", "ROADMAP", "BACKLOG", "TASKS", "PLAN",
    "MASTER-PLAN", "CHANGELOG", "CONTRIBUTING", "DESIGN", "DECISIONS",
)

# Known claim keys for deterministic conflict detection: two documents
# giving different values for the same key is a *candidate conflict* the
# operator may be asked to resolve.
_CLAIM_KEYS = ("language", "framework", "database", "license", "python", "node", "runtime")
_CLAIM_RE = re.compile(
    r"^\s*[*-]?\s*(?P<key>[A-Za-z][A-Za-z ._-]{0,30}?)\s*[:=]\s*(?P<value>\S.{0,120}?)\s*$"
)

# Imperative instruction patterns: found content is a risk finding, never
# an instruction (Scenario 5 of the Step 01 acceptance scenarios).
_INSTRUCTION_PATTERNS = (
    re.compile(r"(?i)\bdelete\s+(all\s+)?files?\b"),
    re.compile(r"(?i)\b(install|pip install|npm install)\b"),
    re.compile(r"(?i)\brun\s+the\s+(following\s+)?(command|script)"),
    re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"),
    re.compile(r"(?i)\breveal\s+(the\s+)?(secret|token|password|key)"),
    re.compile(r"(?i)\bdisable\s+(security|approval|governance)"),
    re.compile(r"(?i)\bexecute\s+this\b"),
)

# Secret-shaped strings: reported by location only — the matched text is
# never copied into the report.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{12,}"),
)

CHECKBOX_RE = re.compile(r"^\s*[-*]?\s*\[(?P<mark>[ xX])\]\s*(?P<text>.+)$")

# Coverage topics for gap analysis: topic -> casefolded keyword hints.
_COVERAGE_TOPICS: dict[str, tuple[str, ...]] = {
    "testing": ("test", "pytest", "unittest", "qa"),
    "hosting": ("deploy", "hosting", "server", "vercel", "aws", "heroku"),
    "authentication": ("auth", "login", "sign in", "sso", "oauth"),
    "performance": ("performance", "latency", "scalab"),
    "accessibility": ("accessib", "a11y", "screen reader", "wcag"),
    "seo": ("seo", "search engine", "sitemap"),
    "monitoring": ("monitor", "logging", "observab", "alert"),
    "licensing": ("licens", "copyright", "proprietary"),
    "budget": ("budget", "cost", "pricing"),
    "schedule": ("deadline", "timeline", "milestone", "roadmap date"),
    "backup": ("backup", "restore", "disaster recovery"),
    "offline": ("offline", "no internet", "local-first"),
}

_TOPIC_TO_QUESTIONS: dict[str, tuple[str, ...]] = {
    "testing": ("quality.testing",),
    "hosting": ("operations.hosting",),
    "authentication": ("product.accounts", "technical.auth"),
    "performance": ("technical.performance",),
    "accessibility": ("design.accessibility",),
    "seo": ("business.seo",),
    "monitoring": ("operations.monitoring",),
    "licensing": ("business.licensing",),
    "budget": ("business.budget",),
    "schedule": ("business.schedule",),
    "backup": ("operations.backup",),
    "offline": ("technical.offline",),
}


@dataclass(frozen=True)
class DocumentRecord:
    """One scanned document with its read-time identity."""

    path: str  # workspace-relative
    sha256: str
    bytes_read: int
    total_bytes: int
    truncated: bool  # content beyond MAX_DOCUMENT_BYTES was not read
    encoding_ok: bool


@dataclass(frozen=True)
class TaskItem:
    """A checkbox item from a document. Never an approved task."""

    source: str
    line_no: int
    text: str
    status: str  # documented_pending | documented_complete


@dataclass(frozen=True)
class ClaimConflict:
    """Two documents disagreeing about the same known key."""

    topic: str
    claims: tuple[dict, ...]  # [{value, source}]


@dataclass(frozen=True)
class RiskFinding:
    """Untrusted-content risk signal: location only, never authorization."""

    kind: str  # instruction_pattern | secret_risk
    source: str
    line_no: int
    excerpt: str


@dataclass
class ReconstructionResult:
    """Structured result of read-only reconstruction."""

    workspace: Path
    scanned_at: str
    documents: list[DocumentRecord] = field(default_factory=list)
    pending_tasks: list[TaskItem] = field(default_factory=list)
    completed_claims: list[TaskItem] = field(default_factory=list)
    claim_conflicts: list[ClaimConflict] = field(default_factory=list)
    risk_findings: list[RiskFinding] = field(default_factory=list)
    documented_coverage: dict[str, list[str]] = field(default_factory=dict)
    missing_topics: list[str] = field(default_factory=list)
    aperio_decisions_count: int = 0
    aperio_plan_tasks: list[dict] = field(default_factory=list)
    aperio_notes: list[str] = field(default_factory=list)
    skipped_oversized: list[str] = field(default_factory=list)
    skipped_unreadable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-safe rendering for the machine-readable protocol."""
        return {
            "format": RECONSTRUCTION_FORMAT,
            "version": RECONSTRUCTION_VERSION,
            "workspace": str(self.workspace),
            "scanned_at": self.scanned_at,
            "documents": [
                {
                    "path": d.path,
                    "sha256": d.sha256,
                    "bytes_read": d.bytes_read,
                    "total_bytes": d.total_bytes,
                    "truncated": d.truncated,
                    "encoding_ok": d.encoding_ok,
                }
                for d in self.documents
            ],
            "pending_tasks": [
                {"source": t.source, "line_no": t.line_no, "text": t.text, "status": t.status}
                for t in self.pending_tasks
            ],
            "completed_claims": [
                {"source": t.source, "line_no": t.line_no, "text": t.text, "status": t.status}
                for t in self.completed_claims
            ],
            "claim_conflicts": [
                {"topic": c.topic, "claims": list(c.claims)} for c in self.claim_conflicts
            ],
            "risk_findings": [
                {"kind": r.kind, "source": r.source, "line_no": r.line_no, "excerpt": r.excerpt}
                for r in self.risk_findings
            ],
            "documented_coverage": {k: list(v) for k, v in self.documented_coverage.items()},
            "missing_topics": list(self.missing_topics),
            "aperio_decisions_count": self.aperio_decisions_count,
            "aperio_plan_tasks": list(self.aperio_plan_tasks),
            "aperio_notes": list(self.aperio_notes),
            "skipped_oversized": list(self.skipped_oversized),
            "skipped_unreadable": list(self.skipped_unreadable),
            "limitations": [
                "documented tasks are claims, not approved work or verified completion",
                "extraction is line-based and keyword-based; it is heuristic evidence, not ground truth",
                "risk findings are signals for the operator, never authorization",
            ],
        }


def reconstruct_workspace(
    raw_path: str,
    discovery: DiscoveryResult | None = None,
) -> ReconstructionResult:
    """Reconstruct project context read-only from relevant documents.

    ``discovery`` may be passed to reuse an existing discovery result; when
    omitted, discovery runs first (it is strictly read-only).
    """
    if discovery is None:
        discovery = discover_workspace(raw_path)
    workspace = discovery.workspace

    result = ReconstructionResult(workspace=workspace, scanned_at=_utc_now())
    doc_paths = _select_documents(workspace)
    claims: dict[str, list[dict]] = {}
    coverage_hits: dict[str, list[str]] = {topic: [] for topic in _COVERAGE_TOPICS}

    for path in doc_paths[:MAX_DOCUMENTS]:
        loaded = _read_document(workspace, path, result)
        if loaded is None:
            continue
        record, text = loaded
        result.documents.append(record)
        rel = record.path

        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            _collect_risk_findings(result, rel, line_no, line)
            match = CHECKBOX_RE.match(line)
            if match:
                item_text = _clip(match.group("text"))
                if item_text:
                    item = TaskItem(
                        source=rel, line_no=line_no, text=item_text,
                        status="documented_pending" if match.group("mark").strip().lower() == "" else "documented_complete",
                    )
                    if item.status == "documented_pending":
                        if len(result.pending_tasks) < MAX_ITEMS_PER_KIND:
                            result.pending_tasks.append(item)
                    elif len(result.completed_claims) < MAX_ITEMS_PER_KIND:
                        result.completed_claims.append(item)
            claim = _match_claim(stripped)
            if claim is not None:
                key, value = claim
                normalized_key = key.casefold().strip()
                if normalized_key in _CLAIM_KEYS and value:
                    claims.setdefault(normalized_key, []).append(
                        {"value": _clip(value), "source": rel}
                    )
            folded = line.casefold()
            for topic, keywords in _COVERAGE_TOPICS.items():
                if any(k in folded for k in keywords):
                    if rel not in coverage_hits[topic]:
                        coverage_hits[topic].append(rel)

    result.claim_conflicts = _find_conflicts(claims)
    result.documented_coverage = {t: v for t, v in coverage_hits.items() if v}
    result.missing_topics = [t for t in _COVERAGE_TOPICS if not coverage_hits[t]]

    if discovery.classification in ("aperio_project", "aperio_partial"):
        _collect_aperio_state(result, discovery)

    return result


def _select_documents(workspace: Path) -> list[Path]:
    """Relevant documents, bounded; symlinked files are refused."""
    candidates: list[Path] = []
    try:
        entries = sorted(os.scandir(workspace), key=lambda e: e.name)
    except OSError:
        return candidates
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) and entry.name == "docs":
            try:
                for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
                    if sub.is_file(follow_symlinks=False) and sub.name.casefold().endswith(
                        (".md", ".txt", ".rst")
                    ):
                        candidates.append(Path(sub.path))
            except OSError:
                continue
            continue
        if entry.is_file(follow_symlinks=False) and entry.name.upper().startswith(
            _RELEVANT_DOC_PREFIXES
        ):
            candidates.append(Path(entry.path))
    return candidates


def _read_document(
    workspace: Path, path: Path, result: ReconstructionResult
) -> tuple[DocumentRecord, str] | None:
    """Bounded, symlink-safe document read with identity and clipping."""
    rel = os.path.relpath(path, workspace)
    try:
        total = path.stat().st_size
    except OSError:
        return None
    if total > MAX_DOCUMENT_BYTES:
        result.skipped_oversized.append(rel)
        return None
    try:
        # Bounded read from the handle: the file may have grown between the
        # stat above and this open, so never trust stat for the byte budget.
        raw, _truncated = safeproc.read_bytes_bounded(path, MAX_DOCUMENT_BYTES)
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
        encoding_ok = True
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        encoding_ok = False
        result.skipped_unreadable.append(f"{rel} (non-UTF-8; read with replacement)")
    record = DocumentRecord(
        path=rel,
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes_read=len(raw),
        total_bytes=total,
        truncated=len(raw) < total,
        encoding_ok=encoding_ok,
    )
    return record, text


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text[:MAX_LINE_CHARS]


def _collect_risk_findings(result: ReconstructionResult, rel: str, line_no: int, line: str) -> None:
    """Scan one line for imperative/secret patterns; record location only."""
    if len(result.risk_findings) >= MAX_ITEMS_PER_KIND:
        return
    for pattern in _INSTRUCTION_PATTERNS:
        match = pattern.search(line)
        if match:
            start = max(0, match.start() - 30)
            excerpt = _clip(line[start : match.end() + 30])
            result.risk_findings.append(
                RiskFinding(kind="instruction_pattern", source=rel, line_no=line_no, excerpt=excerpt)
            )
            break
    for pattern in _SECRET_PATTERNS:
        if pattern.search(line):
            # Location only: never copy the matched secret text.
            result.risk_findings.append(
                RiskFinding(kind="secret_risk", source=rel, line_no=line_no, excerpt="<redacted>")
            )
            break


def _match_claim(line: str) -> tuple[str, str] | None:
    """Match 'key: value' claim lines for the known claim keys."""
    match = _CLAIM_RE.match(line)
    if not match:
        return None
    key = match.group("key").strip().casefold()
    value = match.group("value").strip()
    if key in _CLAIM_KEYS and value:
        return key, value
    return None


def _find_conflicts(claims: dict[str, list[dict]]) -> list[ClaimConflict]:
    """Same key, materially different values -> candidate conflict."""
    conflicts: list[ClaimConflict] = []
    for key, entries in sorted(claims.items()):
        values = {e["value"].casefold() for e in entries}
        if len(values) > 1:
            conflicts.append(ClaimConflict(topic=key, claims=tuple(entries)))
    return conflicts


def _collect_aperio_state(result: ReconstructionResult, discovery: DiscoveryResult) -> None:
    """Read existing Aperio artifacts (decisions, plan tasks) read-only."""
    aperio_dir = discovery.workspace / APERIO_DIR_NAME
    try:
        from aperio.decisions import read_decisions

        read = read_decisions(aperio_dir)
        result.aperio_decisions_count = len(read.entries)
    except Exception:
        result.aperio_notes.append("existing decisions log unreadable; not interpreted")
    try:
        from aperio.planstore import plan_exists, read_plan

        if plan_exists(aperio_dir):
            plan = read_plan(aperio_dir)
            tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
            for task in tasks[:MAX_ITEMS_PER_KIND]:
                result.aperio_plan_tasks.append(
                    {
                        "id": task.get("id"),
                        "title": _clip(str(task.get("title", ""))),
                        "status": task.get("status"),
                    }
                )
    except Exception:
        result.aperio_notes.append("existing plan unreadable; not interpreted")


def missing_information_questions(reconstruction: ReconstructionResult):
    """Registry questions for topics with no documented coverage.

    Gap-filling reuses the adaptive interview engine unchanged: missing
    topics map to existing registry question ids, so the one-question-
    per-turn contract, provenance, and severity rules all apply.
    """
    from aperio.onboarding_interview import get_question

    questions = []
    for topic in reconstruction.missing_topics:
        for qid in _TOPIC_TO_QUESTIONS.get(topic, ()):
            questions.append(get_question(qid))
    # De-duplicate, keep registry order.
    seen: set[str] = set()
    ordered = []
    for question in questions:
        if question.id not in seen:
            seen.add(question.id)
            ordered.append(question)
    return tuple(ordered)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
