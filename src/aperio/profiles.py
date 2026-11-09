"""Universal task profiles (M2): declarative, data-only domain adaptation.

A profile describes *how a domain typically expresses work*. It never
executes anything, never gates completion, and never overrides an operator
instruction. Every criterion it contributes is a **suggestion**: it is
reported separately from the operator's explicit requirements and can
never become a mandatory approval or completion condition.

Profiles are plain data. There is no plugin loader, no dynamic import, and
no executable rule engine — adding a profile means adding one bounded dict
entry here, so nothing a goal file or provider supplies can inject code.
"""

from __future__ import annotations

from aperio.errors import ValidationError

PROFILE_SCHEMA_VERSION = 1

DEFAULT_PROFILE = "general"

#: Bounds applied when a profile field is echoed into a context pack.
MAX_SUGGESTIONS = 12
MAX_SUGGESTION_BYTES = 400

_PROFILE_FIELDS = (
    "id",
    "version",
    "label",
    "description",
    "pack_guidance",
    "default_output_contract",
    "focus_fields",
    "suggested_quality_criteria",
    "suggested_evidence",
    "suggested_checks",
    "output_expectations",
)

PROFILES: dict[str, dict] = {
    "general": {
        "id": "general",
        "version": 1,
        "label": "General knowledge work",
        "description": (
            "Domain-neutral default for everyday assignments, questions, "
            "comparisons, plans, and one-off tasks."
        ),
        "pack_guidance": (
            "Complete the objective in whatever domain actually fits the "
            "task. Use only the stated constraints and expected output; do "
            "not assume a different domain, and do not infer requirements "
            "that were not given."
        ),
        "default_output_contract": {"format": "plain prose or structured text"},
        "focus_fields": ["objective", "intended_outcome", "audience"],
        "suggested_quality_criteria": [
            "Answer the actual question asked, not an adjacent one.",
            "Separate established facts from assumptions and guesses.",
            "State anything that is unknown instead of inventing it.",
        ],
        "suggested_evidence": [
            "Name the basis for factual claims (or mark them as unverified).",
        ],
        "suggested_checks": [
            "Restate the objective in one sentence before answering.",
            "List any information you would need but do not have.",
        ],
        "output_expectations": [
            "A direct response that satisfies the stated outcome.",
            "Length and tone matching the audience, unless specified.",
        ],
    },
    "design": {
        "id": "design",
        "version": 1,
        "label": "Graphic and visual design",
        "description": (
            "Visual identity, layout, brand, illustration, and presentation "
            "work with brand rules, deliverables, and review criteria."
        ),
        "pack_guidance": (
            "Treat this as a design assignment. Respect brand rules, asset "
            "constraints, and the stated audience; propose directions rather "
            "than silently inventing brand facts."
        ),
        "default_output_contract": {
            "format": "direction summary plus deliverable specifications",
        },
        "focus_fields": [
            "audience",
            "deliverables",
            "constraints",
            "quality_criteria",
        ],
        "suggested_quality_criteria": [
            "Brand rules and stated constraints are followed or explicitly challenged.",
            "Hierarchy and legibility hold at the sizes the audience will see.",
            "Every deliverable is specified precisely enough to be produced.",
        ],
        "suggested_evidence": [
            "Reference the brand rules, assets, or specifications relied on.",
            "Describe verification steps (contrast, legibility, export sizes).",
        ],
        "suggested_checks": [
            "Check contrast and legibility at the sizes the audience will see.",
            "Confirm every named deliverable has a usable specification.",
        ],
        "output_expectations": [
            "A rationale, the proposed direction, and per-deliverable specs.",
            "Known unknowns (assets, fonts, brand rules) stated explicitly.",
        ],
    },
    "research": {
        "id": "research",
        "version": 1,
        "label": "Research and evidence review",
        "description": (
            "Questions answered from evidence with source requirements, "
            "claim support, and explicit uncertainty."
        ),
        "pack_guidance": (
            "Treat this as a research task. Tie each factual claim to a "
            "source or mark it as unsupported; keep uncertainty explicit and "
            "never fill a source gap with plausible-sounding assertions."
        ),
        "default_output_contract": {
            "format": "findings with per-claim source references",
        },
        "focus_fields": [
            "objective",
            "evidence_requirements",
            "uncertainties",
            "audience",
        ],
        "suggested_quality_criteria": [
            "Every factual claim is supported by a cited source or labelled unverified.",
            "Contradicting evidence is reported, not averaged away.",
            "Uncertainty and gaps are stated rather than resolved by assertion.",
        ],
        "suggested_evidence": [
            "Record source identity, retrieval date, and why the source is credible.",
            "Note recency limits for time-sensitive claims.",
        ],
        "suggested_checks": [
            "Attach a source or an 'unverified' label to every factual claim.",
            "List contradicting evidence instead of averaging it away.",
        ],
        "output_expectations": [
            "Findings separated from interpretation.",
            "An explicit list of open questions and unresolved conflicts.",
        ],
    },
    "writing": {
        "id": "writing",
        "version": 1,
        "label": "Writing and editorial work",
        "description": (
            "Articles, explainers, and other prose defined by purpose, "
            "audience, length, tone, and factual requirements."
        ),
        "pack_guidance": (
            "Treat this as a writing assignment. Match purpose, audience, "
            "length, and tone; keep factual claims supportable and do not "
            "invent quotes, sources, or statistics."
        ),
        "default_output_contract": {
            "format": "finished prose with a title and clear structure",
        },
        "focus_fields": [
            "audience",
            "intended_outcome",
            "output_contract",
            "requirements",
        ],
        "suggested_quality_criteria": [
            "Structure and length match the assignment and audience.",
            "Tone and register stay consistent throughout.",
            "Factual claims are supportable; no invented sources or quotes.",
        ],
        "suggested_evidence": [
            "List the references behind factual claims.",
        ],
        "suggested_checks": [
            "Re-read for tone and register consistency against the audience.",
            "Confirm no quote, source, or statistic was invented.",
        ],
        "output_expectations": [
            "Ready-to-publish prose unless drafting was requested.",
            "Any deliberate omissions or uncertain claims noted.",
        ],
    },
    "business": {
        "id": "business",
        "version": 1,
        "label": "Business and decision analysis",
        "description": (
            "Decision problems with assumptions, calculations, options, and "
            "risk criteria."
        ),
        "pack_guidance": (
            "Treat this as a business decision task. Make assumptions "
            "explicit and traceable, show calculations so they can be "
            "reproduced, and distinguish recommendation from judgment."
        ),
        "default_output_contract": {
            "format": "recommendation with assumptions, calculations, and risks",
        },
        "focus_fields": [
            "objective",
            "assumptions",
            "risks",
            "acceptance_criteria",
        ],
        "suggested_quality_criteria": [
            "Assumptions are stated and traceable to inputs.",
            "Arithmetic and units are reproducible from the stated inputs.",
            "Risks and sensitivity to key assumptions are covered.",
        ],
        "suggested_evidence": [
            "Reference the input data and its date.",
            "Show the calculation path rather than only its result.",
        ],
        "suggested_checks": [
            "Reproduce every calculation from the stated inputs.",
            "Name the decision each key assumption would change.",
        ],
        "output_expectations": [
            "A recommendation, the evidence behind it, and its limits.",
            "Options compared against explicit decision criteria.",
        ],
    },
    "software": {
        "id": "software",
        "version": 1,
        "label": "Software development (optional)",
        "description": (
            "The existing software-development workflow: plans, tasks, "
            "verification evidence, and independent review stay available "
            "and unchanged."
        ),
        "pack_guidance": (
            "This is the optional software-development profile. Use "
            "repository evidence, tests, and verification records only where "
            "the objective and selected context make them relevant."
        ),
        "default_output_contract": {
            "format": "change description with verification evidence",
        },
        "focus_fields": [
            "objective",
            "requirements",
            "constraints",
            "acceptance_criteria",
            "evidence_requirements",
        ],
        "suggested_quality_criteria": [
            "Acceptance criteria are covered by verification evidence.",
            "Existing behaviour and regressions are considered explicitly.",
            "Known limitations of the verification are stated.",
        ],
        "suggested_evidence": [
            "Name the checks that were run and what they covered.",
            "State what was not verified.",
        ],
        "suggested_checks": [
            "Map each acceptance criterion to a check that was actually run.",
            "State what the verification did not cover.",
        ],
        "output_expectations": [
            "What changed, why, and how it was verified.",
        ],
    },
}

