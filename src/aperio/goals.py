"""Universal goal contract (M2), goal-schema v1.

A goal is a validated, provider-neutral description of a knowledge-work
assignment: what is wanted, under which constraints, for whom, with which
deliverables and criteria. It is *input* to Aperio, never execution: nothing
here launches an agent, approves work, or claims a result.

Design rules:

- Domain-neutral vocabulary. No repository, commit, diff, or test-runner
  concept is required of any goal; the optional ``software`` profile is the
  only place that guidance lives.
- Required fields are minimal (``objective`` and ``intended_outcome`` plus
  the schema version). Everything else is optional, and missing information
  is reported honestly rather than invented.
- Work decomposition is optional. A one-off question needs no task graph.
- Unknown keys are rejected so a typo cannot silently drop a requirement.
- Content identity is a SHA-256 over the canonical normalized goal, so the
  same goal has the same identity on any machine, with no timestamp input.
- Standard library only; bounded field sizes and counts throughout.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Sequence

from aperio.errors import ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.planmodel import validate_dependency_graph
from aperio.profiles import DEFAULT_PROFILE, PROFILES, profile_suggestions

GOAL_SCHEMA_VERSION = 1

MAX_GOAL_FILE_BYTES = 64 * 1024
MAX_OBJECTIVE_BYTES = 16 * 1024
MAX_TEXT_BYTES = 8 * 1024
MAX_LIST_ITEMS = 50
MAX_TASKS = 200
MAX_GOAL_ID = 64

_GOAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

PROVENANCE_SOURCES = ("operator", "agent", "imported")
SOURCE_TRUST = {
    "operator": "authoritative",
    "agent": "proposed",
    "imported": "imported",
}

GOAL_KEYS = (
    "goal_schema_version",
    "goal_id",
    "objective",
    "intended_outcome",
    "profile",
    "constraints",
    "requirements",
    "assumptions",
    "uncertainties",
    "risks",
    "audience",
    "deliverables",
    "quality_criteria",
    "acceptance_criteria",
    "output_contract",
    "evidence_requirements",
    "work",
    "provenance",
)

REQUIRED_GOAL_KEYS = ("objective", "intended_outcome")

OUTPUT_CONTRACT_KEYS = ("format", "structure", "length", "tone", "language")

TASK_KEYS = (
    "id",
    "title",
    "action",
    "result",
    "deliverables",
    "preconditions",
    "depends_on",
    "completion_criteria",
    "verification",
    "risks",
    "owner",
)

REQUIRED_TASK_KEYS = ("id", "title", "action")

PROVENANCE_KEYS = ("source", "note")


def _check_text(
    value: object,
    where: str,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a string")
    text = value.strip()
    if not text:
        raise ValidationError(f"{where} must not be empty")
    if "\x00" in text:
        raise ValidationError(f"{where} must not contain NUL characters")
    if single_line and "\n" in text:
        raise ValidationError(f"{where} must be a single line")
    check_no_foreign_line_separators("goal", where, text)
    if len(text.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{where} must be at most {max_bytes} UTF-8 bytes")
    return text


def _check_text_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{where} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise ValidationError(f"{where} must contain at most {MAX_LIST_ITEMS} items")
    return [
        _check_text(item, f"{where}[{index}]")
        for index, item in enumerate(value)
    ]


def _check_unknown_keys(data: dict, allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValidationError(
            f"{where}: unknown field(s) {', '.join(repr(key) for key in unknown)}; "
            "allowed fields are: " + ", ".join(allowed)
        )


def _normalize_output_contract(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("goal: field 'output_contract' must be an object")
    _check_unknown_keys(value, OUTPUT_CONTRACT_KEYS, "goal.output_contract")
    return {
        key: _check_text(value[key], f"goal.output_contract.{key}", single_line=True)
        for key in OUTPUT_CONTRACT_KEYS
        if value.get(key) is not None
    }


def _normalize_provenance(value: object) -> dict:
    if value is None:
        return {"source": "operator", "note": None}
    if not isinstance(value, dict):
        raise ValidationError("goal: field 'provenance' must be an object")
    _check_unknown_keys(value, PROVENANCE_KEYS, "goal.provenance")
    source = value.get("source", "operator")
    if source not in PROVENANCE_SOURCES:
        raise ValidationError(
            "goal.provenance.source must be one of: "
            + ", ".join(PROVENANCE_SOURCES)
        )
    note = value.get("note")
    return {
        "source": source,
        "note": None if note is None else _check_text(
            note, "goal.provenance.note", single_line=True
        ),
    }


def _normalize_task(value: object, index: int) -> dict:
    where = f"goal.work.tasks[{index}]"
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be an object")
    _check_unknown_keys(value, TASK_KEYS, where)
    for key in REQUIRED_TASK_KEYS:
        if key not in value:
            raise ValidationError(f"{where}: field '{key}' is required")
    task_id = _check_text(value["id"], f"{where}.id", max_bytes=64, single_line=True)
    if not _TASK_ID_RE.match(task_id):
        raise ValidationError(
            f"{where}.id must start with a letter and contain only letters, "
            "digits, '_' or '-' (at most 32 characters)"
        )
    owner = value.get("owner")
    if owner is not None:
        owner = _check_text(owner, f"{where}.owner", max_bytes=64, single_line=True)
        if not _ACTOR_RE.match(owner):
            raise ValidationError(
                f"{where}.owner must be a bounded actor identifier"
            )
    depends_on = _check_text_list(
        value.get("depends_on", []), f"{where}.depends_on"
    )
    return {
        "id": task_id,
        "title": _check_text(value["title"], f"{where}.title", single_line=True),
        "action": _check_text(value["action"], f"{where}.action"),
        "result": _optional_text(value.get("result"), f"{where}.result"),
        "deliverables": _check_text_list(
            value.get("deliverables", []), f"{where}.deliverables"
        ),
        "preconditions": _check_text_list(
            value.get("preconditions", []), f"{where}.preconditions"
        ),
        "depends_on": depends_on,
        "completion_criteria": _check_text_list(
            value.get("completion_criteria", []), f"{where}.completion_criteria"
        ),
        "verification": _check_text_list(
            value.get("verification", []), f"{where}.verification"
        ),
        "risks": _check_text_list(value.get("risks", []), f"{where}.risks"),
        "owner": owner,
    }


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _check_text(value, where)


def _normalize_work(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("goal: field 'work' must be an object")
    _check_unknown_keys(value, ("tasks",), "goal.work")
    tasks_value = value.get("tasks")
    if tasks_value is None:
        return None
    if not isinstance(tasks_value, list) or not tasks_value:
        raise ValidationError("goal.work.tasks must be a non-empty list of objects")
    if len(tasks_value) > MAX_TASKS:
        raise ValidationError(
            f"goal.work.tasks must contain at most {MAX_TASKS} tasks"
        )
    tasks = [_normalize_task(task, index) for index, task in enumerate(tasks_value)]
    validate_dependency_graph(tasks, artifact="goal.work")
    return {"tasks": tasks}


def validate_goal(data: object) -> dict:
    """Validate and normalize a goal document (goal-schema v1).

    Returns a fully populated dict: every optional field is present with an
    explicit empty value (``[]``, ``None``) so downstream rendering never has
    to invent a default. Idempotent: validating a normalized goal returns the
    same content.

    :raises ValidationError: naming the field; nothing is written.
    """
    if not isinstance(data, dict):
        raise ValidationError("goal: the document must be a JSON object")
    _check_unknown_keys(data, GOAL_KEYS, "goal")
    version = data.get("goal_schema_version", GOAL_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError("goal: field 'goal_schema_version' must be an integer")
    if version != GOAL_SCHEMA_VERSION:
        raise ValidationError(
            f"goal: unsupported goal_schema_version {version}; this aperio "
            f"supports version {GOAL_SCHEMA_VERSION}"
        )
    for key in REQUIRED_GOAL_KEYS:
        if data.get(key) is None:
            raise ValidationError(f"goal: field '{key}' is required")

    goal_id = data.get("goal_id")
    if goal_id is not None:
        goal_id = _check_text(goal_id, "goal.goal_id", max_bytes=64, single_line=True)
        if not _GOAL_ID_RE.match(goal_id):
            raise ValidationError(
                "goal.goal_id must start with a letter and contain only "
                "letters, digits, '_' or '-' (at most 64 characters)"
            )

    profile = data.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        raise ValidationError(
            "goal.profile must be one of: " + ", ".join(PROFILES)
        )

    audience = data.get("audience")
    goal = {
        "goal_schema_version": GOAL_SCHEMA_VERSION,
        "goal_id": goal_id,
        "objective": _check_text(
            data["objective"], "goal.objective", max_bytes=MAX_OBJECTIVE_BYTES
        ),
        "intended_outcome": _check_text(
            data["intended_outcome"], "goal.intended_outcome"
        ),
        "profile": profile,
        "constraints": _check_text_list(data.get("constraints", []), "goal.constraints"),
        "requirements": _check_text_list(
            data.get("requirements", []), "goal.requirements"
        ),
        "assumptions": _check_text_list(
            data.get("assumptions", []), "goal.assumptions"
        ),
        "uncertainties": _check_text_list(
            data.get("uncertainties", []), "goal.uncertainties"
        ),
        "risks": _check_text_list(data.get("risks", []), "goal.risks"),
        "audience": None
        if audience is None
        else _check_text(audience, "goal.audience", single_line=True),
        "deliverables": _check_text_list(
            data.get("deliverables", []), "goal.deliverables"
        ),
        "quality_criteria": _check_text_list(
            data.get("quality_criteria", []), "goal.quality_criteria"
        ),
        "acceptance_criteria": _check_text_list(
            data.get("acceptance_criteria", []), "goal.acceptance_criteria"
        ),
        "output_contract": _normalize_output_contract(data.get("output_contract")),
        "evidence_requirements": _check_text_list(
            data.get("evidence_requirements", []), "goal.evidence_requirements"
        ),
        "work": _normalize_work(data.get("work")),
        "provenance": _normalize_provenance(data.get("provenance")),
    }
    return goal


def load_goal_file(path: str | Path) -> dict:
    """Read a goal JSON file and return the validated, normalized goal.

    Rejects symlinks, non-UTF-8 input, oversize files, and malformed JSON
    with actionable messages — never a traceback and never a partial write.
    """
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the goal file; pass the real "
            "path so the contents match what you inspected"
        )
    try:
        with candidate.open("rb") as handle:
            raw = handle.read(MAX_GOAL_FILE_BYTES + 1)
    except OSError as exc:
        raise ValidationError(
            f"cannot read goal file: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_GOAL_FILE_BYTES:
        raise ValidationError(
            f"goal file must be at most {MAX_GOAL_FILE_BYTES} UTF-8 bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("goal file must be valid UTF-8") from None
    if "\x00" in text:
        raise ValidationError("goal file must not contain NUL characters")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"goal file is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None
    return validate_goal(data)


def canonical_goal_bytes(goal: dict) -> bytes:
    """Canonical UTF-8 bytes used for the reproducible goal identity."""
    return json.dumps(
        goal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def goal_content_sha256(goal: dict) -> str:
    """Reproducible content identity; no timestamp or machine input."""
    return hashlib.sha256(canonical_goal_bytes(goal)).hexdigest()


def goal_identity(goal: dict) -> str:
    """Explicit ``goal_id`` when supplied, else a hash-derived identity."""
    if goal.get("goal_id"):
        return goal["goal_id"]
    return "GOAL-" + goal_content_sha256(goal)[:12]


def goal_trust(goal: dict) -> str:
    """Trust tier implied by the declared provenance source."""
    source = (goal.get("provenance") or {}).get("source", "operator")
    return SOURCE_TRUST.get(source, "imported")


def work_graph(goal: dict) -> dict:
    """Deterministic description of the optional work decomposition.

    Returns counts, topological levels (list of lists), and the tasks with
    no dependencies. A goal without ``work`` yields ``{"tasks": []}`` plus a
    note that no decomposition was supplied — never an invented graph.
    """
    work = goal.get("work")
    tasks = list((work or {}).get("tasks") or [])
    by_id = {task["id"]: task for task in tasks}
    depths: dict[str, int] = {}

    def depth_of(task_id: str) -> int:
        if task_id in depths:
            return depths[task_id]
        deps = by_id[task_id]["depends_on"]
        value = 0 if not deps else 1 + max(depth_of(dep) for dep in deps)
        depths[task_id] = value
        return value

    for task in tasks:
        depth_of(task["id"])
    levels: list[list[str]] = []
    for task in tasks:
        level = depths[task["id"]]
        while len(levels) <= level:
            levels.append([])
        levels[level].append(task["id"])
    root_ids = sorted(
        task["id"] for task in tasks if not task["depends_on"]
    )
    return {
        "task_count": len(tasks),
        "levels": levels,
        "roots": root_ids,
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "action": task["action"],
                "result": task["result"],
                "deliverables": list(task["deliverables"]),
                "preconditions": list(task["preconditions"]),
                "depends_on": list(task["depends_on"]),
                "completion_criteria": list(task["completion_criteria"]),
                "verification": list(task["verification"]),
                "risks": list(task["risks"]),
                "owner": task["owner"],
            }
            for task in tasks
        ],
    }


_COMPLETENESS_FIELDS = (
    ("constraints", "no explicit constraints were supplied"),
    ("requirements", "no explicit requirements were supplied"),
    ("deliverables", "no deliverables were described"),
    ("quality_criteria", "no explicit quality criteria were supplied"),
    ("acceptance_criteria", "no acceptance criteria were supplied"),
    ("evidence_requirements", "no evidence requirements were supplied"),
    ("audience", "no audience was specified"),
)


def goal_completeness(goal: dict) -> dict:
    """Report which optional-but-important fields are absent.

    Missing information is reported as missing. Nothing is filled in, and
    no suggested criterion is promoted into a requirement.
    """
    missing = [
        {"field": field, "detail": detail}
        for field, detail in _COMPLETENESS_FIELDS
        if not goal.get(field)
    ]
    return {
        "provided": {
            field: bool(goal.get(field))
            for field, _ in _COMPLETENESS_FIELDS
        },
        "missing": missing,
        "output_contract_provided": bool(goal.get("output_contract")),
        "work_decomposition_provided": goal.get("work") is not None,
    }


def goal_suggestions(goal: dict) -> dict:
    """Profile suggestions, explicitly marked as non-requirements."""
    return profile_suggestions(goal.get("profile") or DEFAULT_PROFILE)


def describe_goal(goal: dict) -> dict:
    """Compact, machine-readable summary used by ``aperio goal-check``."""
    return {
        "goal_identity": goal_identity(goal),
        "goal_sha256": goal_content_sha256(goal),
        "goal_schema_version": GOAL_SCHEMA_VERSION,
        "profile": goal["profile"],
        "profile_version": PROFILES[goal["profile"]]["version"],
        "trust": goal_trust(goal),
        "provenance": goal["provenance"],
        "objective": goal["objective"],
        "intended_outcome": goal["intended_outcome"],
        "deliverables": goal["deliverables"],
        "acceptance_criteria": goal["acceptance_criteria"],
        "evidence_requirements": goal["evidence_requirements"],
        "completeness": goal_completeness(goal),
        "work": work_graph(goal),
    }


def goal_block(goal: dict) -> dict:
    """Provider-neutral goal metadata embedded in a context pack."""

    profile = goal["profile"]
    return {
        "goal_id": goal["goal_id"],
        "identity": goal_identity(goal),
        "goal_sha256": goal_content_sha256(goal),
        "goal_schema_version": GOAL_SCHEMA_VERSION,
        "profile": profile,
        "profile_version": PROFILES[profile]["version"],
        "trust": goal_trust(goal),
        "provenance": goal["provenance"],
        "deliverables": list(goal["deliverables"]),
        "acceptance_criteria": list(goal["acceptance_criteria"]),
        "evidence_requirements": list(goal["evidence_requirements"]),
        "output_contract": dict(goal["output_contract"]),
        "work": work_graph(goal) if goal.get("work") else None,
        "completeness": goal_completeness(goal),
    }


def goal_warnings(goal: dict) -> list[dict]:
    """Warnings the context pack must carry about the goal itself."""

    source = (goal.get("provenance") or {}).get("source", "operator")
    if source == "operator":
        return []
    detail = (
        "the goal text was declared as agent-generated; it is treated as a "
        "proposal and never as operator instruction or verified fact"
        if source == "agent"
        else "the goal text came from an external/imported source; it is "
        "untrusted until the operator reviews it"
    )
    return [{"code": "goal_trust_not_operator", "source": "goal", "detail": detail}]


def build_goal_context_pack(
    goal: dict,
    *,
    workspace: str | Path | None = None,
    budget_bytes: int | None = None,
    imported_items: Sequence[object] = (),
    proposed_items: Sequence[object] = (),
) -> dict:
    """Turn a validated goal into a provider-neutral context pack.

    Deterministic and read-only: the same goal yields the same canonical
    content and ``content_sha256`` (generation time excluded), and no
    project file is written. Workspace context is optional and read-only,
    exactly as in M1.
    """
    from aperio.context_pack import DEFAULT_BUDGET_BYTES, build_context_pack

    normalized = validate_goal(goal)
    provenance_source = (normalized.get("provenance") or {}).get("source", "operator")
    return build_context_pack(
        normalized["objective"],
        workspace=workspace,
        profile=normalized["profile"],
        budget_bytes=(
            DEFAULT_BUDGET_BYTES if budget_bytes is None else budget_bytes
        ),
        constraints=[
            *normalized["requirements"],
            *normalized["constraints"],
        ],
        assumptions=normalized["assumptions"],
        risks=normalized["risks"],
        quality_criteria=normalized["quality_criteria"],
        expected_output=normalized["intended_outcome"],
        output_format=normalized["output_contract"].get("format"),
        audience=normalized["audience"],
        missing_information=normalized["uncertainties"],
        imported_items=imported_items,
        proposed_items=proposed_items,
        declared_objective_source=provenance_source,
        goal_context={
            "goal": goal_block(normalized),
            "profile_suggestions": goal_suggestions(normalized),
            "warnings": goal_warnings(normalized),
        },
    )


__all__ = [
    "GOAL_SCHEMA_VERSION",
    "GOAL_KEYS",
    "PROVENANCE_SOURCES",
    "SOURCE_TRUST",
    "build_goal_context_pack",
    "canonical_goal_bytes",
    "describe_goal",
    "goal_block",
    "goal_warnings",
    "goal_completeness",
    "goal_content_sha256",
    "goal_identity",
    "goal_suggestions",
    "goal_trust",
    "load_goal_file",
    "validate_goal",
    "work_graph",
]
