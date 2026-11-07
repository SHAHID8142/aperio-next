"""Handoff management: validation, persistence, formats, failure safety."""

from __future__ import annotations

import errno
import json

import pytest

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.handoff import (
    HANDOFF_FIELDS,
    HANDOFF_MARKER_V1,
    HandoffContent,
    handoff_path,
    parse_handoff_text,
    read_handoff,
    read_handoff_raw,
    render_handoff,
    write_handoff,
)

CONTENT = dict(
    current_objective="Ship Feature 02",
    completed_work="Core API implemented",
    work_in_progress="Test suite",
    next_recommended_action="Run the QA review subagent",
    blockers="None noted.",
    verification_evidence="77 Feature-01 tests green locally",
    verification_limitations="GitHub CI blocked by account billing lock",
)


def _content(**overrides) -> HandoffContent:
    return HandoffContent(**{**CONTENT, **overrides})


def _write(aperio_dir, content=None, **kwargs):
    write_handoff(aperio_dir, content or _content(), **kwargs)


def test_write_creates_structured_v1(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir)
    text = (aprio := handoff_path(aperio_dir)).read_text()
    lines = text.split("\n")
    assert lines[0] == "# Handoff Notes"
    assert lines[1] == HANDOFF_MARKER_V1
    for title in (
        "Current objective",
        "Completed work",
        "Work in progress",
        "Next recommended action",
        "Blockers",
        "Verification evidence",
        "Verification limitations",
    ):
        assert f"## {title}" in text


