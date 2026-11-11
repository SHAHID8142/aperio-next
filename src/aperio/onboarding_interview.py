"""Adaptive project interview (interview-registry v1, deterministic core).

One primary question per turn. The operator experiences a conversation;
the engine is a deterministic, model-free state machine over:

- a **question registry**: every question has a stable id, a coverage
  category, plain-language prompt, the reason it is asked, concise
  suggestions, and — where a question only matters for some projects — a
  declarative ``when`` condition over previously recorded answers;
- **domain profiles**: each profile (web_app, desktop_app, website, ...)
  selects which questions are *required* and which are *recommended* for
  that kind of project. Profiles are data: registering a new profile
  never changes the universal core, and custom profiles can be loaded
  from a validated JSON file.

Interaction contract (implemented, not aspirational):

- :func:`next_question` returns **at most one** question per call.
- Answered questions are never repeated; irrelevant questions (condition
  fails) are never asked.
- Every rendered question explicitly allows the answers "Not sure yet"
  and "Decide later"; those are recorded as ``unsure``/``deferred`` with
  an appropriate severity, never guessed around.
- Deferred answers do not satisfy conditions that need a concrete value:
  dependent questions stay unasked rather than being asked on a guess.
- Revising an answer keeps the full history in the session (the session
  layer owns that record); a confirmed revision clears the pending
  unresolved item the deferral created.
- AI-generated interpretations are NEVER stored as operator decisions by
  this module: only answers delivered through :func:`apply_answer` (which
  the host passes verbatim from the operator) become recorded state, and
  they are stored as ``confirmed`` only when the operator actually chose
  them.

The question registry and profiles are honest, static content: they
describe what to ask, never what a project *is*. Nothing here calls a
model API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from aperio.artifacts import publish_text_atomic
from aperio.errors import ValidationError
from aperio.onboarding_session import (
    KIND_CONFIRMED,
    KIND_DEFERRED,
    KIND_UNSURE,
    SEVERITY_CRITICAL,
    SEVERITY_OPTIONAL,
    SEVERITY_PENDING_DECISION,
    SEVERITY_REQUIRED_BEFORE_ACTION,
    add_unresolved,
    record_answer,
    resolve_unresolved,
)

INTERVIEW_REGISTRY_FORMAT = "aperio-onboarding-interview-registry"
INTERVIEW_REGISTRY_VERSION = 1

# Coverage categories (Rule 6 of the Step 01 directive).
CAT_FOUNDATIONS = "foundations"
CAT_PRODUCT = "product"
CAT_DESIGN = "design"
CAT_TECHNICAL = "technical"
CAT_OPERATIONS = "operations"
CAT_BUSINESS = "business"
CAT_QUALITY = "quality"
CAT_META = "meta"

COVERAGE_CATEGORIES = (
    CAT_META,
    CAT_FOUNDATIONS,
    CAT_PRODUCT,
    CAT_DESIGN,
    CAT_TECHNICAL,
    CAT_OPERATIONS,
    CAT_BUSINESS,
    CAT_QUALITY,
)

# Standard operator opt-outs, matched case-insensitively after stripping.
UNSURE_PHRASES = ("not sure", "not sure yet", "unsure", "no idea", "i don't know", "idk", "don't know")
DEFER_PHRASES = ("decide later", "later", "defer", "skip for now", "decide later.")

MAX_PROFILE_QUESTIONS = 60


@dataclass(frozen=True)
class Question:
    """One interview question. Registry content is data, not authority."""

    id: str
    category: str
    prompt: str
    purpose: str
    suggestions: tuple[str, ...] = ()
    when: dict | None = None  # declarative condition over recorded answers
    severity_when_unanswered: str = SEVERITY_REQUIRED_BEFORE_ACTION

    def render(self) -> dict:
        """Machine-readable rendering for the host protocol (one question)."""
        return {
            "id": self.id,
            "category": self.category,
            "prompt": self.prompt,
            "purpose": self.purpose,
            "suggestions": list(self.suggestions),
            "allows_unsure": True,
            "allows_defer": True,
        }


@dataclass(frozen=True)
class InterviewProfile:
    """Which questions a project category needs, and how urgently."""

    profile_id: str
    description: str
    required: tuple[str, ...]
    recommended: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "description": self.description,
            "required": list(self.required),
            "recommended": list(self.recommended),
        }


def _q(id: str, category: str, prompt: str, purpose: str, suggestions=(), when=None,
       severity=SEVERITY_REQUIRED_BEFORE_ACTION) -> Question:
    """Compact registry constructor (suggestions must be short phrases)."""
    return Question(
        id=id,
        category=category,
        prompt=prompt,
        purpose=purpose,
        suggestions=tuple(suggestions),
        when=when,
        severity_when_unanswered=severity,
    )


# Canonical mapping between profile ids and the exact suggestion labels the
# registry's category question offers. Conditions must match either form:
# operators answer with the label, programmatic flows may use the id.
_CATEGORY_LABELS = {
    "web_app": "web application",
    "website": "website",
    "desktop_app": "desktop application",
    "mobile_app": "mobile app",
    "saas": "saas product",
    "api": "api / backend system",
    "automation": "automation project",
    "ai_app": "ai-assisted application",
    "design": "design project",
    "research": "research",
    "writing": "writing",
    "business_workflow": "business workflow",
}


def _in_category(*values: str) -> dict:
    """Condition: project.category equals one of the given profile ids,
    accepting both the profile id and its human-readable suggestion label."""
    expanded: list[str] = []
    for value in values:
        expanded.append(value)
        label = _CATEGORY_LABELS.get(value)
        if label and label not in expanded:
            expanded.append(label)
    return {"answer_in": {"question_id": "project.category", "values": expanded}}


def _answer_is(question_id: str, *values: str) -> dict:
    return {"answer_in": {"question_id": question_id, "values": list(values)}}


def _answered(question_id: str) -> dict:
    return {"answer_given": question_id}


def _any(*conditions: dict) -> dict:
    return {"any": list(conditions)}


def _all(*conditions: dict) -> dict:
    return {"all": list(conditions)}


# ---------------------------------------------------------------------------
# The question registry. Adding a question is additive; changing an existing
# question's id is a breaking contract change and requires a version bump.
# ---------------------------------------------------------------------------

QUESTIONS: tuple[Question, ...] = (
    # META — drives adaptive selection.
    _q(
        "project.category", CAT_META,
        "Which of these best describes this project?",
        "This decides which follow-up questions actually matter for your project.",
        (
            "Web application", "Website", "Desktop application", "Mobile app",
            "SaaS product", "API / backend system", "Automation project",
            "AI-assisted application", "Design project", "Research",
            "Writing", "Business workflow", "Something else / general",
        ),
        severity=SEVERITY_CRITICAL,
    ),
    # FOUNDATIONS
    _q(
        "foundation.goal", CAT_FOUNDATIONS,
        "What would you like to build or accomplish?",
        "The whole onboarding, plan, and future work hangs off this one sentence.",
        (
            "A small internal tool", "A client project", "A personal project",
            "Fixing or improving something that exists",
        ),
        severity=SEVERITY_CRITICAL,
    ),
    _q(
        "foundation.target_users", CAT_FOUNDATIONS,
        "Who is this for? Who will actually use it (or read it, or rely on it)?",
        "Users decide the requirements, not the other way around.",
        ("Just me", "My team", "Paying customers", "A specific client", "The public"),
    ),
    _q(
        "foundation.success", CAT_FOUNDATIONS,
        "How will you know this project succeeded? What does 'done well' look like?",
        "Success criteria become the acceptance checks later.",
        ("It saves time on a task", "Someone approves it", "It works without errors", "Revenue / signups"),
    ),
    _q(
        "foundation.scope_exclusions", CAT_FOUNDATIONS,
        "Anything you deliberately want OUT of scope for now?",
        "Exclusions prevent scope creep and keep the plan honest.",
        ("No mobile version yet", "No payments", "No admin panel", "Nothing excluded yet"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "foundation.stakeholders", CAT_FOUNDATIONS,
        "Anyone else involved or affected — clients, teammates, approvers?",
        "Stakeholders shape review and approval steps.",
        ("Solo project", "One client", "A small team", "Multiple approvers"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # PRODUCT
    _q(
        "product.core_features", CAT_PRODUCT,
        "What are the 3-5 most important things it must do?",
        "Core features drive the plan; everything else is a follow-up.",
        ("List them in a sentence each", "Not sure — help me figure it out"),
    ),
    _q(
        "product.user_journey", CAT_PRODUCT,
        "Walk through how a typical user gets value, step by step.",
        "The journey reveals missing features and design needs.",
        ("Example: open app, add task, get reminder", "Not sure yet"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "product.integrations", CAT_PRODUCT,
        "Does it need to connect to other tools or services?",
        "Integrations add dependencies, permissions, and failure modes.",
        ("No integrations", "Email", "Calendar", "Payment provider", "Other APIs"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "saas", "api", "automation", "ai_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "product.accounts", CAT_PRODUCT,
        "Do users need accounts or sign-in?",
        "Sign-in brings auth, privacy, and security requirements.",
        ("No accounts — local only", "Yes, accounts needed", "Single admin login"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "saas", "api"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # DESIGN
    _q(
        "design.ui_expectations", CAT_DESIGN,
        "Any look-and-feel expectations — simple and clean, or something specific?",
        "Design expectations steer implementation choices early.",
        ("Simple and functional", "Modern / polished", "Must match a brand"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "website", "saas", "design"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "design.references", CAT_DESIGN,
        "Any sites or apps whose style you'd like it to resemble?",
        "Concrete references are easier to follow than adjectives.",
        ("No strong preference", "I have links to share"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "website", "saas", "design"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "design.accessibility", CAT_DESIGN,
        "Any accessibility needs — screen readers, keyboard-only use, contrast?",
        "Accessibility is far cheaper to plan for than to retrofit.",
        ("Standard accessibility", "Strict requirements (e.g. WCAG)", "Not sure what applies"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "website", "saas"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "design.responsive", CAT_DESIGN,
        "Must it work well on phones as well as desktop screens?",
        "Responsive behavior changes layout and testing scope.",
        ("Yes, mobile matters", "Desktop only", "Mobile only"),
        when=_in_category("web_app", "website", "mobile_app", "saas"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "design.localization", CAT_DESIGN,
        "Will it need to support more than one language?",
        "Localization affects text handling and layout from day one.",
        ("One language only", "Two or more languages", "Later — design for it now"),
        when=_in_category("web_app", "desktop_app", "mobile_app", "website", "saas"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # TECHNICAL
    _q(
        "technical.platforms", CAT_TECHNICAL,
        "Where must it run? (operating systems, browsers, or devices)",
        "Platforms decide testing, packaging, and compatibility work.",
        ("macOS only", "Windows + macOS", "All modern browsers", "iOS + Android", "Linux servers"),
    ),
    _q(
        "technical.stack", CAT_TECHNICAL,
        "Any technology preferences or constraints?",
        "Constraints now prevent rewrites later; 'no preference' is a fine answer.",
        ("No preference", "Python", "JavaScript / TypeScript", "Must reuse existing stack"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "technical.storage", CAT_TECHNICAL,
        "Where does its data live — local files, a database, or a cloud service?",
        "Storage choice shapes backups, privacy, and hosting.",
        ("Local files", "A database", "Cloud service", "No data stored"),
        when=_any(
            _in_category("web_app", "desktop_app", "mobile_app", "saas", "api", "automation"),
            _answered("product.accounts"),
        ),
    ),
    _q(
        "technical.auth", CAT_TECHNICAL,
        "How should sign-in work, once accounts exist?",
        "Auth method carries security and privacy implications.",
        ("Email + password", "Single sign-on (SSO)", "Magic links", "Not sure yet"),
        when=_answer_is("product.accounts", "yes, accounts needed", "single admin login"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "technical.privacy", CAT_TECHNICAL,
        "What personal or private data will it handle, if any?",
        "Private data raises the bar for storage, sharing, and deletion.",
        ("No personal data", "Names/emails only", "Sensitive personal data"),
        when=_any(
            _answer_is("product.accounts", "yes, accounts needed"),
            _in_category("saas", "api", "ai_app"),
        ),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "technical.performance", CAT_TECHNICAL,
        "Any speed or scale expectations worth noting?",
        "Performance targets affect architecture and testing.",
        ("No special requirements", "Sub-second responses", "Thousands of users"),
        when=_in_category("web_app", "saas", "api", "mobile_app", "desktop_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "technical.offline", CAT_TECHNICAL,
        "Must it keep working without an internet connection?",
        "Offline support is an architectural decision, not a feature.",
        ("Must work offline", "Online only", "Nice to have"),
        when=_in_category("desktop_app", "mobile_app", "website"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "technical.ai_data", CAT_TECHNICAL,
        "What data is okay to send to AI models, if the project uses them?",
        "AI data boundaries are a privacy and compliance decision.",
        ("No AI usage", "Public data only", "Private data needs care"),
        when=_in_category("ai_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # OPERATIONS
    _q(
        "operations.hosting", CAT_OPERATIONS,
        "Where will it be hosted or deployed?",
        "Hosting choice affects cost, privacy, and deployment work.",
        ("My own machine/server", "A cloud provider", "Static hosting", "Not sure yet"),
        when=_in_category("web_app", "website", "saas", "api"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "operations.distribution", CAT_OPERATIONS,
        "How will it reach its users — installer, app store, download?",
        "Distribution determines packaging and update mechanics.",
        ("Direct download", "App store", "Internal use only", "Not sure yet"),
        when=_in_category("desktop_app", "mobile_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "operations.updates", CAT_OPERATIONS,
        "How will updates be delivered after launch?",
        "Update mechanics affect versioning and user trust.",
        ("Manual updates", "Automatic updates", "Not applicable"),
        when=_in_category("desktop_app", "mobile_app", "web_app", "saas"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "operations.monitoring", CAT_OPERATIONS,
        "How will you know it's healthy once it's running?",
        "Monitoring expectations shape operations work.",
        ("Manual checks", "Error alerts", "Full observability", "Not applicable"),
        when=_in_category("web_app", "saas", "api", "automation"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "operations.backup", CAT_OPERATIONS,
        "Any backup or recovery expectations for its data?",
        "Backups protect against the failure nobody plans for.",
        ("Manual backups are fine", "Automatic backups needed", "No data to back up"),
        when=_answered("technical.storage"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "operations.maintenance", CAT_OPERATIONS,
        "Who maintains it after launch, and how often?",
        "Maintenance reality-checks scope and documentation needs.",
        ("Me, casually", "A team", "Client's team", "Hand-off expected"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # BUSINESS
    _q(
        "business.budget", CAT_BUSINESS,
        "Any budget or cost constraints (hosting, services, licenses)?",
        "Costs gate hosting and tooling choices.",
        ("Minimal / free tier only", "Small monthly budget", "Project budget exists"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "business.schedule", CAT_BUSINESS,
        "Any deadline or target dates?",
        "Schedule shapes how much lands in the first version.",
        ("No fixed deadline", "This month", "This quarter", "Fixed client date"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "business.licensing", CAT_BUSINESS,
        "Any licensing or ownership terms to respect?",
        "Licensing affects what can ship and how.",
        ("Personal / private use", "Open source", "Commercial product", "Client owns it"),
        when=_in_category("desktop_app", "web_app", "mobile_app", "saas", "api", "design", "writing"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "business.monetization", CAT_BUSINESS,
        "Should it make money — and if so, how?",
        "Monetization changes features (payments, plans) and legal needs.",
        ("Not for money", "Subscriptions", "One-time purchase", "Ads", "Not sure yet"),
        when=_in_category("website", "saas", "mobile_app", "web_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "business.seo", CAT_BUSINESS,
        "What are the SEO expectations for the site?",
        "SEO changes content structure, metadata, and delivery.",
        ("Basic SEO is enough", "Strong SEO is a requirement", "Not applicable"),
        when=_in_category("website"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "business.support", CAT_BUSINESS,
        "Who supports users when something breaks?",
        "Support reality-checks reliability promises.",
        ("Just me", "A support team", "Self-service docs", "Not applicable"),
        when=_in_category("saas", "website", "web_app", "mobile_app"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # QUALITY
    _q(
        "quality.testing", CAT_QUALITY,
        "How will you verify it actually works?",
        "Verification evidence is what separates 'ran' from 'done'.",
        ("Manual testing by me", "Automated tests", "Both", "Not sure yet"),
    ),
    _q(
        "quality.acceptance", CAT_QUALITY,
        "What's the minimum bar for 'good enough to ship'?",
        "An explicit bar prevents endless polishing and premature shipping.",
        ("It works end-to-end", "Client approves", "Tests pass", "Not sure yet"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "quality.risks", CAT_QUALITY,
        "Any known risks, worries, or open questions?",
        "Naming risks early keeps them visible instead of latent.",
        ("None I can think of", "Time", "Technical unknowns", "Cost"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    # DOMAIN-SPECIFIC
    _q(
        "research.question", CAT_FOUNDATIONS,
        "What question are you trying to answer with this research?",
        "A sharp question keeps evidence-gathering honest.",
        ("A factual question", "A comparison", "An open exploration"),
        when=_in_category("research"),
        severity=SEVERITY_CRITICAL,
    ),
    _q(
        "research.method", CAT_FOUNDATIONS,
        "How will you gather the evidence (sources, experiments, interviews)?",
        "Method determines how much the conclusions can be trusted.",
        ("Web sources", "Academic sources", "Interviews", "Experiments"),
        when=_in_category("research"),
    ),
    _q(
        "research.evidence", CAT_QUALITY,
        "What makes evidence acceptable here — and is citation required?",
        "Evidence standards are the quality bar for research work.",
        ("Informal sources fine", "Citations required", "Peer-reviewed only"),
        when=_in_category("research"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "research.deliverable", CAT_PRODUCT,
        "What will the final output be (report, summary, dataset)?",
        "The deliverable defines when the research is done.",
        ("Written report", "Slide deck", "Structured dataset", "Decision memo"),
        when=_in_category("research"),
    ),
    _q(
        "design.deliverable", CAT_PRODUCT,
        "What exactly will you hand over (mockups, assets, a spec)?",
        "The deliverable defines 'done' for design work.",
        ("Mockups / wireframes", "Final assets", "A written spec", "Working prototype"),
        when=_in_category("design"),
    ),
    _q(
        "writing.deliverable", CAT_PRODUCT,
        "What form does the writing take (article, book, docs, copy)?",
        "Form determines structure, length, and review needs.",
        ("Article / post", "Documentation", "Book / long form", "Marketing copy"),
        when=_in_category("writing"),
    ),
    _q(
        "writing.length", CAT_PRODUCT,
        "Any expected length or depth?",
        "Length expectations prevent mismatched drafts.",
        ("Short (<1000 words)", "Medium", "Long form", "No fixed length"),
        when=_in_category("writing"),
        severity=SEVERITY_PENDING_DECISION,
    ),
    _q(
        "workflow.steps", CAT_PRODUCT,
        "Roughly what are the steps of the workflow being organized?",
        "The steps become the backbone of the plan.",
        ("A short list is fine", "Not fully known yet"),
        when=_in_category("business_workflow", "automation"),
    ),
)

_REGISTRY_BY_ID: dict[str, Question] = {q.id: q for q in QUESTIONS}


def get_question(question_id: str) -> Question:
    """Look up a registry question by id.

    :raises ValidationError: for unknown ids (prevents arbitrary session
        pollution through the answer path).
    """
    question = _REGISTRY_BY_ID.get(question_id)
    if question is None:
        raise ValidationError(
            f"unknown question id {question_id!r}; this question is not part of "
            "the onboarding registry"
        )
    return question


def list_questions() -> tuple[Question, ...]:
    """All registry questions, in stable declaration order."""
    return QUESTIONS


# ---------------------------------------------------------------------------
# Domain profiles: data-only selection over the registry.
# ---------------------------------------------------------------------------

PROFILES: dict[str, InterviewProfile] = {}


def _register(profile: InterviewProfile) -> InterviewProfile:
    """Register a profile, validating that every question id exists."""
    if profile.profile_id in PROFILES:
        raise ValidationError(f"profile {profile.profile_id!r} is already registered")
    if not 1 <= len(profile.required) + len(profile.recommended) <= MAX_PROFILE_QUESTIONS:
        raise ValidationError(
            f"profile {profile.profile_id!r} must select 1-{MAX_PROFILE_QUESTIONS} questions"
        )
    for qid in (*profile.required, *profile.recommended):
        if qid not in _REGISTRY_BY_ID:
            raise ValidationError(
                f"profile {profile.profile_id!r} references unknown question {qid!r}"
            )
    PROFILES[profile.profile_id] = profile
    return profile


DEFAULT_PROFILE = "general"

_register(InterviewProfile(
    "general", "General knowledge-work project; minimal essential set.",
    required=("foundation.goal", "foundation.target_users", "foundation.success", "quality.testing"),
    recommended=("foundation.scope_exclusions", "product.core_features", "quality.risks", "business.schedule"),
))
_register(InterviewProfile(
    "web_app", "Web application.",
    required=("foundation.goal", "foundation.target_users", "product.core_features",
              "technical.platforms", "technical.storage", "quality.testing"),
    recommended=("foundation.success", "foundation.scope_exclusions", "product.integrations",
                 "product.accounts", "design.ui_expectations", "design.responsive",
                 "design.accessibility", "technical.stack", "technical.auth",
                 "technical.privacy", "technical.performance", "operations.hosting",
                 "operations.monitoring", "operations.updates", "quality.acceptance",
                 "quality.risks", "business.schedule"),
))
_register(InterviewProfile(
    "website", "Content website.",
    required=("foundation.goal", "foundation.target_users", "product.core_features",
              "quality.testing"),
    recommended=("foundation.success", "design.ui_expectations", "design.responsive",
                 "design.accessibility", "business.seo", "operations.hosting",
                 "operations.maintenance", "quality.acceptance", "business.monetization",
                 "business.schedule"),
))
_register(InterviewProfile(
    "desktop_app", "Desktop application.",
    required=("foundation.goal", "foundation.target_users", "product.core_features",
              "technical.platforms", "technical.storage", "quality.testing"),
    recommended=("foundation.success", "product.accounts", "design.ui_expectations",
                 "design.accessibility", "technical.offline", "technical.stack",
                 "operations.distribution", "operations.updates", "operations.backup",
                 "quality.acceptance", "quality.risks"),
))
_register(InterviewProfile(
    "mobile_app", "Mobile application.",
    required=("foundation.goal", "foundation.target_users", "product.core_features",
              "technical.platforms", "quality.testing"),
    recommended=("foundation.success", "product.accounts", "design.ui_expectations",
                 "design.accessibility", "technical.offline", "technical.storage",
                 "operations.distribution", "operations.updates", "quality.acceptance"),
))
_register(InterviewProfile(
    "saas", "SaaS product.",
    required=("foundation.goal", "foundation.target_users", "product.core_features",
              "technical.storage", "technical.auth", "operations.hosting", "quality.testing"),
    recommended=("foundation.success", "product.integrations", "technical.privacy",
                 "technical.performance", "operations.monitoring", "business.monetization",
                 "business.support", "quality.acceptance", "quality.risks"),
))
_register(InterviewProfile(
    "api", "API / backend system.",
    required=("foundation.goal", "product.core_features", "technical.storage",
              "technical.auth", "quality.testing"),
    recommended=("foundation.target_users", "product.integrations", "technical.performance",
                 "operations.hosting", "operations.monitoring", "quality.acceptance"),
))
_register(InterviewProfile(
    "automation", "Automation project.",
    required=("foundation.goal", "product.core_features", "quality.testing"),
    recommended=("product.integrations", "workflow.steps", "operations.monitoring",
                 "business.schedule", "quality.risks"),
))
_register(InterviewProfile(
    "ai_app", "AI-assisted application.",
    required=("foundation.goal", "product.core_features", "quality.testing"),
    recommended=("technical.ai_data", "technical.privacy", "product.integrations",
                 "product.accounts", "quality.acceptance", "quality.risks"),
))
_register(InterviewProfile(
    "design", "Design project.",
    required=("foundation.goal", "foundation.target_users", "design.deliverable"),
    recommended=("design.ui_expectations", "design.references", "design.accessibility",
                 "business.schedule", "business.licensing"),
))
_register(InterviewProfile(
    "research", "Research task.",
    required=("foundation.goal", "research.question", "research.method",
              "research.deliverable"),
    recommended=("research.evidence", "foundation.success", "business.schedule"),
))
_register(InterviewProfile(
    "writing", "Writing project.",
    required=("foundation.goal", "foundation.target_users", "writing.deliverable"),
    recommended=("writing.length", "foundation.success", "business.schedule"),
))
_register(InterviewProfile(
    "business_workflow", "Business workflow organization.",
    required=("foundation.goal", "foundation.target_users", "workflow.steps"),
    recommended=("foundation.success", "business.schedule", "operations.maintenance"),
))


def get_profile(profile_id: str = DEFAULT_PROFILE) -> InterviewProfile:
    """Look up a domain profile.

    :raises ValidationError: for unknown profile ids.
    """
    profile = PROFILES.get(profile_id)
    if profile is None:
        known = ", ".join(sorted(PROFILES))
        raise ValidationError(
            f"unknown interview profile {profile_id!r}; available profiles: {known}"
        )
    return profile


def list_profiles() -> tuple[InterviewProfile, ...]:
    """All registered profiles, sorted by id for stable output."""
    return tuple(PROFILES[key] for key in sorted(PROFILES))


def profile_for_category(category_answer: str) -> InterviewProfile:
    """Map the project-category answer to a profile.

    Free-form or unsure answers fall back to the general profile; the
    mapping is exact-match on the suggestion labels (case-insensitive).
    """
    mapping = {
        "web application": "web_app",
        "website": "website",
        "desktop application": "desktop_app",
        "mobile app": "mobile_app",
        "saas product": "saas",
        "api / backend system": "api",
        "api / backend": "api",
        "automation project": "automation",
        "ai-assisted application": "ai_app",
        "design project": "design",
        "research": "research",
        "writing": "writing",
        "business workflow": "business_workflow",
    }
    profile_id = mapping.get(category_answer.strip().casefold(), DEFAULT_PROFILE)
    return get_profile(profile_id)


def register_profile(profile: InterviewProfile) -> InterviewProfile:
    """Public extension point: register a custom domain profile.

    Validated exactly like the built-ins, so an extension cannot reference
    questions that do not exist or silently override an existing profile.
    """
    return _register(profile)


def load_profile_extension(path: str | Path) -> InterviewProfile:
    """Load and register a custom profile from a JSON file (bounded read).

    The file must be a JSON object with ``profile_id`` (string),
    ``description`` (string), and ``required``/``recommended`` (lists of
    known question ids). Anything else is refused.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read profile file {file_path}: {exc.strerror or exc}") from None
    if len(raw) > 64 * 1024:
        raise ValidationError(f"profile file {file_path} exceeds 64 KiB")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"profile file {file_path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValidationError(f"profile file {file_path} must contain a JSON object")
    profile_id = data.get("profile_id")
    description = data.get("description")
    required = data.get("required", [])
    recommended = data.get("recommended", [])
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValidationError(f"profile file {file_path} needs a non-empty 'profile_id'")
    if not isinstance(description, str):
        raise ValidationError(f"profile file {file_path} needs a 'description' string")
    if not isinstance(required, list) or not isinstance(recommended, list):
        raise ValidationError(f"profile file {file_path}: 'required'/'recommended' must be lists")
    if not all(isinstance(x, str) for x in (*required, *recommended)):
        raise ValidationError(f"profile file {file_path}: question ids must be strings")
    return register_profile(
        InterviewProfile(
            profile_id=profile_id.strip(),
            description=description,
            required=tuple(required),
            recommended=tuple(recommended),
        )
    )


