"""Onboarding session state: creation, mutation, resume, and safety."""

from __future__ import annotations

import json
import os

import pytest

from aperio.errors import NotInitializedError, StateError, ValidationError
from aperio.onboarding_discovery import CLASS_EMPTY_NEW, CLASS_APERIO_PROJECT
from aperio.onboarding_session import (
    KIND_CONFIRMED,
    KIND_UNSURE,
    SEVERITY_CRITICAL,
    SEVERITY_OPTIONAL,
    STAGE_INTERVIEW,
    STAGE_STANDARDIZATION,
    add_blocker,
    add_unresolved,
    add_tool_record,
    cancel_session,
    complete_session,
    create_session,
    has_approval,
    read_session,
    record_answer,
    record_approval,
    record_decision,
    resolve_blocker,
    resolve_unresolved,
    session_exists,
    set_intent,
    set_stage,
    update_session,
)
from aperio.project import init_project


@pytest.fixture
def apdir(project_dir):
    """The .aperio directory of a freshly initialized project."""
    init_project(str(project_dir))
    return project_dir / ".aperio"


def _new_session(apdir, classification=CLASS_EMPTY_NEW):
    # `workspace` identity is the project root (the .aperio parent).
    return create_session(
        apdir,
        workspace=str(apdir.parent),
        classification=classification,
        classification_reasons=("test",),
    )


def test_create_and_read_roundtrip(apdir):
    created = _new_session(apdir)
    loaded = read_session(apdir)
    assert loaded["session_id"] == created["session_id"]
    assert loaded["session_id"].startswith("ob-")
    assert loaded["format"] == "aperio-onboarding-session"
    assert loaded["schema_version"] == 1
    assert loaded["stage"] == "discovery"
    assert loaded["workspace"] == str(apdir.parent)


def test_create_refuses_duplicate(apdir):
    _new_session(apdir)
    with pytest.raises(StateError, match="already exists"):
        _new_session(apdir)


def test_create_requires_valid_project(tmp_path):
    (tmp_path / ".aperio").mkdir()  # partial, no state.json
    with pytest.raises(Exception):
        create_session(
            tmp_path, workspace=str(tmp_path), classification=CLASS_EMPTY_NEW
        )


def test_unknown_classification_refused(apdir):
    with pytest.raises(ValidationError, match="unknown project classification"):
        create_session(
            apdir, workspace=str(apdir), classification="banana"
        )


def test_resume_after_interruption(apdir):
    _new_session(apdir)
    record_answer(apdir, "goal", answer="A desktop to-do app", kind=KIND_CONFIRMED)
    record_answer(apdir, "platforms", answer="macOS", kind=KIND_CONFIRMED)
    # Simulated interruption: a fresh process re-reads the session.
    resumed = read_session(apdir)
    assert resumed["answers"]["goal"]["answer"] == "A desktop to-do app"
    assert resumed["answers"]["platforms"]["answer"] == "macOS"
    assert resumed["answers"]["goal"]["revision"] == 1


def test_answer_revision_preserves_history(apdir):
    _new_session(apdir)
    record_answer(apdir, "goal", answer="first idea", kind=KIND_CONFIRMED)
    record_answer(apdir, "goal", answer="better idea", kind=KIND_CONFIRMED)
    data = read_session(apdir)
    assert data["answers"]["goal"]["answer"] == "better idea"
    assert data["answers"]["goal"]["revision"] == 2
    assert data["answer_history"]["goal"][0]["answer"] == "first idea"


def test_answer_kinds_validated(apdir):
    _new_session(apdir)
    with pytest.raises(ValidationError, match="unknown answer kind"):
        record_answer(apdir, "goal", answer="x", kind="ai_generated")
    record_answer(apdir, "goal", answer="not sure yet", kind=KIND_UNSURE)
    assert read_session(apdir)["answers"]["goal"]["kind"] == KIND_UNSURE


def test_oversized_answer_refused(apdir):
    _new_session(apdir)
    huge = "x" * (9 * 1024)
    with pytest.raises(ValidationError, match="byte limit"):
        record_answer(apdir, "goal", answer=huge, kind=KIND_CONFIRMED)


