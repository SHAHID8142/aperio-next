"""Adaptive interview engine: one question per turn, profiles, conditions."""

from __future__ import annotations

import json

import pytest

from aperio.errors import ValidationError
from aperio.onboarding_interview import (
    DEFAULT_PROFILE,
    InterviewProfile,
    apply_answer,
    condition_passes,
    get_profile,
    get_question,
    interview_progress,
    list_profiles,
    load_profile_extension,
    next_question,
    profile_for_category,
    register_profile,
    revise_answer,
    write_registry_markdown,
)
from aperio.onboarding_session import create_session
from aperio.project import init_project


@pytest.fixture
def apdir(project_dir):
    init_project(str(project_dir))
    return project_dir / ".aperio"


def _seed(apdir, classification="empty_new"):
    """Create the onboarding session that apply_answer records into."""
    return create_session(
        apdir, workspace=str(apdir.parent), classification=classification
    )


def _answered(qid, answer, kind="confirmed"):
    return {"answer": answer, "kind": kind}


def test_first_question_is_the_category_driver(apdir):
    session = {"answers": {}}
    question = next_question(session)
    assert question["id"] == "project.category"
    assert question["allows_unsure"] is True
    assert question["allows_defer"] is True


def test_one_question_per_turn(apdir):
    session = {"answers": {}}
    question = next_question(session, "web_app")
    assert isinstance(question, dict)
    # The contract: exactly one question object, not a list.
    assert "prompt" in question and "id" in question


def test_required_questions_come_before_recommended(apdir):
    session = {"answers": {"project.category": _answered("project.category", "Web application")}}
    ids = []
    # Drive the whole interview by auto-answering everything returned.
    for _ in range(40):
        question = next_question(session, "web_app")
        if question is None:
            break
        ids.append(question["id"])
        answer = "Yes, accounts needed" if question["id"] == "product.accounts" else "some answer"
        session["answers"][question["id"]] = _answered(question["id"], answer)
    profile = get_profile("web_app")
    # Every required question must appear before the first recommended one.
    first_recommended = next(
        (i for i, qid in enumerate(ids) if qid in set(profile.recommended)), len(ids)
    )
    for qid in profile.required:
        assert ids.index(qid) < first_recommended, (
            f"{qid} asked after a recommended question"
        )


def test_answered_questions_never_repeated(apdir):
    session = {"answers": {"foundation.goal": _answered("foundation.goal", "A tool")}}
    seen = []
    for _ in range(30):
        question = next_question(session, "general")
        if question is None:
            break
        seen.append(question["id"])
        session["answers"][question["id"]] = _answered(question["id"], "answer")
    assert "foundation.goal" not in seen
    assert len(seen) == len(set(seen))


def test_interview_completes_to_none(apdir):
    session = {"answers": {}}
    for _ in range(40):
        question = next_question(session, "general")
        if question is None:
            break
        session["answers"][question["id"]] = _answered(question["id"], "x")
    assert next_question(session, "general") is None
    progress = interview_progress(session, "general")
    assert progress["complete"] is True


def test_irrelevant_questions_skipped(apdir):
    session = {"answers": {}}
    seen = set()
    for _ in range(60):
        question = next_question(session, "research")
        if question is None:
            break
        seen.add(question["id"])
        answer = "Research" if question["id"] == "project.category" else "x"
        session["answers"][question["id"]] = _answered(question["id"], answer)
    # Research profile must never ask UI-responsive or SEO questions.
    assert "design.responsive" not in seen
    assert "business.seo" not in seen
    assert "research.question" in seen


def test_conditions_select_followups(apdir):
    # Declaring accounts needed makes technical.auth relevant.
    answers = {
        "product.accounts": _answered("product.accounts", "Yes, accounts needed"),
        "project.category": _answered("project.category", "Web application"),
    }
    assert condition_passes(get_question("technical.auth").when, answers)
    # Without accounts, it is irrelevant.
    assert not condition_passes(get_question("technical.auth").when, {})


def test_deferred_answers_do_not_select_branches(apdir):
    answers = {
        "product.accounts": _answered("product.accounts", "Decide later", kind="deferred"),
    }
    assert not condition_passes(get_question("technical.auth").when, answers)


def test_unknown_condition_shape_fails_closed(apdir):
    with pytest.raises(ValidationError):
        condition_passes({"exploded": True}, {})


def test_category_maps_to_profile(apdir):
    assert profile_for_category("Web application").profile_id == "web_app"
    assert profile_for_category("Research").profile_id == "research"
    assert profile_for_category("something entirely custom").profile_id == DEFAULT_PROFILE


def test_category_answer_drives_adaptation(apdir):
    session = {"answers": {}}
    question = next_question(session, "general")
    assert question["id"] == "project.category"
    # Answering "Research" makes research questions relevant.
    session["answers"]["project.category"] = _answered("project.category", "Research")
    seen = set()
    for _ in range(60):
        question = next_question(session, "research")
        if question is None:
            break
        seen.add(question["id"])
        session["answers"][question["id"]] = _answered(question["id"], "x")
    assert "research.method" in seen
    # Desktop-only questions stay out of a research interview.
    assert "operations.distribution" not in seen


def test_unsure_answer_records_unresolved(apdir):
    _seed(apdir)
    apply_answer(apdir, "foundation.target_users", "Not sure yet")
    from aperio.onboarding_session import read_session

    data = read_session(apdir)
    assert data["answers"]["foundation.target_users"]["kind"] == "unsure"
    topics = [u["topic"] for u in data["unresolved"]]
    assert "question:foundation.target_users" in topics