# ---------------------------------------------------------------------------
# Condition evaluation (deterministic, fail-closed on unknown shapes).
# ---------------------------------------------------------------------------

def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def condition_passes(condition: dict | None, answers: dict) -> bool:
    """Evaluate a declarative condition against recorded answers.

    An answer counts only if it is present AND carries a concrete value:
    ``unsure`` and ``deferred`` answers never satisfy conditions that need
    a specific value (an operator's 'decide later' must not silently
    select a branch for them).
    """
    if condition is None:
        return True
    if not isinstance(condition, dict):
        raise ValidationError("interview condition must be a JSON object")

    if "all" in condition:
        return all(condition_passes(c, answers) for c in condition["all"])
    if "any" in condition:
        return any(condition_passes(c, answers) for c in condition["any"])
    if "not" in condition:
        return not condition_passes(condition["not"], answers)
    if "answer_given" in condition:
        qid = condition["answer_given"]
        entry = answers.get(qid)
        return isinstance(entry, dict) and bool(str(entry.get("answer", "")).strip())
    if "answer_in" in condition:
        spec = condition["answer_in"]
        entry = answers.get(spec.get("question_id"))
        if not isinstance(entry, dict) or entry.get("kind") != KIND_CONFIRMED:
            return False
        values = spec.get("values", [])
        return _normalize(str(entry.get("answer", ""))) in {_normalize(str(v)) for v in values}
    raise ValidationError(
        f"unsupported interview condition shape: {sorted(condition)!r}"
    )