def test_stage_transitions_recorded(apdir):
    _new_session(apdir)
    set_stage(apdir, STAGE_INTERVIEW)
    set_stage(apdir, STAGE_STANDARDIZATION, detail="approved proposal")
    data = read_session(apdir)
    assert data["stage"] == STAGE_STANDARDIZATION
    events = [h["event"] for h in data["history"]]
    assert events.count("stage.changed") == 2


def test_unknown_stage_refused(apdir):
    _new_session(apdir)
    with pytest.raises(ValidationError, match="unknown onboarding stage"):
        set_stage(apdir, "party")


def test_cancel_preserves_state(apdir):
    _new_session(apdir)
    record_answer(apdir, "goal", answer="keep me", kind=KIND_CONFIRMED)
    cancel_session(apdir)
    data = read_session(apdir)
    assert data["cancelled"] is True
    assert data["stage"] == "cancelled"
    assert data["answers"]["goal"]["answer"] == "keep me"
    with pytest.raises(StateError, match="already cancelled"):
        cancel_session(apdir)
    with pytest.raises(StateError, match="cancelled"):
        set_stage(apdir, STAGE_INTERVIEW)


def test_complete_refuses_after_cancel(apdir):
    _new_session(apdir)
    cancel_session(apdir)
    with pytest.raises(StateError):
        complete_session(apdir)


def test_decisions_are_appended_and_hardened(apdir):
    _new_session(apdir)
    record_decision(apdir, title="Use SQLite", text="Local file database.", source="operator")
    record_decision(apdir, title="No cloud sync in v1", text="Deferred.", source="operator:interview")
    data = read_session(apdir)
    assert [d["id"] for d in data["decisions"]] == ["DEC-0001", "DEC-0002"]
    with pytest.raises(ValidationError, match="single line"):
        record_decision(apdir, title="two\nlines", text="x")
    with pytest.raises(ValidationError, match="start with '#'"):
        record_decision(apdir, title="# heading injection", text="x")


def test_approval_scoping(apdir):
    _new_session(apdir)
    record_approval(apdir, action="standardization.apply", scope="README.md")
    assert has_approval(apdir, "standardization.apply", "README.md")
    assert not has_approval(apdir, "standardization.apply", "docs/prd.md")
    assert not has_approval(apdir, "tool.install", "README.md")


def test_unresolved_lifecycle(apdir):
    _new_session(apdir)
    add_unresolved(apdir, topic="auth provider", severity=SEVERITY_CRITICAL, reason="no evidence")
    add_unresolved(apdir, topic="logo", severity=SEVERITY_OPTIONAL)
    data = read_session(apdir)
    assert len(data["unresolved"]) == 2
    add_unresolved(apdir, topic="auth provider", severity=SEVERITY_OPTIONAL, reason="decided")
    data = read_session(apdir)
    assert len(data["unresolved"]) == 2  # deduplicated by topic
    resolve_unresolved(apdir, "auth provider")
    data = read_session(apdir)
    open_items = [u for u in data["unresolved"] if u["resolved_at"] is None]
    assert [u["topic"] for u in open_items] == ["logo"]
    with pytest.raises(ValidationError):
        resolve_unresolved(apdir, "auth provider")  # already resolved


def test_unknown_severity_refused(apdir):
    _new_session(apdir)
    with pytest.raises(ValidationError, match="unknown severity"):
        add_unresolved(apdir, topic="x", severity="meh")


def test_blockers_lifecycle(apdir):
    _new_session(apdir)
    add_blocker(apdir, description="Required MCP server is missing")
    data = read_session(apdir)
    blocker = data["blockers"][0]
    assert blocker["description"] == "Required MCP server is missing"
    resolve_blocker(apdir, blocker["id"])
    data = read_session(apdir)
    assert data["blockers"][0]["resolved_at"] is not None


