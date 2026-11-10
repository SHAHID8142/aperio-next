"""Roadmap data model: the LEVEL-1 master project planning document.

Aperio's two-level planning model keeps ONE execution engine: the
detailed, approved plan (``plan.json``, :mod:`aperio.planmodel`) stays
the only source of executable task truth. The roadmap is the master
planning document above it — overall lifecycle, major capabilities,
milestones with dependencies, risks, and acceptance conditions — and it
carries **no execution state of its own**: milestone progress is derived
from which milestone the current plan implements, never stored or
guessed. This module is therefore deliberately small: it validates and
derives, it never schedules.

Pure functions over plain dicts — no filesystem access (persistence
lives in :mod:`aperio.roadmapstore`). Same document conventions as the
plan: schema-versioned, unknown keys found ON DISK are tolerated and
preserved, while input documents (``roadmap-create`` / ``roadmap-revise``)
are strictly validated.

Roadmap schema v1::

    {
      "schema_version": 1,
      "roadmap_version": <int, bumps on every saved change>,
      "approval": {"status": "draft"|"approved", "approved_revision": null|int,
                   "approved_at": null|ISO-Z, "approved_by": null|str},
      "objective": str (the project mission),
      "lifecycle": str (the overall delivery lifecycle),
      "milestones": [<milestone>],
      "created_at": ISO-Z, "updated_at": ISO-Z, "aperio_version": str
    }

Milestone (all content keys are author-supplied; there are no
system-managed milestone fields — the roadmap tracks no execution)::

    {
      "id": "MILESTONE-\\d+" (unique),
      "title": single-line str,
      "capabilities": [str] (>=1)  (the major capabilities delivered),
      "depends_on": ["MILESTONE-..."],
      "risks": [str],
      "acceptance_conditions": [str] (>=1)
    }

Later milestones are elaborated progressively through ``roadmap-revise``
(same rules as plan revisions: stale revisions refused, approval resets
to draft). The linkage to the detailed level is one-directional: a plan
may declare the milestone it implements (plan-schema v3 optional
``milestone`` field); the roadmap never points back, so the two
documents cannot contradict each other about execution state.
"""

from __future__ import annotations

from aperio.errors import StateError, ValidationError
from aperio.planmodel import (
    _check_str_list,
    _check_text,
    _is_iso_utc_z,
    validate_dependency_graph,
    validate_milestone_id,
)

ROADMAP_SCHEMA_VERSION = 1
ROADMAP_SCHEMA_READ_VERSIONS = (1,)

ROADMAP_CONTENT_KEYS = (
    "objective",
    "lifecycle",
    "milestones",
)
MILESTONE_CONTENT_KEYS = (
    "id",
    "title",
    "capabilities",
    "depends_on",
    "risks",
    "acceptance_conditions",
)
ROADMAP_SYSTEM_KEYS = (
    "schema_version",
    "roadmap_version",
    "approval",
    "created_at",
    "updated_at",
    "aperio_version",
)

_ROADMAP_STR_FIELDS = ("objective", "lifecycle")


def validate_roadmap_input(data: object) -> dict:
    """Validate a ``roadmap-create`` / ``roadmap-revise`` input document.

    Accepts ONLY author-supplied content keys; system-managed keys and
    unknown keys are rejected so a typo can never silently land in the
    shared roadmap.

    :returns: a normalized content dict (safe to embed into a document).
    :raises ValidationError: for any shape violation, with the offending
        key named.
    """
    if not isinstance(data, dict):
        raise ValidationError("roadmap input must be a JSON object")

    unknown = sorted(set(data) - set(ROADMAP_CONTENT_KEYS))
    if unknown:
        raise ValidationError(
            f"roadmap input has unknown or system-managed field(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(ROADMAP_CONTENT_KEYS)})"
        )
    missing = [key for key in ROADMAP_CONTENT_KEYS if key not in data]
    if missing:
        raise ValidationError(
            f"roadmap input is missing required field(s): {', '.join(missing)}"
        )

    roadmap: dict = {}
    for field in _ROADMAP_STR_FIELDS:
        roadmap[field] = _check_text("roadmap", field, data[field])

    milestones_input = data["milestones"]
    if not isinstance(milestones_input, list):
        raise ValidationError(
            "roadmap: field 'milestones' must be a list of milestone objects"
        )
    if not milestones_input:
        raise ValidationError(
            "roadmap: field 'milestones' needs at least 1 milestone — "
            "an empty roadmap describes no deliverable lifecycle"
        )
    roadmap["milestones"] = [
        validate_milestone_input(milestone, where=f"milestones[{index}]")
        for index, milestone in enumerate(milestones_input)
    ]
    validate_dependency_graph(
        roadmap["milestones"], artifact="roadmap", entity="milestone"
    )
    return roadmap