# ---------------------------------------------------------------------------
# Selection engine: one question per turn.
# ---------------------------------------------------------------------------

def _answerable(session: dict, profile_id: str, extra_questions: tuple[Question, ...] = ()) -> list[Question]:
    """Active, not-yet-answered questions in registry order."""
    profile = get_profile(profile_id)
    selected_ids = [*profile.required, *profile.recommended]
    extras_by_id = {q.id: q for q in extra_questions}
    answers = session.get("answers", {})
    ordered: list[Question] = []
    seen: set[str] = set()
    # The category question is the adaptive driver: it always comes first
    # when unanswered, whatever profile was requested — the answer decides
    # which profile should actually drive the rest of the interview.
    meta = get_question("project.category")
    if meta.id not in answers:
        seen.add(meta.id)
        ordered.append(meta)
    for qid in selected_ids:
        if qid in seen:
            continue
        seen.add(qid)
        ordered.append(get_question(qid))
    for qid in sorted(extras_by_id):
        if qid not in seen:
            seen.add(qid)
            ordered.append(extras_by_id[qid])

    active = []
    for question in ordered:
        if question.id in answers:
            continue
        if condition_passes(question.when, answers):
            active.append(question)
    return active


def next_question(
    session: dict,
    profile_id: str = DEFAULT_PROFILE,
    extra_questions: tuple[Question, ...] = (),
) -> dict | None:
    """The single next question to ask, or ``None`` when coverage is met.

    Required questions are asked before recommended ones; within a tier,
    registry declaration order (stable) applies. This is the ONE question
    per turn contract: callers must render at most this one question.
    """
    active = _answerable(session, profile_id, extra_questions)
    if not active:
        return None
    # The category driver outranks the required-before-recommended rule:
    # it must be answered before any profile question makes sense.
    if active[0].id == "project.category":
        return active[0].render()
    profile = get_profile(profile_id)
    required_set = set(profile.required)
    for question in active:
        if question.id in required_set:
            return question.render()
    return active[0].render()