def test_tool_records(apdir):
    _new_session(apdir)
    add_tool_record(apdir, {"name": "chrome-devtools-mcp", "status": "missing"})
    data = read_session(apdir)
    assert data["tool_records"][0]["name"] == "chrome-devtools-mcp"
    with pytest.raises(ValidationError):
        add_tool_record(apdir, {"status": "no-name"})


def test_intent_recorded(apdir):
    _new_session(apdir)
    set_intent(apdir, "Track my team's tasks offline")
    assert read_session(apdir)["intent"] == "Track my team's tasks offline"


def test_corrupt_session_refused_not_rewritten(apdir):
    _new_session(apdir)
    path = apdir / "onboarding/session.json"
    original = path.read_bytes()
    path.write_text("{ broken")
    with pytest.raises(StateError, match="corrupt"):
        read_session(apdir)
    with pytest.raises(StateError, match="corrupt"):
        update_session(apdir, lambda d: d)
    assert path.read_bytes() == b"{ broken"


def test_newer_session_schema_refused(apdir):
    _new_session(apdir)
    path = apdir / "onboarding/session.json"
    data = read_session(apdir)
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(StateError, match="schema version 99"):
        read_session(apdir)


def test_unknown_keys_preserved_across_updates(apdir):
    _new_session(apdir)
    path = apdir / "onboarding/session.json"
    data = read_session(apdir)
    data["future_extension"] = {"kept": True}
    path.write_text(json.dumps(data))
    record_answer(apdir, "goal", answer="answer", kind=KIND_CONFIRMED)
    reloaded = read_session(apdir)
    assert reloaded["future_extension"] == {"kept": True}
    assert reloaded["answers"]["goal"]["answer"] == "answer"


def test_workspace_mismatch_refused(apdir, tmp_path):
    _new_session(apdir)
    # Copy the whole project elsewhere: the session must refuse to run.
    import shutil

    clone = tmp_path / "clone"
    shutil.copytree(apdir.parent, clone)
    with pytest.raises(StateError, match="not portable"):
        read_session(clone / ".aperio")


def test_symlinked_session_refused(apdir):
    _new_session(apdir)
    onboarding_dir = apdir / "onboarding"
    secret = apdir / "secret.json"
    secret.write_text("{}")
    real = onboarding_dir / "session.json"
    real.unlink()
    os.symlink(secret, real)
    with pytest.raises(StateError, match="symlink"):
        read_session(apdir)
    with pytest.raises(StateError, match="symlink"):
        update_session(apdir, lambda d: d)


def test_cas_catches_concurrent_modification(apdir):
    _new_session(apdir)
    from aperio.onboarding_session import (
        _publish_cas,
        session_path,
        parse_session_bytes,
    )

    raw_before = session_path(apdir).read_bytes()
    snapshot = parse_session_bytes(raw_before, session_path(apdir))
    # Another writer completes first...
    record_answer(apdir, "goal", answer="raced", kind=KIND_CONFIRMED)
    # ...then the stale snapshot tries to publish: refused, nothing modified.
    snapshot["intent"] = "stale write"
    with pytest.raises(StateError, match="changed while the update was being prepared"):
        _publish_cas(apdir, snapshot, expected_raw=raw_before)
    assert read_session(apdir)["intent"] is None
    assert read_session(apdir)["answers"]["goal"]["answer"] == "raced"


def test_history_is_bounded(apdir):
    _new_session(apdir)
    from aperio.onboarding_session import MAX_HISTORY_EVENTS

    for i in range(MAX_HISTORY_EVENTS + 20):
        record_answer(apdir, f"q{i}", answer="x", kind=KIND_CONFIRMED)
    data = read_session(apdir)
    assert len(data["history"]) <= MAX_HISTORY_EVENTS


def test_session_file_json_shape(apdir):
    _new_session(apdir)
    payload = json.loads((apdir / "onboarding/session.json").read_text())
    for key in (
        "format", "schema_version", "session_id", "workspace", "stage",
        "classification", "answers", "unresolved", "decisions", "approvals",
        "history", "cancelled",
    ):
        assert key in payload, f"missing {key}"