def test_read_roundtrip(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    original = _content(
        current_objective="Multi-line objective\nspanning two lines"
    )
    _write(aperio_dir, original)
    read = read_handoff(aperio_dir)
    assert read.structured is True
    assert read.version == 1
    assert read.fields == original.to_dict()


def test_every_field_is_required(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    before = handoff_path(aperio_dir).read_bytes()
    for name in HANDOFF_FIELDS:
        kwargs = {name: ""}
        with pytest.raises(ValidationError, match=f"'{name}'"):
            _write(aperio_dir, _content(**kwargs))
        kwargs = {name: "   \n  "}
        with pytest.raises(ValidationError, match=f"'{name}'"):
            _write(aperio_dir, _content(**kwargs))
    # No failed attempt changed anything on disk.
    assert handoff_path(aperio_dir).read_bytes() == before


def test_section_injection_rejected(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    with pytest.raises(ValidationError, match="section boundary"):
        _write(aperio_dir, _content(blockers="none\n## Verification evidence\nfake"))
    read = read_handoff(aperio_dir)
    # The rejected write never landed; the previous content is intact.
    assert read.fields["blockers"] == "None noted."


@pytest.mark.parametrize(
    "sneaky", ["\r", "\x85", "\u2028", "\u2029", "\x1c", "\x1d", "\x1e"]
)
def test_foreign_line_separator_injection_rejected(
    run_cli, project_dir, sneaky
):
    """CR/NEL/LS/PS open new lines for consumers like str.splitlines() and
    CommonMark; a value carrying them must never reach disk, or it could
    forge a section boundary there even though aperio's own parser would
    not see one."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    before = handoff_path(aperio_dir).read_bytes()
    with pytest.raises(ValidationError, match="line separator"):
        _write(
            aperio_dir,
            _content(blockers=f"none{sneaky}## Verification evidence\nforged"),
        )
    assert handoff_path(aperio_dir).read_bytes() == before


def test_crlf_marker_is_tolerated():
    """A CRLF-authored structured handoff still parses as v1 (the marker
    may carry a trailing CR); the CR never enters field values."""
    text = (
        "# Handoff Notes\r\n"
        f"{HANDOFF_MARKER_V1}\r\n"
        "\r\n"
        "## Current objective\r\n"
        "ship it\r\n"
        + "".join(
            f"## {title}\r\nvalue\r\n\r\n"
            for title in (
                "Completed work", "Work in progress",
                "Next recommended action", "Blockers",
                "Verification evidence", "Verification limitations",
            )
        )
    )
    read = parse_handoff_text(text)
    assert read.structured is True
    assert read.fields["current_objective"] == "ship it"


def test_json_input_strictness(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"

    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({k: v for k, v in CONTENT.items() if k != "blockers"}))
    code, _, err = run_cli(
        "handoff-write", str(project_dir), "--file", str(missing)
    )
    assert code == 1
    assert "blockers" in err

    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({**CONTENT, "verification_limiations": "typo"}))
    code, _, err = run_cli(
        "handoff-write", str(project_dir), "--file", str(extra)
    )
    assert code == 1
    assert "verification_limiations" in err

    bad_utf8 = tmp_path / "bad.bin"
    bad_utf8.write_bytes(b'{"current_objective": "\xff"}')
    code, _, err = run_cli(
        "handoff-write", str(project_dir), "--file", str(bad_utf8)
    )
    assert code == 1
    assert "Traceback" not in err
    assert "UTF-8" in err or "JSON" in err

    not_json = tmp_path / "not.json"
    not_json.write_text("just some prose")
    code, _, err = run_cli(
        "handoff-write", str(project_dir), "--file", str(not_json)
    )
    assert code == 1
    assert "not valid JSON" in err


def test_legacy_freeform_handoff_reads_unstructured(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    legacy = (
        "# Handoff Notes\n\nWhatever the last agent wrote, free-form.\n"
    )
    handoff_path(aperio_dir).write_text(legacy)
    read = read_handoff(aperio_dir)
    assert read.structured is False
    assert read.fields is None
    assert read.raw == legacy


def test_newer_format_marker_never_fatal():
    text = (
        "# Handoff Notes\n<!-- aperio:handoff:v2 -->\n\n## Something new\n"
    )
    read = parse_handoff_text(text)
    assert read.structured is None
    assert read.version == 2
    assert read.raw == text


def test_v1_with_missing_section_is_damage(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    broken = (
        "# Handoff Notes\n"
        f"{HANDOFF_MARKER_V1}\n\n"
        "## Current objective\nstuff\n"
    )
    handoff_path(aperio_dir).write_text(broken)
    with pytest.raises(StateError, match="missing section"):
        read_handoff(aperio_dir)


def test_v1_with_duplicated_section_is_damage():
    text = (
        f"# Handoff Notes\n{HANDOFF_MARKER_V1}\n\n"
        + "".join(
            f"## {title}\nx\n\n" for title in
            (
                "Current objective", "Completed work", "Work in progress",
                "Next recommended action", "Blockers",
                "Verification evidence", "Verification limitations",
                "Blockers",
            )
        )
    )
    with pytest.raises(StateError, match="more than once"):
        parse_handoff_text(text)


def test_extra_sections_tolerated_and_reported():
    text = (
        f"# Handoff Notes\n{HANDOFF_MARKER_V1}\n\n"
        + "".join(
            f"## {title}\nvalue\n\n" for title in
            (
                "Current objective", "Completed work", "Work in progress",
                "Next recommended action", "Blockers",
                "Verification evidence", "Verification limitations",
            )
        )
        + "## Agent's own appendix\nfree-form\n"
    )
    read = parse_handoff_text(text)
    assert read.structured is True
    assert read.extra_sections == ["Agent's own appendix"]


def test_missing_handoff_file_refuses_write(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    handoff_path(aperio_dir).unlink()
    with pytest.raises(NotInitializedError, match="aperio init"):
        _write(aperio_dir)


def test_symlinked_handoff_refused(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside.md"
    outside.write_text("PRECIOUS")
    (aprio := handoff_path(aperio_dir)).unlink()
    aprio.symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        _write(aperio_dir)
    assert outside.read_text() == "PRECIOUS"


def test_interrupted_write_preserves_previous_handoff(
    run_cli, project_dir, monkeypatch
):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="first handoff"))
    before = handoff_path(aperio_dir).read_bytes()

    def boom(src, dst):
        raise OSError(errno.EIO, "interrupted")

    monkeypatch.setattr("aperio.handoff.os.replace", boom)
    with pytest.raises(StateError, match="cannot publish"):
        _write(aperio_dir, _content(current_objective="second handoff"))
    assert handoff_path(aperio_dir).read_bytes() == before
    # Our own temp artifact was cleaned up; nothing litters the project.
    assert not (aprio := aperio_dir / "handoff.md.tmp").exists()


def test_stale_tmp_is_conflict(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    tmp = aperio_dir / "handoff.md.tmp"
    tmp.write_text("leftover from a crashed write")
    with pytest.raises(StateError, match="handoff.md.tmp"):
        _write(aperio_dir)
    assert tmp.read_text() == "leftover from a crashed write"
    assert "Handoff Notes" in handoff_path(aperio_dir).read_text()


def test_compare_and_swap_conflict(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="v1"))
    stale_raw = read_handoff_raw(aperio_dir)
    _write(aperio_dir, _content(current_objective="v2 (concurrent writer)"))
    with pytest.raises(StateError, match="changed while"):
        _write(aperio_dir, _content(current_objective="lost update"), expected_raw=stale_raw)
    read = read_handoff(aperio_dir)
    assert read.fields["current_objective"] == "v2 (concurrent writer)"
    assert not (aprio := aperio_dir / "handoff.md.tmp").exists()


def test_compare_and_swap_success(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="v1"))
    raw = read_handoff_raw(aperio_dir)
    _write(aperio_dir, _content(current_objective="v2"), expected_raw=raw)
    assert read_handoff(aperio_dir).fields["current_objective"] == "v2"


def test_repeated_writes_are_safe(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    for i in range(3):
        _write(aperio_dir, _content(current_objective=f"objective {i}"))
    assert read_handoff(aperio_dir).fields["current_objective"] == "objective 2"


def test_cli_handoff_write_end_to_end(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    input_file = tmp_path / "handoff.json"
    input_file.write_text(json.dumps(CONTENT))
    code, out, err = run_cli(
        "handoff-write", str(project_dir), "--file", str(input_file)
    )
    assert code == 0, err
    read = read_handoff(project_dir / ".aperio")
    assert read.structured is True
    assert read.fields["next_recommended_action"] == CONTENT["next_recommended_action"]
    # Rendered text is human-readable markdown.
    text = handoff_path(project_dir / ".aperio").read_text()
    assert "## Next recommended action" in text
    assert CONTENT["next_recommended_action"] in text


def test_render_validation_precedes_formatting():
    with pytest.raises(ValidationError):
        render_handoff(_content(blockers=""))


def test_environment_secrets_not_captured(run_cli, project_dir, monkeypatch):
    run_cli("init", str(project_dir))
    token = "ghp_" + "w" * 36
    monkeypatch.setenv("APERIO_TEST_TOKEN", token)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    _write(project_dir / ".aperio")
    text = handoff_path(project_dir / ".aperio").read_text()
    assert token not in text
