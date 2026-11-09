"""Universal mistake prevention and quality evaluation (M4, quality-report v1).

Two deterministic, read-only engines over the M2 goal contract:

* **Preflight** (``run_preflight``) inspects a *goal* before work begins:
  unclear or missing expectations, conflicting or duplicated instructions,
  untrusted instructions, unvalidated assumptions, and — when a memory store
  is supplied — inapplicable, expired, or conflicting memory.
* **Deliverable check** (``run_quality_check``) compares a *candidate
  deliverable* against what the goal explicitly requires: operator-named
  sections and texts, modal constraint phrases ("must include X", "must not
  contain Y"), stated length bounds, output-format expectations, deliverable
  coverage, and source attribution when the goal demands evidence.

What this layer deliberately does **not** do:

* It does not judge whether prose is *good*, whether a claim is *true*, or
  whether a design is *beautiful*. Where a question needs ground truth the
  tool has no access to, the finding is reported with status ``unknown`` or
  as a ``recommendation`` — never as a confirmed failure.
* It never executes untrusted content, never writes to a memory store, never
  promotes memory, and never alters the operator's goal or deliverable. The
  only file it can create is its own report, through the shared
  atomic-publication path.
* It is not a governance gate. Nothing in the Feature 03/06 lifecycle reads
  a quality report; completion still requires the existing evidence and
  independent-review contract.

Findings use the stable taxonomy in ``ISSUE_CATEGORIES`` (taxonomy v1) and
the declarative rules in ``RULES`` (quality-rules v1). Every issue carries
its rule id, category, severity, status, the requirement it came from, the
evidence, an explanation, and a suggested corrective action. Issue identity
is content-derived, so the same finding in two runs has the same id, and a
report's ``content_sha256`` is reproducible from its inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Sequence

from aperio.errors import ValidationError
from aperio.goals import (
    goal_authority,
    goal_content_sha256,
    goal_identity,
)
from aperio.profiles import profile_versions

QUALITY_REPORT_FORMAT = "aperio-quality-report"
QUALITY_REPORT_VERSION = 1
QUALITY_TAXONOMY_VERSION = 1
QUALITY_RULES_VERSION = 1

MAX_DELIVERABLE_BYTES = 1024 * 1024
MAX_REQUEST_ITEMS = 100
MAX_REQUEST_TEXT_BYTES = 8 * 1024

# Stable, versioned mistake taxonomy (taxonomy v1). Names are never renamed;
# new categories may be added in a later taxonomy version.
ISSUE_CATEGORIES = (
    "requirement-violation",
    "missing-deliverable",
    "unsupported-claim",
    "conflicting-information",
    "context-misuse",
    "outdated-information",
    "provenance-failure",
    "output-format-failure",
    "insufficient-evidence",
    "incomplete-task",
    "privacy-or-safety-violation",
)

ISSUE_STATUSES = ("confirmed", "warning", "unknown", "recommendation")
SEVERITY_LEVELS = ("high", "medium", "low", "info")
_SEVERITY_ORDER = {level: index for index, level in enumerate(SEVERITY_LEVELS)}

PROFILE_NAMES = ("general", "design", "research", "writing", "business",
                 "software")

REPORT_LIMITATIONS = (
    "structural checks only: this tool cannot judge truth, quality, or "
    "completeness that is not objectively checkable from the goal and the "
    "deliverable text",
    "a finding's status reflects what a deterministic rule can prove; "
    "'warning' and 'recommendation' findings need human judgment",
    "no memory was written and no content was altered: the goal, the "
    "deliverable, and any memory store are read-only inputs",
)

_LENGTH_NOTE = (
    "only word-count bounds are enforced numerically; bounds expressed in "
    "pages, slides, or characters are reported as expectations, not checked"
)


# ---------------------------------------------------------------------------
# declarative rule registry (quality-rules v1)
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str,
    *,
    applies: str,
    category: str,
    severity: str,
    status: str,
    description: str,
    profiles: Sequence[str] | None = None,
) -> dict:
    if category not in ISSUE_CATEGORIES:
        raise ValueError(f"rule {rule_id}: unknown category {category!r}")
    if status not in ISSUE_STATUSES:
        raise ValueError(f"rule {rule_id}: unknown status {status!r}")
    if severity not in SEVERITY_LEVELS:
        raise ValueError(f"rule {rule_id}: unknown severity {severity!r}")
    return {
        "rule_id": rule_id,
        "applies": applies,
        "category": category,
        "severity": severity,
        "status": status,
        "profiles": list(profiles) if profiles is not None else None,
        "description": description,
    }


# ``profiles: None`` means the rule applies to every profile. The registry is
# plain data on purpose: one engine, no plugin loading, no executable rules.
RULES: dict[str, dict] = {
    rule["rule_id"]: rule
    for rule in (
        # -- preflight (goal-side) --------------------------------------
        _rule(
            "P-untrusted-instructions",
            applies="preflight",
            category="provenance-failure",
            severity="high",
            status="confirmed",
            description=(
                "the goal is declared as agent- or import-authored, so none "
                "of its instructions are operator-approved"
            ),
        ),
        _rule(
            "P-objective-underspecified",
            applies="preflight",
            category="incomplete-task",
            severity="medium",
            status="warning",
            description="the objective states very little to act on",
        ),
        _rule(
            "P-missing-acceptance-criteria",
            applies="preflight",
            category="insufficient-evidence",
            severity="medium",
            status="warning",
            description=(
                "no acceptance criteria: done cannot be judged objectively"
            ),
        ),
        _rule(
            "P-missing-deliverables",
            applies="preflight",
            category="missing-deliverable",
            severity="medium",
            status="warning",
            description="no concrete deliverables are named",
        ),
        _rule(
            "P-missing-quality-criteria",
            applies="preflight",
            category="insufficient-evidence",
            severity="low",
            status="recommendation",
            description="no quality criteria: quality will be a matter of taste",
        ),
        _rule(
            "P-duplicate-instructions",
            applies="preflight",
            category="conflicting-information",
            severity="medium",
            status="warning",
            description=(
                "the same instruction text appears more than once, which "
                "often hides an unresolved edit"
            ),
        ),
        _rule(
            "P-conflicting-length-expectations",
            applies="preflight",
            category="conflicting-information",
            severity="medium",
            status="warning",
            description=(
                "the goal states more than one distinct length bound"
            ),
        ),
        _rule(
            "P-unvalidated-assumptions",
            applies="preflight",
            category="unsupported-claim",
            severity="low",
            status="recommendation",
            description="assumptions are untested input, not established facts",
        ),
        _rule(
            "P-unaddressed-risks",
            applies="preflight",
            category="incomplete-task",
            severity="low",
            status="recommendation",
            description="risks are listed without stated handling or acceptance",
        ),
        _rule(
            "P-oversized-instruction-list",
            applies="preflight",
            category="context-misuse",
            severity="low",
            status="warning",
            description=(
                "an instruction list is near its cap; confirm every entry is "
                "load-bearing"
            ),
        ),
        _rule(
            "P-expired-memory",
            applies="preflight",
            category="outdated-information",
            severity="medium",
            status="warning",
            description=(
                "memory that would apply is outside its validity window"
            ),
        ),
        _rule(
            "P-unreviewed-memory",
            applies="preflight",
            category="unsupported-claim",
            severity="low",
            status="warning",
            description="selected memory is still unreviewed (proposed)",
        ),
        _rule(
            "P-memory-conflict",
            applies="preflight",
            category="conflicting-information",
            severity="medium",
            status="warning",
            description="selected memories disagree; nothing resolves them",
        ),
        # -- deliverable-side --------------------------------------------
        _rule(
            "Q-empty-deliverable",
            applies="deliverable",
            category="incomplete-task",
            severity="high",
            status="confirmed",
            description="the deliverable text is empty",
        ),
        _rule(
            "Q-required-section",
            applies="deliverable",
            category="output-format-failure",
            severity="high",
            status="confirmed",
            description="an operator-required section is absent",
        ),
        _rule(
            "Q-required-text",
            applies="deliverable",
            category="requirement-violation",
            severity="high",
            status="confirmed",
            description="operator-required text is absent",
        ),
        _rule(
            "Q-forbidden-text",
            applies="deliverable",
            category="privacy-or-safety-violation",
            severity="high",
            status="confirmed",
            description="operator-forbidden text is present",
        ),
        _rule(
            "Q-constraint-required-inclusion",
            applies="deliverable",
            category="requirement-violation",
            severity="high",
            status="confirmed",
            description=(
                "a 'must include/mention/contain/reference' constraint is "
                "not satisfied"
            ),
        ),
        _rule(
            "Q-constraint-prohibition",
            applies="deliverable",
            category="requirement-violation",
            severity="high",
            status="confirmed",
            description=(
                "a 'must not / must never' constraint is violated"
            ),
        ),
        _rule(
            "Q-min-length",
            applies="deliverable",
            category="output-format-failure",
            severity="medium",
            status="confirmed",
            description="the deliverable is shorter than the stated minimum",
        ),
        _rule(
            "Q-max-length",
            applies="deliverable",
            category="output-format-failure",
            severity="medium",
            status="confirmed",
            description="the deliverable exceeds the stated maximum",
        ),
        _rule(
            "Q-target-length",
            applies="deliverable",
            category="output-format-failure",
            severity="low",
            status="warning",
            description=(
                "the deliverable misses the stated approximate length by "
                "more than 10 percent"
            ),
        ),
        _rule(
            "Q-deliverable-coverage",
            applies="deliverable",
            category="missing-deliverable",
            severity="medium",
            status="warning",
            description=(
                "a named deliverable is never mentioned in the deliverable "
                "text; coverage is not proof, absence is a signal"
            ),
        ),
        _rule(
            "Q-missing-attribution",
            applies="deliverable",
            category="insufficient-evidence",
            severity="medium",
            status="warning",
            description=(
                "the goal requires evidence but no recognizable source "
                "attribution appears"
            ),
        ),
        _rule(
            "Q-unsupported-absolute-claim",
            applies="deliverable",
            category="unsupported-claim",
            severity="low",
            status="warning",
            description=(
                "absolute factual phrasing appears in a deliverable whose "
                "goal demands evidence"
            ),
        ),
        _rule(
            "Q-source-support-unknown",
            applies="deliverable",
            category="insufficient-evidence",
            severity="info",
            status="unknown",
            description=(
                "attribution was found; whether the sources actually support "
                "the claims cannot be checked by this tool"
            ),
        ),
        _rule(
            "Q-format-mismatch",
            applies="deliverable",
            category="output-format-failure",
            severity="medium",
            status="confirmed",
            description=(
                "the deliverable does not match the stated output format"
            ),
        ),
        _rule(
            "Q-missing-limitations",
            applies="deliverable",
            category="incomplete-task",
            severity="low",
            status="warning",
            description=(
                "the goal names risks or uncertainties but the deliverable "
                "states no limitations"
            ),
        ),
        _rule(
            "Q-missing-verification-evidence",
            applies="deliverable",
            category="insufficient-evidence",
            severity="low",
            status="recommendation",
            description=(
                "acceptance criteria exist but the deliverable never states "
                "how they would be checked",
                ),
            profiles=("software",),
        ),
    )
}


def _issue_id(rule_id: str, requirement_ref: str | None, evidence: str) -> str:
    canonical = json.dumps(
        {"rule": rule_id, "ref": requirement_ref, "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "QI-" + hashlib.sha256(canonical).hexdigest()[:12]


def _issue(
    rule_id: str,
    *,
    requirement_ref: str | None,
    evidence: str,
    explanation: str,
    corrective_action: str,
    severity: str | None = None,
    status: str | None = None,
) -> dict:
    rule = RULES[rule_id]
    return {
        "issue_id": _issue_id(rule_id, requirement_ref, evidence),
        "rule_id": rule_id,
        "rule_version": QUALITY_RULES_VERSION,
        "taxonomy_version": QUALITY_TAXONOMY_VERSION,
        "category": rule["category"],
        "severity": severity or rule["severity"],
        "status": status or rule["status"],
        "requirement_ref": requirement_ref,
        "evidence": _bound_evidence(evidence),
        "explanation": explanation,
        "corrective_action": corrective_action,
    }


def _bound_evidence(evidence: str, limit: int = 300) -> str:
    text = " ".join(evidence.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# quality-request v1
# ---------------------------------------------------------------------------

REQUEST_KEYS = (
    "request_schema_version",
    "required_sections",
    "required_texts",
    "forbidden_texts",
    "min_words",
    "max_words",
    "use_output_contract",
)


def validate_quality_request(data: object) -> dict:
    """Validate a quality-request v1 document into its normalized form."""

    if not isinstance(data, dict):
        raise ValidationError("quality request: the document must be a JSON object")
    unknown = sorted(set(data) - set(REQUEST_KEYS))
    if unknown:
        raise ValidationError(
            "quality request: unknown fields: " + ", ".join(unknown)
        )
    version = data.get("request_schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValidationError(
            "quality request: unsupported request_schema_version; this aperio "
            "supports version 1"
        )

    def _text_list(key: str) -> list[str]:
        value = data.get(key, [])
        if not isinstance(value, list):
            raise ValidationError(f"quality request: '{key}' must be a list")
        if len(value) > MAX_REQUEST_ITEMS:
            raise ValidationError(
                f"quality request: '{key}' must hold at most "
                f"{MAX_REQUEST_ITEMS} entries"
            )
        items: list[str] = []
        for index, entry in enumerate(value):
            if not isinstance(entry, str) or not entry.strip():
                raise ValidationError(
                    f"quality request: '{key}[{index}]' must be non-empty text"
                )
            if "\x00" in entry:
                raise ValidationError(
                    f"quality request: '{key}[{index}]' must not contain NUL"
                )
            encoded = entry.encode("utf-8")
            if len(encoded) > MAX_REQUEST_TEXT_BYTES:
                raise ValidationError(
                    f"quality request: '{key}[{index}]' must be at most "
                    f"{MAX_REQUEST_TEXT_BYTES} UTF-8 bytes"
                )
            items.append(entry)
        return items

    def _word_bound(key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(
                f"quality request: '{key}' must be a non-negative integer"
            )
        return value

    min_words = _word_bound("min_words")
    max_words = _word_bound("max_words")
    if min_words is not None and max_words is not None and min_words > max_words:
        raise ValidationError(
            "quality request: 'min_words' must not exceed 'max_words'"
        )
    use_contract = data.get("use_output_contract", True)
    if not isinstance(use_contract, bool):
        raise ValidationError(
            "quality request: 'use_output_contract' must be a boolean"
        )
    return {
        "request_schema_version": 1,
        "required_sections": _text_list("required_sections"),
        "required_texts": _text_list("required_texts"),
        "forbidden_texts": _text_list("forbidden_texts"),
        "min_words": min_words,
        "max_words": max_words,
        "use_output_contract": use_contract,
    }


def load_quality_request_file(path: str) -> dict:
    """Read and validate a quality-request JSON file (bounded, UTF-8)."""

    from pathlib import Path

    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the quality request file"
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read quality request file: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_REQUEST_FILE_BYTES:
        raise ValidationError(
            f"quality request file must be at most {MAX_REQUEST_FILE_BYTES} "
            "UTF-8 bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("quality request file must be valid UTF-8") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"quality request file is not valid JSON (line {exc.lineno}: "
            f"{exc.msg})"
        ) from None
    return validate_quality_request(data)


MAX_REQUEST_FILE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# deterministic text analysis helpers
# ---------------------------------------------------------------------------

_INCLUSION_RE = re.compile(
    r"\b(?:must|shall|should)\s+(?:also\s+)?"
    r"(include|mention|contain|reference|state|name)\s+(.{3,}?)(?:\s*[.;]|$)",
    re.IGNORECASE,
)
_PROHIBITION_RE = re.compile(
    r"\b(?:must|shall)\s+(?:not|never)\s+(?:\w+\s+){0,2}?"
    r"(?:include|contain|use|mention|reference|share|publish|assume|"
    r"rely\s+on)\s+(?:any\s+|the\s+)?(.{3,}?)(?:\s*[.;]|$)",
    re.IGNORECASE,
)
_MAX_WORDS_RE = re.compile(
    r"(?:at\s+most|no\s+more\s+than|up\s+to|max(?:imum)?(?:\s+of)?|≤)\s*"
    r"(\d[\d,]*)\s*words?\b",
    re.IGNORECASE,
)
_MAX_WORDS_TRAILING_RE = re.compile(
    r"(\d[\d,]*)\s*words?\s+(?:at\s+(?:the\s+)?most|or\s+(?:fewer|less)|"
    r"max(?:imum)?)\b",
    re.IGNORECASE,
)
_MIN_WORDS_RE = re.compile(
    r"(?:at\s+least|no\s+fewer\s+than|min(?:imum)?(?:\s+of)?)\s*"
    r"(\d[\d,]*)\s*words?\b",
    re.IGNORECASE,
)
_TARGET_WORDS_RE = re.compile(r"(\d[\d,]*)[\s-]*words?\b", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION_RE = re.compile(
    r"\b(no|not|never|without|avoid\w*|exclud\w*|neither|nor|instead of)\b",
    re.IGNORECASE,
)

_ATTRIBUTION_MARKERS = (
    "http://", "https://", "doi:", "et al.", "according to", "source:",
    "sources:", "cited in", "©",
)
_CITATION_PATTERNS = (
    re.compile(r"\[\d+\]"),                 # [1]
    re.compile(r"\(\s*[A-Z][A-Za-z'’-]+(?:\s+(?:et\s+al\.?|and\s+[A-Z]"
               r"[A-Za-z'’-]+))?,\s*\d{4}\s*\)"),  # (Author, 2024)
    re.compile(r"\^\d+"),                   # footnote
)
_ABSOLUTE_CLAIM_RE = re.compile(
    r"\b(always|never|proven|provably|guaranteed?|impossible|"
    r"all\s+(?:studies|research|users|customers)|"
    r"(?:studies|research)\s+(?:show|prove|demonstrate)s?)\b",
    re.IGNORECASE,
)
_VERIFICATION_MARKERS = (
    "test", "check", "verif", "acceptance", "review", "inspect",
)
_LIMITATION_MARKERS = (
    "limitation", "caveat", "uncertain", "not covered", "out of scope",
    "assumption", "risk",
)


def _words(text: str) -> list[str]:
    return [word for word in text.split() if word.strip()]


def _has_section(deliverable: str, name: str) -> bool:
    """A section name counts as present when it appears as a Markdown/ATX
    heading, a Setext underline target, or a standalone label line."""

    wanted = " ".join(name.split()).casefold()
    if not wanted:
        return False
    for raw_line in deliverable.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        elif set(line) <= set("=-") and len(line) >= 2:
            continue
        normalized = " ".join(line.split()).casefold()
        normalized = normalized.rstrip(":").strip()
        if normalized == wanted:
            return True
        # numbered headings: "2. Results" matches required section "Results"
        no_number = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", normalized)
        if no_number == wanted:
            return True
    return False


def _parse_length_bounds(goal: dict) -> dict:
    """Deterministic length bounds stated by the goal itself."""

    texts: list[tuple[str, str]] = []
    contract = goal.get("output_contract") or {}
    length_field = contract.get("length")
    if isinstance(length_field, str) and length_field.strip():
        texts.append(("output_contract.length", length_field))
    for field in ("requirements", "constraints", "deliverables",
                  "quality_criteria", "acceptance_criteria"):
        for index, entry in enumerate(goal.get(field) or ()):
            if isinstance(entry, str) and _TARGET_WORDS_RE.search(entry):
                texts.append((f"goal.{field}[{index}]", entry))

    max_bounds: list[tuple[str, str, int]] = []
    min_bounds: list[tuple[str, str, int]] = []
    targets: list[tuple[str, str, int]] = []
    for ref, text in texts:
        for pattern in (_MAX_WORDS_RE, _MAX_WORDS_TRAILING_RE):
            for match in pattern.finditer(text):
                max_bounds.append(
                    (ref, match.group(0), int(match.group(1).replace(",", "")))
                )
        for match in _MIN_WORDS_RE.finditer(text):
            min_bounds.append((ref, match.group(0), int(match.group(1).replace(",", ""))))
        for match in _TARGET_WORDS_RE.finditer(text):
            value = int(match.group(1).replace(",", ""))
            already_bound = any(
                match.group(0).casefold() in bound_text.casefold()
                for _, bound_text, _ in max_bounds + min_bounds
                if _ == ref
            )
            if not already_bound:
                targets.append((ref, match.group(0), value))

    distinct_max = sorted({value for _, _, value in max_bounds})
    distinct_min = sorted({value for _, _, value in min_bounds})
    stated = isinstance(length_field, str) and bool(length_field.strip())
    return {
        "max": max_bounds,
        "min": min_bounds,
        "target": targets,
        "distinct_max": distinct_max,
        "distinct_min": distinct_min,
        "note": _LENGTH_NOTE if stated and not (
            max_bounds or min_bounds or targets
        ) else None,
    }


def _effective_length_bounds(goal: dict, request: dict) -> dict:
    bounds = _parse_length_bounds(goal) if request["use_output_contract"] else {
        "max": [], "min": [], "target": [],
        "distinct_max": [], "distinct_min": [], "note": None,
    }
    if request["min_words"] is not None:
        bounds["min"].append(("request.min_words", "min_words", request["min_words"]))
        bounds["distinct_min"] = sorted(set(bounds["distinct_min"]) | {request["min_words"]})
    if request["max_words"] is not None:
        bounds["max"].append(("request.max_words", "max_words", request["max_words"]))
        bounds["distinct_max"] = sorted(set(bounds["distinct_max"]) | {request["max_words"]})
    return bounds


def _extract_inclusions(constraint: str) -> list[str]:
    found = []
    for match in _INCLUSION_RE.finditer(constraint):
        phrase = match.group(2).strip().strip("\"'“”‘’`*")
        phrase = phrase.rstrip(".")
        if len(phrase) >= 3:
            found.append(phrase)
    return found


def _extract_prohibitions(constraint: str) -> list[str]:
    found = []
    for match in _PROHIBITION_RE.finditer(constraint):
        phrase = match.group(1).strip().strip("\"'“”‘’`*").rstrip(".")
        if len(phrase) >= 3:
            found.append(phrase)
    return found


def _has_attribution(deliverable: str) -> bool:
    lowered = deliverable.casefold()
    if any(marker in lowered for marker in _ATTRIBUTION_MARKERS):
        return True
    return any(pattern.search(deliverable) for pattern in _CITATION_PATTERNS)


def _in_negated_context(deliverable: str, phrase: str) -> bool:
    """True when the phrase only appears inside a sentence that itself
    negates it ("we do not assume X") — compliance, not violation."""

    lowered_phrase = phrase.casefold()
    for sentence in _SENTENCE_SPLIT_RE.split(deliverable):
        if lowered_phrase in sentence.casefold() and _NEGATION_RE.search(
            sentence
        ):
            return True
    return False


def _covers(deliverable_text: str, phrase: str) -> bool:
    """A deliverable phrase counts as covered when its full text appears, or
    when every significant token (4+ characters) appears anywhere."""

    lowered = deliverable_text.casefold()
    core = " ".join(phrase.split()).casefold()
    if not core:
        return True
    if core in lowered:
        return True
    tokens = [t for t in re.split(r"\W+", core) if len(t) >= 4]
    if not tokens:
        # every token is short; require the full phrase
        return False
    return all(token in lowered for token in tokens)


def _format_findings(goal: dict, deliverable: str, words_count: int) -> list[dict]:
    """Objective output-format checks derived from output_contract.format."""

    issues: list[dict] = []
    contract = goal.get("output_contract") or {}
    format_field = contract.get("format")
    if not isinstance(format_field, str) or not format_field.strip():
        return issues
    lowered = format_field.casefold()

    def add(status: str, evidence: str, explanation: str, action: str,
            severity: str = "medium") -> None:
        issues.append(
            _issue(
                "Q-format-mismatch",
                requirement_ref="goal.output_contract.format",
                evidence=evidence,
                explanation=explanation,
                corrective_action=action,
                status=status,
                severity=severity,
            )
        )

    if "json" in lowered:
        stripped = deliverable.strip()
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            add(
                "confirmed",
                "output_contract.format states JSON; the deliverable does "
                "not parse as JSON",
                "a JSON deliverable must parse as JSON; this one does not",
                "emit the deliverable as valid JSON, or correct the goal's "
                "stated format",
            )
    if "markdown" in lowered or "md" in lowered.split():
        if not any(line.strip().startswith("#") for line in deliverable.splitlines()):
            add(
                "warning",
                "output_contract.format states Markdown; no Markdown heading "
                "was found",
                "the deliverable carries no Markdown structure although the "
                "goal names Markdown",
                "structure the deliverable with Markdown headings or correct "
                "the stated format",
                severity="low",
            )
    if "table" in lowered:
        if "|" not in deliverable:
            add(
                "warning",
                "output_contract.format mentions a table; no table markup "
                "was found",
                "the deliverable contains no table markup although the goal "
                "names a table",
                "include the table (Markdown pipes or an explicit table "
                "section) or correct the stated format",
                severity="low",
            )
    return issues


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


def _goal_header(goal: dict) -> dict:
    return {
        "goal_identity": goal_identity(goal),
        "goal_sha256": goal_content_sha256(goal),
        "profile": goal["profile"],
        "profile_version": profile_versions()[goal["profile"]],
        "authority": goal_authority(goal),
        "provenance_source": goal["provenance"]["source"],
    }


def _finalise(kind: str, goal: dict, issues: list[dict], *,
              deliverable: dict | None, request: dict | None,
              memory: dict | None, checks: list[str]) -> dict:
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for issue in issues:
        by_status[issue["status"]] = by_status.get(issue["status"], 0) + 1
        by_severity[issue["severity"]] = by_severity.get(issue["severity"], 0) + 1
        by_category[issue["category"]] = by_category.get(issue["category"], 0) + 1
    issues.sort(
        key=lambda issue: (
            _SEVERITY_ORDER[issue["severity"]],
            issue["rule_id"],
            issue["issue_id"],
        )
    )
    content = {
        "kind": kind,
        "goal": _goal_header(goal),
        "deliverable": deliverable,
        "request": request,
        "memory": memory,
        "issues": issues,
        "counts": {
            "total": len(issues),
            "by_status": dict(sorted(by_status.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "by_category": dict(sorted(by_category.items())),
        },
        "checks_run": checks,
        "limitations": list(REPORT_LIMITATIONS),
    }
    content_sha = hashlib.sha256(
        json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "format": QUALITY_REPORT_FORMAT,
        "format_version": QUALITY_REPORT_VERSION,
        "taxonomy_version": QUALITY_TAXONOMY_VERSION,
        "rules_version": QUALITY_RULES_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "aperio_version": _aperio_version(),
        "content_sha256": content_sha,
        "content": content,
    }


def _aperio_version() -> str:
    from aperio import __version__

    return __version__


def run_preflight(goal: dict, *, memory: dict | None = None) -> dict:
    """Inspect a validated goal before work begins; read-only, no writes.

    ``memory`` is an optional recall selection (the output of
    ``aperio.memrecall.recall_memories``); it is inspected, never modified.
    """

    issues: list[dict] = []
    checks: list[str] = []

    def check(rule_id: str) -> None:
        checks.append(rule_id)

    # Authority: untrusted instructions must be visible as a finding, not a
    # footnote. Nothing here re-labels the goal; the M2 authority model is
    # the only authority source.
    check("P-untrusted-instructions")
    if goal_authority(goal) == "untrusted":
        issues.append(
            _issue(
                "P-untrusted-instructions",
                requirement_ref="goal.provenance.source",
                evidence=(
                    f"declared source is {goal['provenance']['source']!r}"
                ),
                explanation=(
                    "this goal was not declared by the operator; every "
                    "instruction in it is an untrusted proposal until the "
                    "operator reviews and adopts it"
                ),
                corrective_action=(
                    "have the operator review each instruction and re-declare "
                    "the goal with provenance.source 'operator' before it is "
                    "treated as authoritative"
                ),
            )
        )

    # Objective clarity.
    check("P-objective-underspecified")
    objective_words = len(_words(goal["objective"]))
    if objective_words < 4:
        issues.append(
            _issue(
                "P-objective-underspecified",
                requirement_ref="goal.objective",
                evidence=goal["objective"],
                explanation=(
                    f"the objective is {objective_words} word(s); it states "
                    "very little to act on or to verify against"
                ),
                corrective_action=(
                    "state what is wanted, for whom, and what a useful "
                    "result changes"
                ),
            )
        )

    completeness = {
        "acceptance_criteria": goal["acceptance_criteria"],
        "deliverables": goal["deliverables"],
        "quality_criteria": goal["quality_criteria"],
    }
    for field, values in completeness.items():
        rule_id = {
            "acceptance_criteria": "P-missing-acceptance-criteria",
            "deliverables": "P-missing-deliverables",
            "quality_criteria": "P-missing-quality-criteria",
        }[field]
        check(rule_id)
        if not values:
            issues.append(
                _issue(
                    rule_id,
                    requirement_ref=f"goal.{field}",
                    evidence=f"goal.{field} is empty",
                    explanation=(
                        "without it, a result cannot be checked "
                        + {
                            "acceptance_criteria": "against an explicit "
                            "definition of done",
                            "deliverables": "against named outputs",
                            "quality_criteria": "against explicit quality "
                            "expectations",
                        }[field]
                    ),
                    corrective_action=(
                        "add the field to the goal, or accept and record "
                        "that it is deliberately absent"
                    ),
                )
            )

    # Duplicated instructions across the instruction-bearing lists.
    check("P-duplicate-instructions")
    seen: dict[str, str] = {}
    for field in ("requirements", "constraints", "deliverables",
                  "quality_criteria", "acceptance_criteria"):
        for index, entry in enumerate(goal.get(field) or ()):
            key = " ".join(entry.split()).casefold()
            if key in seen:
                issues.append(
                    _issue(
                        "P-duplicate-instructions",
                        requirement_ref=f"goal.{field}[{index}]",
                        evidence=(
                            f"duplicates {seen[key]}: {entry}"
                        ),
                        explanation=(
                            "identical instruction text appears twice; a "
                            "copy-paste often hides an unresolved edit"
                        ),
                        corrective_action=(
                            "keep one entry and confirm the wording is "
                            "what the operator means"
                        ),
                    )
                )
            else:
                seen[key] = f"goal.{field}[{index}]"

    # Conflicting length expectations.
    check("P-conflicting-length-expectations")
    bounds = _parse_length_bounds(goal)
    if len(bounds["distinct_max"]) > 1 or len(bounds["distinct_min"]) > 1:
        parts = []
        if len(bounds["distinct_max"]) > 1:
            parts.append(
                "maxima " + ", ".join(str(v) for v in bounds["distinct_max"])
            )
        if len(bounds["distinct_min"]) > 1:
            parts.append(
                "minima " + ", ".join(str(v) for v in bounds["distinct_min"])
            )
        issues.append(
            _issue(
                "P-conflicting-length-expectations",
                requirement_ref="goal",
                evidence=(
                    "; ".join(
                        f"{ref}: {text}"
                        for ref, text, _ in bounds["max"] + bounds["min"]
                    )
                ),
                explanation=(
                    "the goal states more than one distinct length bound ("
                    + " and ".join(parts)
                    + "); a deliverable cannot satisfy both"
                ),
                corrective_action=(
                    "keep one bound per direction and correct the others"
                ),
            )
        )

    check("P-unvalidated-assumptions")
    for index, assumption in enumerate(goal["assumptions"]):
        issues.append(
            _issue(
                "P-unvalidated-assumptions",
                requirement_ref=f"goal.assumptions[{index}]",
                evidence=assumption,
                explanation=(
                    "assumptions are untested input: if one is wrong, work "
                    "built on it is wrong too"
                ),
                corrective_action=(
                    "confirm the assumption, mark how it will be validated, "
                    "or restate it as an uncertainty"
                ),
            )
        )

    check("P-unaddressed-risks")
    if goal["risks"] and not goal["acceptance_criteria"]:
        issues.append(
            _issue(
                "P-unaddressed-risks",
                requirement_ref="goal.risks",
                evidence=f"{len(goal['risks'])} risk(s) named",
                explanation=(
                    "risks are listed but no acceptance criterion says how "
                    "any of them is handled or accepted"
                ),
                corrective_action=(
                    "add acceptance criteria covering the named risks, or "
                    "record the decision to accept them"
                ),
            )
        )

    check("P-oversized-instruction-list")
    for field in ("requirements", "constraints"):
        if len(goal[field]) >= 40:
            issues.append(
                _issue(
                    "P-oversized-instruction-list",
                    requirement_ref=f"goal.{field}",
                    evidence=f"{len(goal[field])} entries (cap 50)",
                    explanation=(
                        "long instruction lists dilute attention; entries "
                        "past the ones an agent actually uses are silent "
                        "context misuse"
                    ),
                    corrective_action=(
                        "prune to the load-bearing entries or split the goal"
                    ),
                )
            )

    # Memory applicability (read-only inspection of a recall selection).
    if memory is not None:
        excluded = (memory.get("excluded") or {}).get("by_reason") or {}
        expired = int(excluded.get("expired", 0))
        check("P-expired-memory")
        if expired:
            issues.append(
                _issue(
                    "P-expired-memory",
                    requirement_ref="memory store",
                    evidence=f"{expired} memory record(s) outside their "
                    "validity window",
                    explanation=(
                        "memory that would otherwise apply has expired; "
                        "acting on it would use outdated information"
                    ),
                    corrective_action=(
                        "review the expired memories; re-propose current "
                        "replacements rather than extending old windows"
                    ),
                )
            )
        proposed = [
            m["memory_id"] for m in memory.get("selected", [])
            if m.get("state") == "proposed"
        ]
        check("P-unreviewed-memory")
        if proposed:
            issues.append(
                _issue(
                    "P-unreviewed-memory",
                    requirement_ref="memory store",
                    evidence=", ".join(sorted(proposed)[:5]),
                    explanation=(
                        "selected memory is unreviewed: proposals, not "
                        "established knowledge"
                    ),
                    corrective_action=(
                        "review the memories (accept or retract) before the "
                        "task relies on them"
                    ),
                )
            )
        conflicts = memory.get("conflicts") or []
        check("P-memory-conflict")
        if conflicts:
            issues.append(
                _issue(
                    "P-memory-conflict",
                    requirement_ref="memory store",
                    evidence="; ".join(
                        ", ".join(group["memory_ids"]) for group in conflicts[:3]
                    ),
                    explanation=(
                        "selected memories disagree about the same subject; "
                        "nothing resolves the disagreement"
                    ),
                    corrective_action=(
                        "retract or supersede the outdated memory, then "
                        "re-run the preflight"
                    ),
                )
            )

    return _finalise(
        "preflight", goal, issues,
        deliverable=None, request=None, memory=memory, checks=checks,
    )


def run_quality_check(
    goal: dict,
    *,
    deliverable_text: str,
    request: dict | None = None,
    deliverable_source: str = "operator-supplied text",
) -> dict:
    """Evaluate a candidate deliverable against the goal's explicit asks.

    ``deliverable_text`` must be UTF-8 text no larger than
    ``MAX_DELIVERABLE_BYTES``. ``request`` is a quality-request v1 mapping
    (validated if supplied). Read-only: nothing is written or altered.
    """

    request = validate_quality_request(request or {})
    if not isinstance(deliverable_text, str):
        raise ValidationError("deliverable text must be a string")
    if "\x00" in deliverable_text:
        raise ValidationError("deliverable text must not contain NUL characters")
    encoded = deliverable_text.encode("utf-8")
    if len(encoded) > MAX_DELIVERABLE_BYTES:
        raise ValidationError(
            f"deliverable text must be at most {MAX_DELIVERABLE_BYTES} UTF-8 "
            "bytes"
        )

    issues: list[dict] = []
    checks: list[str] = []
    lowered = deliverable_text.casefold()
    words_count = len(_words(deliverable_text))

    def check(rule_id: str) -> None:
        checks.append(rule_id)

    deliverable_block = {
        "source": deliverable_source,
        "utf8_bytes": len(encoded),
        "words": words_count,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "sections_found": sorted(
            {
                line.lstrip("#").strip().rstrip(":").casefold()
                for line in deliverable_text.splitlines()
                if line.strip().startswith("#")
            }
        )[:50],
    }

    check("Q-empty-deliverable")
    if not deliverable_text.strip():
        issues.append(
            _issue(
                "Q-empty-deliverable",
                requirement_ref="deliverable",
                evidence="no text",
                explanation="an empty deliverable cannot satisfy any goal",
                corrective_action="supply the actual deliverable text",
            )
        )

    # Operator-named structure and content.
    for index, name in enumerate(request["required_sections"]):
        check("Q-required-section")
        if not _has_section(deliverable_text, name):
            issues.append(
                _issue(
                    "Q-required-section",
                    requirement_ref=f"request.required_sections[{index}]",
                    evidence=f"required section {name!r} not found",
                    explanation=(
                        "the operator required this section; no heading or "
                        "label line carries that name"
                    ),
                    corrective_action=(
                        f"add a {name!r} section, or correct the requirement"
                    ),
                )
            )
    for index, text in enumerate(request["required_texts"]):
        check("Q-required-text")
        if text.casefold() not in lowered:
            issues.append(
                _issue(
                    "Q-required-text",
                    requirement_ref=f"request.required_texts[{index}]",
                    evidence=f"required text {text!r} not found",
                    explanation="the operator required this text verbatim",
                    corrective_action=(
                        "include the required text or correct the requirement"
                    ),
                )
            )
    for index, text in enumerate(request["forbidden_texts"]):
        check("Q-forbidden-text")
        if text.casefold() in lowered:
            issues.append(
                _issue(
                    "Q-forbidden-text",
                    requirement_ref=f"request.forbidden_texts[{index}]",
                    evidence=f"forbidden text {text!r} present",
                    explanation=(
                        "the operator forbade this text; its presence is a "
                        "privacy or safety violation for this deliverable"
                    ),
                    corrective_action="remove the forbidden text",
                )
            )

    # Modal constraint phrases: deterministic extractions from the goal's own
    # constraints and requirements. Each carries its source so a reviewer can
    # audit every finding.
    for field in ("constraints", "requirements"):
        for index, constraint in enumerate(goal[field] or ()):
            ref = f"goal.{field}[{index}]"
            for phrase in _extract_inclusions(constraint):
                check("Q-constraint-required-inclusion")
                if phrase.casefold() not in lowered:
                    issues.append(
                        _issue(
                            "Q-constraint-required-inclusion",
                            requirement_ref=ref,
                            evidence=(
                                f"constraint {constraint!r} requires "
                                f"{phrase!r}; not found"
                            ),
                            explanation=(
                                "the goal states an explicit inclusion "
                                "requirement and the deliverable never "
                                "mentions it"
                            ),
                            corrective_action=(
                                f"include {phrase!r} or correct the constraint"
                            ),
                        )
                    )
            for phrase in _extract_prohibitions(constraint):
                check("Q-constraint-prohibition")
                if phrase.casefold() in lowered and not _in_negated_context(
                    deliverable_text, phrase
                ):
                    issues.append(
                        _issue(
                            "Q-constraint-prohibition",
                            requirement_ref=ref,
                            evidence=(
                                f"constraint {constraint!r} prohibits "
                                f"{phrase!r}; it appears"
                            ),
                            explanation=(
                                "the goal states an explicit prohibition and "
                                "the deliverable violates it"
                            ),
                            corrective_action=(
                                f"remove or rework the {phrase!r} material"
                            ),
                        )
                    )

    # Length bounds from the request and the goal's own stated lengths.
    bounds = _effective_length_bounds(goal, request)
    for ref, text, value in bounds["min"]:
        check("Q-min-length")
        if words_count < value:
            issues.append(
                _issue(
                    "Q-min-length",
                    requirement_ref=ref,
                    evidence=f"{text!r} requires >= {value} words; the "
                    f"deliverable has {words_count}",
                    explanation="the deliverable is shorter than the stated "
                    "minimum length",
                    corrective_action="expand the deliverable or correct the "
                    "stated bound",
                )
            )
    for ref, text, value in bounds["max"]:
        check("Q-max-length")
        if words_count > value:
            issues.append(
                _issue(
                    "Q-max-length",
                    requirement_ref=ref,
                    evidence=f"{text!r} allows <= {value} words; the "
                    f"deliverable has {words_count}",
                    explanation="the deliverable exceeds the stated maximum "
                    "length",
                    corrective_action="trim the deliverable or correct the "
                    "stated bound",
                )
            )
    for ref, text, value in bounds["target"]:
        check("Q-target-length")
        tolerance = max(1, int(value * 0.1))
        if not (value - tolerance <= words_count <= value + tolerance):
            issues.append(
                _issue(
                    "Q-target-length",
                    requirement_ref=ref,
                    evidence=f"{text!r} targets about {value} words; the "
                    f"deliverable has {words_count}",
                    explanation=(
                        "the deliverable misses the stated approximate "
                        "length by more than 10 percent"
                    ),
                    corrective_action="adjust the length or restate the goal "
                    "with a range",
                )
            )

    # Deliverable coverage: a named deliverable that is never mentioned is a
    # signal, not proof — so this is a warning, never a confirmed failure.
    for index, entry in enumerate(goal["deliverables"]):
        check("Q-deliverable-coverage")
        phrase = entry.strip().strip("\"'“”‘’*").strip(" .")
        if len(phrase) >= 4 and not _covers(deliverable_text, phrase):
            issues.append(
                _issue(
                    "Q-deliverable-coverage",
                    requirement_ref=f"goal.deliverables[{index}]",
                    evidence=f"deliverable {entry!r} is never mentioned",
                    explanation=(
                        "the goal names this deliverable and the text never "
                        "refers to it; absence of a mention is not proof the "
                        "deliverable is missing, but it is a signal"
                    ),
                    corrective_action=(
                        "cover the deliverable explicitly or remove it from "
                        "the goal"
                    ),
                )
            )

    # Evidence and attribution.
    needs_evidence = bool(goal["evidence_requirements"])
    if needs_evidence:
        check("Q-missing-attribution")
        if not _has_attribution(deliverable_text):
            issues.append(
                _issue(
                    "Q-missing-attribution",
                    requirement_ref="goal.evidence_requirements",
                    evidence="no URL, citation, or source marker found",
                    explanation=(
                        "the goal requires evidence and the deliverable "
                        "carries no recognizable source attribution"
                    ),
                    corrective_action=(
                        "cite sources for factual claims, or correct the "
                        "goal's evidence requirement"
                    ),
                )
            )
        check("Q-unsupported-absolute-claim")
        absolute_sentences = []
        for sentence in re.split(r"(?<=[.!?])\s+", deliverable_text):
            if _ABSOLUTE_CLAIM_RE.search(sentence):
                absolute_sentences.append(sentence.strip())
        if absolute_sentences:
            issues.append(
                _issue(
                    "Q-unsupported-absolute-claim",
                    requirement_ref="goal.evidence_requirements",
                    evidence=" | ".join(
                        _bound_evidence(s, 120)
                        for s in absolute_sentences[:5]
                    ),
                    explanation=(
                        "absolute factual phrasing appears in a deliverable "
                        "whose goal demands evidence; the tool cannot check "
                        "whether these claims are supported"
                    ),
                    corrective_action=(
                        "support or qualify the absolute claims with cited "
                        "evidence"
                    ),
                )
            )
        check("Q-source-support-unknown")
        if _has_attribution(deliverable_text):
            issues.append(
                _issue(
                    "Q-source-support-unknown",
                    requirement_ref="goal.evidence_requirements",
                    evidence="attribution markers present",
                    explanation=(
                        "the tool found attribution but cannot read the "
                        "sources; whether they support the claims remains "
                        "unverified and needs human review"
                    ),
                    corrective_action=(
                        "a human checks the cited sources against the claims"
                    ),
                    severity="info",
                    status="unknown",
                )
            )

    # Output format expectations.
    for finding in _format_findings(goal, deliverable_text, words_count):
        check("Q-format-mismatch")
        issues.append(finding)

    # Limitations: a goal that names risks or uncertainties should see them
    # acknowledged somewhere.
    if goal["risks"] or goal["uncertainties"]:
        check("Q-missing-limitations")
        lowered_all = lowered
        if not any(marker in lowered_all for marker in _LIMITATION_MARKERS):
            issues.append(
                _issue(
                    "Q-missing-limitations",
                    requirement_ref="goal.risks|goal.uncertainties",
                    evidence="no limitation or caveat language found",
                    explanation=(
                        "the goal names risks or uncertainties and the "
                        "deliverable states no limitations or caveats"
                    ),
                    corrective_action=(
                        "state the known limitations explicitly"
                    ),
                )
            )

    # Software-profile verification evidence (the one profile-scoped rule).
    profile = goal["profile"]
    rule = RULES["Q-missing-verification-evidence"]
    if rule["profiles"] is not None and profile in rule["profiles"]:
        check("Q-missing-verification-evidence")
        if goal["acceptance_criteria"] and not any(
            marker in lowered for marker in _VERIFICATION_MARKERS
        ):
            issues.append(
                _issue(
                    "Q-missing-verification-evidence",
                    requirement_ref="goal.acceptance_criteria",
                    evidence="no check/verify/test language found",
                    explanation=(
                        "the goal defines acceptance criteria and the "
                        "deliverable never states how they would be checked"
                    ),
                    corrective_action=(
                        "describe the verification for each acceptance "
                        "criterion"
                    ),
                )
            )

    return _finalise(
        "deliverable", goal, issues,
        deliverable=deliverable_block, request=request, memory=None,
        checks=sorted(set(checks)),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_STATUS_MARK = {
    "confirmed": "confirmed failure",
    "warning": "warning",
    "unknown": "unknown — needs human judgment",
    "recommendation": "recommendation",
}


def render_quality_report_markdown(report: dict) -> str:
    """Render the human-readable report from one canonical payload."""

    content = report["content"]
    lines: list[str] = []
    kind_title = (
        "Preflight report"
        if content["kind"] == "preflight"
        else "Deliverable quality report"
    )
    goal = content["goal"]
    lines.append(f"# {kind_title}")
    lines.append("")
    lines.append(f"Goal: `{goal['goal_identity']}` — profile "
                 f"**{goal['profile']}** v{goal['profile_version']}, "
                 f"authority **{goal['authority']}**")
    if content["deliverable"]:
        block = content["deliverable"]
        lines.append(
            f"Deliverable: {block['words']} words, {block['utf8_bytes']} "
            f"UTF-8 bytes, sha256 `{block['sha256'][:12]}…`"
        )
    lines.append("")
    counts = content["counts"]
    if counts["total"] == 0:
        lines.append("**No findings.** Nothing checked contradicted the "
                     "goal's explicit, objectively checkable requirements.")
    else:
        summary = ", ".join(
            f"{count} {status}"
            for status, count in sorted(counts["by_status"].items())
        )
        lines.append(f"{counts['total']} finding(s): {summary}.")
    lines.append("")
    for issue in content["issues"]:
        lines.append(
            f"## {issue['issue_id']} — {_STATUS_MARK[issue['status']]} "
            f"({issue['severity']})"
        )
        lines.append("")
        lines.append(
            f"- **Category:** {issue['category']} "
            f"(rule `{issue['rule_id']}`, quality-rules "
            f"v{issue['rule_version']})"
        )
        if issue["requirement_ref"]:
            lines.append(f"- **Requirement:** `{issue['requirement_ref']}`")
        lines.append(f"- **Evidence:** {issue['evidence']}")
        lines.append(f"- **Explanation:** {issue['explanation']}")
        lines.append(f"- **Suggested action:** {issue['corrective_action']}")
        lines.append("")
    if content["memory"]:
        memory = content["memory"]
        selected = len(memory.get("selected", []))
        excluded = (memory.get("excluded") or {}).get("by_reason") or {}
        lines.append("## Memory inspected (read-only)")
        lines.append("")
        lines.append(
            f"- selected: {selected}; excluded: "
            + ", ".join(f"{reason}: {count}"
                        for reason, count in sorted(excluded.items()))
        )
        lines.append("")
    lines.append("## Checks run")
    lines.append("")
    for rule_id in content["checks_run"]:
        rule = RULES[rule_id]
        lines.append(f"- `{rule_id}` — {rule['description']}")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in content["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)