if tuple(PROFILES) != (
    "general", "design", "research", "writing", "business", "software",
):  # pragma: no cover - structural guard
    raise RuntimeError("profile registry order or membership changed")

PROFILE_IDS: tuple[str, ...] = tuple(PROFILES)


def _check_profile_id(profile_id: object) -> str:
    if not isinstance(profile_id, str) or profile_id not in PROFILES:
        raise ValidationError(
            "profile must be one of: " + ", ".join(PROFILE_IDS)
        )
    return profile_id


def get_profile(profile_id: str = DEFAULT_PROFILE) -> dict:
    """Return a validated, read-only view of one profile (never a copy of
    mutable global state: callers receive a shallow copy of the entry)."""
    profile = PROFILES[_check_profile_id(profile_id)]
    return {
        key: list(value) if isinstance(value, list) else
        dict(value) if isinstance(value, dict) else value
        for key, value in profile.items()
    }


def list_profiles() -> list[dict]:
    """All built-in profiles in registry order, for discovery/JSON output."""
    return [get_profile(profile_id) for profile_id in PROFILE_IDS]


def profile_versions() -> dict[str, int]:
    return {profile_id: PROFILES[profile_id]["version"] for profile_id in PROFILE_IDS}


def profile_suggestions(profile_id: str) -> dict:
    """Suggestion-only view of a profile, bounded for safe rendering.

    Consumers must present these as suggestions, never as operator
    requirements, and never use them to gate approval or completion.
    """
    profile = PROFILES[_check_profile_id(profile_id)]
    bounded: dict[str, list[str]] = {}
    for key in (
        "suggested_quality_criteria",
        "suggested_evidence",
        "suggested_checks",
        "output_expectations",
        "focus_fields",
    ):
        values = profile[key][:MAX_SUGGESTIONS]
        bounded[key] = [
            value if len(value.encode("utf-8")) <= MAX_SUGGESTION_BYTES
            else value.encode("utf-8")[:MAX_SUGGESTION_BYTES].decode(
                "utf-8", "ignore"
            )
            for value in values
        ]
    return {
        "profile": profile["id"],
        "profile_version": profile["version"],
        "note": (
            "Suggestions only. They are not operator requirements and never "
            "gate approval or completion."
        ),
        **bounded,
    }