def validate_milestone_input(milestone: object, *, where: str) -> dict:
    """Validate one author-supplied milestone (content keys only)."""
    if not isinstance(milestone, dict):
        raise ValidationError(f"roadmap: {where} must be a JSON object")
    unknown = sorted(set(milestone) - set(MILESTONE_CONTENT_KEYS))
    if unknown:
        raise ValidationError(
            f"roadmap: {where} has unknown or system-managed field(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(MILESTONE_CONTENT_KEYS)})"
        )
    missing = [key for key in MILESTONE_CONTENT_KEYS if key not in milestone]
    if missing:
        raise ValidationError(
            f"roadmap: {where} is missing required field(s): {', '.join(missing)}"
        )
    cleaned: dict = {
        "id": validate_milestone_id(milestone["id"], where=f"roadmap: {where}.id")
    }
    cleaned["title"] = _check_text(
        "roadmap", f"{where}.title", milestone["title"], single_line=True
    )
    for field in ("capabilities", "acceptance_conditions"):
        cleaned[field] = _check_str_list(
            "roadmap", f"{where}.{field}", milestone[field], min_len=1
        )
    for field in ("depends_on", "risks"):
        cleaned[field] = _check_str_list(
            "roadmap", f"{where}.{field}", milestone[field]
        )
    return cleaned