def interview_progress(
    session: dict,
    profile_id: str = DEFAULT_PROFILE,
    extra_questions: tuple[Question, ...] = (),
) -> dict:
    """Coverage report: what is answered, outstanding, and by category."""
    profile = get_profile(profile_id)
    answers = session.get("answers", {})
    extras_by_id = {q.id: q for q in extra_questions}

    def status_for(qid: str) -> str:
        if qid not in answers:
            return "outstanding"
        kind = answers[qid].get("kind")
        if kind == KIND_CONFIRMED:
            return "confirmed"
        return kind  # unsure / deferred

    required_status = {qid: status_for(qid) for qid in profile.required}
    recommended_status = {qid: status_for(qid) for qid in profile.recommended}
    # The category driver is tracked like a required item: an interview is
    # not complete while the question that adapts everything else is open.
    meta_status = status_for("project.category")
    if meta_status == "outstanding":
        required_status = {"project.category": meta_status, **required_status}
    for qid, question in extras_by_id.items():
        required_status.setdefault(qid, status_for(qid))

    coverage: dict[str, dict] = {}
    tracked = (*profile.required, *profile.recommended, *extras_by_id)
    if meta_status == "outstanding":
        tracked = ("project.category", *tracked)
    for qid in tracked:
        question = extras_by_id.get(qid) or get_question(qid)
        bucket = coverage.setdefault(question.category, {"total": 0, "confirmed": 0, "outstanding": 0})
        bucket["total"] += 1
        state = status_for(qid)
        if state == "confirmed":
            bucket["confirmed"] += 1
        elif state == "outstanding":
            bucket["outstanding"] += 1

    outstanding_required = [qid for qid, s in required_status.items() if s == "outstanding"]
    return {
        "profile": profile.to_dict(),
        "required_status": required_status,
        "recommended_status": recommended_status,
        "outstanding_required": outstanding_required,
        "coverage": coverage,
        "complete": not outstanding_required,
    }