def test_deferred_answer_records_pending_decision(apdir):
    _seed(apdir)
    apply_answer(apdir, "foundation.scope_exclusions", "Decide later")
    from aperio.onboarding_session import SEVERITY_PENDING_DECISION, read_session

    data = read_session(apdir)
    entry = next(u for u in data["unresolved"] if u["topic"] == "question:foundation.scope_exclusions")
    assert entry["severity"] == SEVERITY_PENDING_DECISION


def test_critical_question_deferral_is_critical(apdir):
    _seed(apdir)
    apply_answer(apdir, "foundation.goal", "not sure yet")
    from aperio.onboarding_session import SEVERITY_CRITICAL, read_session

    data = read_session(apdir)
    entry = next(u for u in data["unresolved"] if u["topic"] == "question:foundation.goal")
    assert entry["severity"] == SEVERITY_CRITICAL


def test_revision_clears_unresolved(apdir):
    _seed(apdir)
    apply_answer(apdir, "foundation.target_users", "decide later")
    apply_answer(apdir, "foundation.target_users", "My team")
    from aperio.onboarding_session import read_session

    data = read_session(apdir)
    entry = next(u for u in data["unresolved"] if u["topic"] == "question:foundation.target_users")
    assert entry["resolved_at"] is not None
    assert data["answers"]["foundation.target_users"]["kind"] == "confirmed"
    # History preserved.
    assert data["answer_history"]["foundation.target_users"][0]["kind"] == "deferred"


def test_revise_answer_alias_records_revision(apdir):
    _seed(apdir)
    apply_answer(apdir, "foundation.goal", "First idea")
    revise_answer(apdir, "foundation.goal", "Second, better idea")
    from aperio.onboarding_session import read_session

    data = read_session(apdir)
    assert data["answers"]["foundation.goal"]["revision"] == 2


def test_unknown_question_id_refused(apdir):
    _seed(apdir)
    with pytest.raises(ValidationError, match="unknown question id"):
        apply_answer(apdir, "totally.made.up", "yes")


def test_empty_answer_refused(apdir):
    _seed(apdir)
    with pytest.raises(ValidationError, match="must not be empty"):
        apply_answer(apdir, "foundation.goal", "   ")


def test_unknown_profile_refused(apdir):
    with pytest.raises(ValidationError, match="unknown interview profile"):
        next_question({"answers": {}}, "quantum_computing")


def test_profile_registration_validates_questions():
    with pytest.raises(ValidationError, match="unknown question"):
        register_profile(
            InterviewProfile("broken", "references nothing", required=("no.such.q",), recommended=())
        )
    with pytest.raises(ValidationError, match="already registered"):
        register_profile(
            InterviewProfile("general", "duplicate", required=("foundation.goal",), recommended=())
        )


def test_custom_profile_load_and_use(tmp_path, apdir):
    path = tmp_path / "custom-profile.json"
    path.write_text(json.dumps({
        "profile_id": "field_research",
        "description": "Custom example profile",
        "required": ["foundation.goal", "research.method"],
        "recommended": ["foundation.success"],
    }))
    profile = load_profile_extension(str(path))
    assert profile.profile_id == "field_research"
    session = {"answers": {"project.category": _answered("project.category", "Research")}}
    question = next_question(session, "field_research")
    assert question["id"] == "foundation.goal"


def test_profile_extension_rejects_unknown_question(tmp_path):
    path = tmp_path / "bad-profile.json"
    path.write_text(json.dumps({
        "profile_id": "bad", "description": "x",
        "required": ["ghost.question"], "recommended": [],
    }))
    with pytest.raises(ValidationError, match="unknown question"):
        load_profile_extension(str(path))


def test_profile_extension_rejects_invalid_json(tmp_path):
    path = tmp_path / "not-json.json"
    path.write_text("{oops")
    with pytest.raises(ValidationError, match="not valid JSON"):
        load_profile_extension(str(path))


def test_coverage_report_categories(apdir):
    session = {"answers": {
        "foundation.goal": _answered("foundation.goal", "x"),
        "foundation.target_users": _answered("foundation.target_users", "y", kind="deferred"),
    }}
    progress = interview_progress(session, "general")
    assert progress["outstanding_required"] == [
        "project.category", "foundation.success", "quality.testing"
    ]
    foundations = progress["coverage"]["foundations"]
    assert foundations["total"] == 4  # goal, target_users, success, scope_exclusions
    assert foundations["confirmed"] == 1  # only goal is confirmed
    assert progress["coverage"]["meta"]["outstanding"] == 1


def test_registry_dump_writes_reviewable_markdown(tmp_path, apdir):
    out = tmp_path / "registry.md"
    write_registry_markdown(str(out))
    text = out.read_text()
    assert "# Onboarding interview registry" in text
    assert "### foundation.goal" in text
    assert "### web_app" in text
    # No-clobber without force.
    with pytest.raises(Exception):
        write_registry_markdown(str(out))


def test_interview_session_persistence_end_to_end(apdir):
    # Full loop: ask -> answer -> session persists -> next ask.
    from aperio.onboarding_session import read_session

    _seed(apdir)
    session = read_session(apdir)
    question = next_question(session, "general")
    assert question["id"] == "project.category"
    apply_answer(apdir, "project.category", "Web application")
    session = read_session(apdir)
    question = next_question(session, "web_app")
    assert question["id"] == "foundation.goal"
    apply_answer(apdir, "foundation.goal", "A bug tracker for my team")
    data = read_session(apdir)
    assert data["answers"]["project.category"]["answer"] == "Web application"
    assert data["answers"]["foundation.goal"]["answer"] == "A bug tracker for my team"