def validate_roadmap_document(data: object, path) -> dict:
    """Validate a parsed roadmap.json document (unknown keys tolerated).

    :raises StateError: for shape/schema damage (the caller converts
        future schema versions to their own message before this runs).
    """
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: roadmap must be a JSON object")

    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise StateError(f"{path} is corrupt: 'schema_version' must be an integer")
    if schema_version not in ROADMAP_SCHEMA_READ_VERSIONS:
        supported = ", ".join(str(v) for v in ROADMAP_SCHEMA_READ_VERSIONS)
        raise StateError(
            f"{path} declares roadmap schema version {schema_version}, but "
            f"this aperio supports versions {supported}; aperio will not "
            "modify it."
        )
    roadmap_version = data.get("roadmap_version")
    if (
        isinstance(roadmap_version, bool)
        or not isinstance(roadmap_version, int)
        or roadmap_version < 1
    ):
        raise StateError(
            f"{path} is corrupt: 'roadmap_version' must be a positive integer"
        )

    approval = data.get("approval")
    if not isinstance(approval, dict):
        raise StateError(f"{path} is corrupt: 'approval' must be an object")
    status = approval.get("status")
    if status not in ("draft", "approved"):
        raise StateError(
            f"{path} is corrupt: 'approval.status' must be 'draft' or 'approved'"
        )
    rev = approval.get("approved_revision")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        raise StateError(
            f"{path} is corrupt: 'approval.approved_revision' must be null "
            "or an integer"
        )
    for field in ("approved_at", "approved_by"):
        value = approval.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise StateError(
                f"{path} is corrupt: 'approval.{field}' must be null or a "
                "non-empty string"
            )
    if status == "approved":
        if not isinstance(rev, int) or rev < 1:
            raise StateError(
                f"{path} is corrupt: approved roadmaps must record a "
                "positive 'approval.approved_revision'"
            )
        if not isinstance(approval.get("approved_at"), str) or not approval["approved_at"]:
            raise StateError(
                f"{path} is corrupt: approved roadmaps must record 'approved_at'"
            )
    if (
        approval["status"] == "approved"
        and approval["approved_revision"] is not None
        and approval["approved_revision"] > roadmap_version
    ):
        raise StateError(
            f"{path} is corrupt: approval.approved_revision "
            f"({approval['approved_revision']}) exceeds roadmap_version "
            f"({roadmap_version})"
        )

    authors = data.get("authors")
    if authors is not None:
        if not isinstance(authors, dict):
            raise StateError(f"{path} is corrupt: 'authors' must be an object")
        for key, record in authors.items():
            if not isinstance(key, str) or not key.isdigit():
                raise StateError(
                    f"{path} is corrupt: authors keys must be revision numbers"
                )
            revision = int(key)
            if revision < 1 or revision > roadmap_version:
                raise StateError(
                    f"{path} is corrupt: authors[{key}] is not a revision of "
                    "this roadmap"
                )
            if not isinstance(record, dict):
                raise StateError(
                    f"{path} is corrupt: authors[{key}] must be an object"
                )
            for field in ("actor", "role", "recorded_at", "method"):
                value = record.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise StateError(
                        f"{path} is corrupt: authors[{key}].{field} must be "
                        "a non-empty string"
                    )
            if record["method"] not in ("created", "revised", "adopted"):
                raise StateError(
                    f"{path} is corrupt: authors[{key}].method is invalid"
                )
            if record["role"] not in ("planner", "implementer", "reviewer"):
                raise StateError(
                    f"{path} is corrupt: authors[{key}].role is not a "
                    "built-in role"
                )

    for field in ("created_at", "updated_at", "aperio_version"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise StateError(
                f"{path} is corrupt: '{field}' must be a non-empty string"
            )

    missing_content = [key for key in ROADMAP_CONTENT_KEYS if key not in data]
    if missing_content:
        raise StateError(
            f"{path} is corrupt: missing roadmap field(s): "
            f"{', '.join(missing_content)}"
        )
    milestones = data.get("milestones")
    if not isinstance(milestones, list):
        raise StateError(f"{path} is corrupt: 'milestones' must be a list")

    ids: set[str] = set()
    try:
        for index, milestone in enumerate(milestones):
            cleaned = validate_milestone_input(
                milestone, where=f"milestones[{index}]"
            )
            if cleaned["id"] in ids:
                raise ValidationError(
                    f"roadmap: duplicate milestone id {cleaned['id']} "
                    f"(milestones[{index}])"
                )
            ids.add(cleaned["id"])
        validate_dependency_graph(
            milestones, artifact="roadmap", entity="milestone"
        )
    except ValidationError as exc:
        raise StateError(f"{path} is corrupt: {exc}") from None
    return data


# --- derived milestone status ---------------------------------------------------


def derive_milestone_view(roadmap: dict, plan: dict | None) -> list[dict]:
    """Per-milestone derived view for display: which milestone the current
    plan implements, and whether each milestone's roadmap dependencies are
    implemented by that plan.

    Pure derivation over the two documents — no storage, no guessing. A
    milestone is never reported "complete": completion is a property of
    plan execution (verified tasks), not of the roadmap document.
    """
    plan_milestone = None
    if plan is not None:
        plan_milestone = plan.get("milestone")
    view: list[dict] = []
    for milestone in roadmap["milestones"]:
        in_plan = plan_milestone is not None and plan_milestone == milestone["id"]
        dependencies_in_plan = None
        if plan is not None:
            dependencies_in_plan = all(
                dep == plan_milestone for dep in milestone["depends_on"]
            )
        view.append(
            {
                "id": milestone["id"],
                "title": milestone["title"],
                "in_current_plan": in_plan,
                "dependencies_in_plan": dependencies_in_plan,
            }
        )
    return view