# ---------------------------------------------------------------------------
# Answer application: operator-provided text becomes session state.
# ---------------------------------------------------------------------------

def _classify_answer(raw: str) -> str:
    folded = _normalize(raw)
    for phrase in UNSURE_PHRASES:
        if folded == phrase:
            return KIND_UNSURE
    for phrase in DEFER_PHRASES:
        if folded == phrase:
            return KIND_DEFERRED
    return KIND_CONFIRMED


def apply_answer(
    apdir: Path,
    question_id: str,
    raw_answer: str,
    *,
    extra_questions: tuple[Question, ...] = (),
) -> dict:
    """Record one operator answer and its consequences in the session.

    The raw answer is stored verbatim; its provenance kind is derived
    deterministically (exact match on the standard opt-out phrases).
    "Not sure yet" / "Decide later" create a pending unresolved item with
    the question's severity; a later confirmed revision clears it.
    """
    question = get_question(question_id)  # unknown ids are refused here
    if not isinstance(raw_answer, str):
        raise ValidationError("answer must be a string")
    stripped = raw_answer.strip()
    if not stripped:
        raise ValidationError("answer must not be empty")
    if len(stripped.encode("utf-8")) > 8 * 1024:
        raise ValidationError("answer exceeds the 8 KiB limit")

    kind = _classify_answer(stripped)
    record_answer(apdir, question_id, answer=stripped, kind=kind)

    if kind != KIND_CONFIRMED:
        severity = question.severity_when_unanswered
        add_unresolved(
            apdir,
            topic=f"question:{question_id}",
            severity=severity,
            reason=f"operator answered '{stripped}' ({kind}) for: {question.prompt[:120]}",
            question_id=question_id,
        )
    else:
        # A confirmed revision clears the deferral it supersedes, if any.
        try:
            resolve_unresolved(apdir, f"question:{question_id}")
        except ValidationError:
            pass
    return read_session_snapshot(apdir)