def _validate_registry() -> None:
    """Structural self-check: every entry is well-formed and bounded."""
    for profile_id, profile in PROFILES.items():
        missing = set(_PROFILE_FIELDS) - set(profile)
        extra = set(profile) - set(_PROFILE_FIELDS)
        if missing or extra:
            raise RuntimeError(
                f"profile {profile_id!r} fields mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        if profile["id"] != profile_id:
            raise RuntimeError(f"profile key {profile_id!r} disagrees with its id")
        if not isinstance(profile["version"], int) or profile["version"] < 1:
            raise RuntimeError(f"profile {profile_id!r} has an invalid version")
        for key in ("label", "description", "pack_guidance"):
            if not isinstance(profile[key], str) or not profile[key].strip():
                raise RuntimeError(f"profile {profile_id!r}: {key} must be text")
        for key in (
            "suggested_quality_criteria",
            "suggested_evidence",
            "output_expectations",
            "focus_fields",
        ):
            values = profile[key]
            if not isinstance(values, list) or not values:
                raise RuntimeError(f"profile {profile_id!r}: {key} must be a list")
            if len(values) > MAX_SUGGESTIONS:
                raise RuntimeError(f"profile {profile_id!r}: {key} is too long")
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(
                        f"profile {profile_id!r}: {key} entries must be text"
                    )
        if not isinstance(profile["default_output_contract"], dict):
            raise RuntimeError(
                f"profile {profile_id!r}: default_output_contract must be a dict"
            )


_validate_registry()


__all__ = [
    "DEFAULT_PROFILE",
    "MAX_SUGGESTIONS",
    "PROFILES",
    "PROFILE_IDS",
    "PROFILE_SCHEMA_VERSION",
    "get_profile",
    "list_profiles",
    "profile_suggestions",
    "profile_versions",
]
