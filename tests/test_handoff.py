"""Handoff management: validation, persistence, formats, failure safety."""

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import subprocess
import sys

import pytest

from aperio.errors import NotInitializedError, PathError, StateError, ValidationError
from aperio.handoff import (
    HANDOFF_FIELDS,
    HANDOFF_MARKER_V1,
    HandoffContent,
    handoff_path,
    parse_handoff_text,
    read_handoff,
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
    return write_handoff(aperio_dir, content or _content(), **kwargs)


def test_write_creates_structured_v1(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir)
    text = (aprio := handoff_path(aperio_dir)).read_text()
    lines = text.split("\n")
    assert lines[0] == "# Handoff Notes"
    assert lines[1].startswith("<!-- aperio:handoff:v1")
    assert "rev=1" in lines[1]  # first write is revision 1
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
        _write(
            aperio_dir,
            _content(current_objective="second handoff"),
            expected_revision=1,
        )
    assert handoff_path(aperio_dir).read_bytes() == before
    # Our own temp artifact was cleaned up; nothing litters the project.
    assert not (aprio := aperio_dir / "handoff.md.tmp").exists()
    # The lock was released despite the failure.
    assert not (aprio := aperio_dir / "handoff.lock").exists()


def test_stale_tmp_is_conflict(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    tmp = aperio_dir / "handoff.md.tmp"
    tmp.write_text("leftover from a crashed write")
    with pytest.raises(StateError, match="handoff.md.tmp"):
        _write(aperio_dir)
    assert tmp.read_text() == "leftover from a crashed write"
    assert "Handoff Notes" in handoff_path(aperio_dir).read_text()


def test_revision_flow_sequential(run_cli, project_dir):
    """Normal sequential handoffs: the first write needs no declaration;
    every later write must declare the revision it replaces, and each
    success bumps the revision."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    assert read_handoff(aperio_dir).revision == 0  # init template
    rev = _write(aperio_dir, _content(current_objective="rev 1"))
    assert rev == 1
    with pytest.raises(StateError, match="declare that revision"):
        _write(aperio_dir, _content(current_objective="undeclared"))
    rev = _write(
        aperio_dir, _content(current_objective="rev 2"), expected_revision=1
    )
    assert rev == 2
    rev = _write(
        aperio_dir, _content(current_objective="rev 3"), expected_revision=2
    )
    assert rev == 3
    read = read_handoff(aperio_dir)
    assert read.revision == 3
    assert read.fields["current_objective"] == "rev 3"
    assert "rev=3" in read.raw  # revision visible in the marker
    assert not (aprio := aperio_dir / "handoff.lock").exists()


def test_stale_revision_conflicts(run_cli, project_dir):
    """A stale agent (prepared against an older revision) cannot silently
    overwrite a newer handoff: conflict, nothing modified."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="agent A's handoff"))
    _write(
        aperio_dir,
        _content(current_objective="agent B's newer handoff"),
        expected_revision=1,
    )
    with pytest.raises(StateError, match="moved on"):
        _write(
            aperio_dir,
            _content(current_objective="agent A's stale update"),
            expected_revision=1,
        )
    read = read_handoff(aperio_dir)
    assert read.fields["current_objective"] == "agent B's newer handoff"
    assert read.revision == 2
    assert not (aprio := aperio_dir / "handoff.md.tmp").exists()


def test_force_replaces_deliberately(run_cli, project_dir):
    """--force is the opt-in deliberate replacement (no declaration)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="v1"))
    rev = _write(aperio_dir, _content(current_objective="forced"), force=True)
    assert rev == 2
    assert read_handoff(aperio_dir).fields["current_objective"] == "forced"


def test_newer_handoff_format_refused_without_force(run_cli, project_dir):
    """A handoff declaring an unsupported newer format is never silently
    replaced; force is the explicit user decision (documented as content
    loss for what aperio cannot read)."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    handoff_path(aperio_dir).write_text(
        "# Handoff Notes\n<!-- aperio:handoff:v2 -->\n\n## Something new\n"
    )
    with pytest.raises(StateError, match="newer format"):
        _write(aperio_dir, _content(current_objective="v1 writer"))
    # force = the explicit decision to discard the unreadable content.
    rev = _write(
        aperio_dir, _content(current_objective="v1 writer"), force=True
    )
    assert rev == 1
    assert read_handoff(aperio_dir).structured is True


def test_concurrent_handoff_writers_serialize(run_cli, project_dir):
    """Two simultaneous CLI handoff-writers declaring the same revision:
    exactly one wins, the loser gets a clear conflict, and the surviving
    handoff is the winner's at the next revision."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="base rev 1"))
    inputs = {}
    for name in ("writer-a", "writer-b"):
        path = project_dir / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    **CONTENT,
                    "current_objective": f"handoff from {name}",
                    "revises": 1,
                }
            )
        )
        inputs[name] = path

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(
                _handoff_worker, str(project_dir), str(path), False
            )
            for name, path in inputs.items()
        }
        codes = {name: fut.result(timeout=120) for name, fut in futures.items()}
    assert sorted(codes.values()) == [0, 1], (
        "exactly one writer wins, one gets the revision conflict"
    )
    read = read_handoff(aperio_dir)
    assert read.revision == 2
    assert read.fields["current_objective"].startswith("handoff from ")
    assert not (aprio := aperio_dir / "handoff.lock").exists()


def _handoff_worker(project_dir: str, input_file: str, force: bool) -> int:
    """Runs in a separate process: one CLI handoff-write."""
    cmd = [sys.executable, "-m", "aperio", "handoff-write", project_dir, "--file", input_file]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode


def test_legacy_handoff_replaceable_without_declaration(run_cli, project_dir):
    """Feature 01 compatibility: a legacy free-form handoff (revision 0)
    stays replaceable without any revision declaration — but a declaration
    that does not match reality (e.g. revises=5 on a rev-0 file) is
    treated as a stale writer, never silently ignored."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    handoff_path(aperio_dir).write_text(
        "# Handoff Notes\n\nFree-form Feature-01 content.\n"
    )
    with pytest.raises(StateError, match="moved on"):
        _write(
            aperio_dir,
            _content(current_objective="stale declaration"),
            expected_revision=5,
        )
    rev = _write(aperio_dir, _content(current_objective="structured now"))
    assert rev == 1
    read = read_handoff(aperio_dir)
    assert read.structured is True
    assert read.revision == 1