def read_session_snapshot(apdir: Path) -> dict:
    """Validated session snapshot (imported here to keep cli thin)."""
    from aperio.onboarding_session import read_session

    return read_session(apdir)


def revise_answer(apdir: Path, question_id: str, raw_answer: str) -> dict:
    """Revise a previous answer (same contract as apply_answer)."""
    # get_question validates the id is in the registry.
    return apply_answer(apdir, question_id, raw_answer)


def write_registry_markdown(path: str | Path, *, force: bool = False) -> None:
    """Human-readable registry + profile dump for review and docs parity."""
    lines = [
        "# Onboarding interview registry",
        "",
        f"Format: {INTERVIEW_REGISTRY_FORMAT} v{INTERVIEW_REGISTRY_VERSION}",
        "",
        "## Questions",
        "",
    ]
    for question in QUESTIONS:
        lines.append(f"### {question.id} ({question.category})")
        lines.append("")
        lines.append(f"{question.prompt}")
        lines.append("")
        lines.append(f"*Why asked:* {question.purpose}")
        if question.when is not None:
            lines.append(f"*Condition:* `{json.dumps(question.when, sort_keys=True)}`")
        lines.append(f"*Severity when unanswered:* {question.severity_when_unanswered}")
        if question.suggestions:
            lines.append("*Suggestions:* " + "; ".join(question.suggestions))
        lines.append("")
    lines.append("## Profiles")
    lines.append("")
    for profile in list_profiles():
        lines.append(f"### {profile.profile_id}")
        lines.append("")
        lines.append(profile.description)
        lines.append("")
        lines.append("- Required: " + ", ".join(profile.required))
        lines.append("- Recommended: " + ", ".join(profile.recommended))
        lines.append("")
    publish_text_atomic(Path(path), "\n".join(lines), force=force, what="interview registry dump")