def test_external_modification_detected(run_cli, project_dir, monkeypatch):
    """A non-cooperating tool writing between the in-lock read and the
    rename is detected by the final byte check; nothing is replaced."""
    import pathlib

    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _write(aperio_dir, _content(current_objective="current"))

    real_read_bytes = pathlib.Path.read_bytes
    handoff_reads = [0]

    def tamper_on_third_handoff_read(self):
        data = real_read_bytes(self)
        if self.name == "handoff.md":
            handoff_reads[0] += 1
            if handoff_reads[0] == 3:
                # 1st read: in-lock revision read; 2nd: expected-bytes
                # capture; 3rd: the pre-publish check — the external tool
                # lands exactly there.
                with open(self, "w", encoding="utf-8") as fh:
                    fh.write("an external tool landed here")
                data = real_read_bytes(self)
        return data

    monkeypatch.setattr(pathlib.Path, "read_bytes", tamper_on_third_handoff_read)
    with pytest.raises(StateError, match="changed during the write"):
        _write(
            aperio_dir,
            _content(current_objective="ours"),
            expected_revision=1,
        )
    monkeypatch.undo()
    assert "external tool landed here" in handoff_path(aperio_dir).read_text()
    assert not (aprio := aperio_dir / "handoff.md.tmp").exists()


def test_repeated_writes_are_safe(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    revision = 0
    for i in range(3):
        revision = _write(
            aperio_dir,
            _content(current_objective=f"objective {i}"),
            expected_revision=revision if revision else None,
        )
    read = read_handoff(aperio_dir)
    assert read.fields["current_objective"] == "objective 2"
    assert read.revision == 3


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
