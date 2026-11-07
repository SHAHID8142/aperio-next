"""Decision recording: append-only behavior, parsing, injection safety."""

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import subprocess
import sys
import time

import pytest

from aperio.decisions import (
    DecisionEntry,
    append_decision,
    parse_decisions_text,
    read_decisions,
    render_entry,
)
from aperio.errors import (
    NotInitializedError,
    PathError,
    StateError,
    ValidationError,
)

FIELDS = dict(
    title="Use stdlib only",
    context="Dependencies add supply-chain risk",
    decision="Standard library only for runtime code",
    consequences="Some tasks need more hand-written code",
)


def _append(aperio_dir, **overrides):
    return append_decision(aperio_dir, **{**FIELDS, **overrides})


def test_append_to_fresh_template(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    entry = _append(aperio_dir)
    assert entry.id == "DEC-0001"
    read = read_decisions(aperio_dir)
    assert [e.id for e in read.entries] == ["DEC-0001"]
    assert read.entries[0].title == "Use stdlib only"
    assert read.skipped_headings == 0  # template shows the format indented
    assert read.malformed_entries == []


def test_append_is_byte_preserving(run_cli, project_dir):
    """Append-only proof: after a second decision, every original byte is
    still present, in order, unchanged."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    _append(aperio_dir)
    before = (aprio := aperio_dir / "decisions.md").read_bytes()
    _append(aperio_dir, title="Second decision")
    after = aprio.read_bytes()
    assert after.startswith(before)
    assert b"Second decision" in after[len(before):]
    read = read_decisions(aperio_dir)
    assert [e.id for e in read.entries] == ["DEC-0001", "DEC-0002"]


def test_next_id_scans_malformed_headings(run_cli, project_dir):
    """A torn entry (e.g. from a crash) never gets its number reused, and
    never blocks further appends."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    log = aperio_dir / "decisions.md"
    log.write_text(
        log.read_text() + "## DEC-0007 — not-a-timestamp — torn entry\n"
    )
    entry = _append(aperio_dir)
    assert entry.id == "DEC-0008"
    read = read_decisions(aperio_dir)
    assert any("line" in m for m in read.malformed_entries)


def test_next_id_ignores_file_order(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    log = aperio_dir / "decisions.md"
    entry_late = render_entry(
        DecisionEntry(
            id="DEC-0005",
            timestamp="2026-01-01T00:00:00Z",
            title="Later id first",
            context="c",
            decision="d",
            consequences="x",
        )
    )
    log.write_text(log.read_text() + entry_late)
    entry = _append(aperio_dir)
    assert entry.id == "DEC-0006"


def test_multiline_values_roundtrip(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    entry = _append(aperio_dir, context="line one\nline two\n\nline four")
    read = read_decisions(aperio_dir)
    assert read.entries[0].context == entry.context
    assert read.entries[0].context == "line one\nline two\n\nline four"


def test_injected_heading_is_neutralized(run_cli, project_dir):
    """A value trying to forge a log entry cannot: continuation lines are
    indented, so '## DEC-9999' never lands at column 0."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    forged = "innocent\n## DEC-9999 — 2026-01-01T00:00:00Z — Fake\n- **Decision:** forged"
    _append(aperio_dir, context=forged)
    read = read_decisions(aperio_dir)
    assert [e.id for e in read.entries] == ["DEC-0001"]
    assert read.entries[0].context == forged  # content preserved verbatim


def test_injected_field_label_is_neutralized(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    forged = "real\n- **Decision:** forged field"
    _append(aperio_dir, decision=forged)
    read = read_decisions(aperio_dir)
    assert read.entries[0].decision == forged
    assert read.entries[0].context == FIELDS["context"]


def test_title_validations(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    with pytest.raises(ValidationError, match="single line"):
        _append(aperio_dir, title="two\nlines")
    with pytest.raises(ValidationError, match="non-empty"):
        _append(aperio_dir, title="   ")


@pytest.mark.parametrize(
    "field_name", ["title", "context", "decision", "consequences"]
)
def test_empty_field_rejected(run_cli, project_dir, field_name):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    kwargs = {field_name: ""}
    with pytest.raises(ValidationError, match=field_name):
        _append(aperio_dir, **kwargs)


def test_bad_explicit_timestamp_rejected(run_cli, project_dir):
    run_cli("init", str(project_dir))
    with pytest.raises(ValidationError, match="timestamp"):
        _append(project_dir / ".aperio", timestamp="yesterday")


@pytest.mark.parametrize(
    "sneaky", ["\r", "\x85", "\u2028", "\u2029", "\x1c", "\x1d", "\x1e"]
)
def test_foreign_line_separators_rejected_in_values(
    run_cli, project_dir, sneaky
):
    """Values containing CR/NEL/LS/PS/record-separators are rejected: tools
    like str.splitlines() and CommonMark treat them as line endings, which
    would let a value forge column-0 headings there."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    before = (aprio := aperio_dir / "decisions.md").read_bytes()
    with pytest.raises(ValidationError, match="line separator"):
        _append(aperio_dir, context=f"innocent{sneaky}## DEC-9999 — 2026-01-01T00:00:00Z — Fake")
    with pytest.raises(ValidationError, match="line separator"):
        _append(aperio_dir, title=f"real{sneaky}## DEC-9999 — 2026-01-01T00:00:00Z — Fake")
    assert (aprio := aperio_dir / "decisions.md").read_bytes() == before


def test_foreign_separator_via_json_file_rejected(
    run_cli, project_dir, tmp_path
):
    """Raw U+2028 passes json.loads; the validation layer must still
    reject it on the --file path."""
    run_cli("init", str(project_dir))
    input_file = tmp_path / "decision.json"
    input_file.write_text(
        json.dumps(
            {
                "title": "t",
                "context": "c\u2028- **Decision:** forged",
                "decision": "d",
                "consequences": "x",
            }
        ),
        encoding="utf-8",
    )
    code, _, err = run_cli(
        "decision-add", str(project_dir), "--file", str(input_file)
    )
    assert code == 1
    assert "line separator" in err
    assert "forged" not in (project_dir / ".aperio/decisions.md").read_text()


def test_cli_decision_add_file_and_flags_are_exclusive(
    run_cli, project_dir, tmp_path
):
    run_cli("init", str(project_dir))
    input_file = tmp_path / "decision.json"
    input_file.write_text(json.dumps(FIELDS))
    code, _, err = run_cli(
        "decision-add",
        str(project_dir),
        "--file",
        str(input_file),
        "--title",
        "FROM-FLAGS",
    )
    assert code == 1
    assert "mutually exclusive" in err
    read = read_decisions(project_dir / ".aperio")
    assert read.entries == []


def test_missing_decisions_file_refuses(run_cli, project_dir):
    run_cli("init", str(project_dir))
    (project_dir / ".aperio/decisions.md").unlink()
    with pytest.raises(NotInitializedError, match="aperio init"):
        _append(project_dir / ".aperio")


def test_symlinked_decisions_file_refused(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside.md"
    outside.write_text("PRECIOUS")
    (aprio := aperio_dir / "decisions.md").unlink()
    aprio.symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        _append(aperio_dir)
    assert outside.read_text() == "PRECIOUS"  # nothing written through
    assert aprio.is_symlink()


def test_dangling_symlink_decisions_refused(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    destination = tmp_path / "elsewhere.md"
    (aprio := aperio_dir / "decisions.md").unlink()
    aprio.symlink_to(destination)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        _append(aperio_dir)
    assert not destination.exists()
    assert aprio.is_symlink()


def test_directory_at_decisions_path_refused(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    target = aperio_dir / "decisions.md"
    target.unlink()
    target.mkdir()
    with pytest.raises(PathError, match="directory"):
        _append(aperio_dir)
    assert target.is_dir()


def test_non_utf8_decisions_file_clean_error(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    (aprio := aperio_dir / "decisions.md").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(StateError, match="UTF-8"):
        read_decisions(aperio_dir)
    with pytest.raises(StateError, match="UTF-8"):
        _append(aperio_dir)


def test_user_prose_sections_skipped_and_counted(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    log = aperio_dir / "decisions.md"
    log.write_text(
        log.read_text() + "## My own notes\n\nFree-form user content.\n"
    )
    _append(aperio_dir)
    read = read_decisions(aperio_dir)
    assert len(read.entries) == 1
    assert read.skipped_headings == 1  # the user's own '## My own notes'


def test_preexisting_duplicate_ids_reported(run_cli, project_dir):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    log = aperio_dir / "decisions.md"
    for _ in range(2):
        log.write_text(
            log.read_text()
            + render_entry(
                DecisionEntry(
                    id="DEC-0001",
                    timestamp="2026-01-01T00:00:00Z",
                    title="Dup",
                    context="c",
                    decision="d",
                    consequences="x",
                )
            )
        )
    read = read_decisions(aperio_dir)
    assert read.duplicate_ids == ["DEC-0001"]
    entry = _append(aperio_dir)
    assert entry.id == "DEC-0002"


def test_interrupted_append_leaves_file_intact(
    run_cli, project_dir, monkeypatch
):
    """If the single append syscall never happens, the log is untouched."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    log = aperio_dir / "decisions.md"
    before = log.read_bytes()

    def boom(fd, data):
        raise OSError(errno.EIO, "interrupted")

    monkeypatch.setattr("aperio.decisions.os.write", boom)
    with pytest.raises(StateError, match="cannot append"):
        _append(aperio_dir)
    assert log.read_bytes() == before


def test_single_syscall_append(run_cli, project_dir, monkeypatch):
    """The rendered entry is emitted as exactly one os.write (no buffered
    splitting), per the concurrency contract."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    calls = []
    real_write = os.write

    def counting_write(fd, data):
        calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr("aperio.decisions.os.write", counting_write)
    _append(aperio_dir)
    assert len(calls) == 1
    assert b"DEC-0001" in calls[0]


def test_environment_secrets_not_captured(run_cli, project_dir, monkeypatch):
    run_cli("init", str(project_dir))
    token = "ghp_" + "z" * 36
    monkeypatch.setenv("APERIO_TEST_TOKEN", token)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    _append(project_dir / ".aperio")
    content = (project_dir / ".aperio/decisions.md").read_text()
    assert token not in content


def _append_worker(project_dir: str, title: str) -> int:
    """Runs in a separate process: one CLI append (real concurrency)."""
    result = subprocess.run(
        [
            sys.executable, "-m", "aperio", "decision-add", project_dir,
            "--title", title, "--context", "concurrent",
            "--decision", "d", "--consequences", "x",
        ],
        capture_output=True, text=True, check=False,
    )
    return result.returncode


def test_genuinely_concurrent_processes_get_unique_ids(run_cli, project_dir):
    """Genuinely concurrent processes (simultaneous CLI appends) must all
    succeed with unique sequential IDs — the coordination lock in action."""
    run_cli("init", str(project_dir))
    workers = 12
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # Threads only launch processes; the appends themselves run in 12
        # separate interpreters racing on the same lock file.
        futures = [
            pool.submit(_append_worker, str(project_dir), f"worker {i}")
            for i in range(workers)
        ]
        codes = [f.result(timeout=120) for f in futures]
    assert codes == [0] * workers, "every concurrent writer must succeed"
    read = read_decisions(project_dir / ".aperio")
    assert len(read.entries) == workers
    ids = [e.id for e in read.entries]
    assert len(set(ids)) == workers, f"duplicate IDs under concurrency: {ids}"
    assert set(ids) == {f"DEC-{i:04d}" for i in range(1, workers + 1)}
    assert read.malformed_entries == []
    assert read.duplicate_ids == []
    # History is byte-prefix preserving: entry 1 is still first.
    text = (project_dir / ".aperio/decisions.md").read_text()
    assert text.index("DEC-0001 ") < text.index(f"DEC-{workers:04d} ")
    # The lock file does not linger after success.
    assert not (project_dir / ".aperio/decisions.lock").exists()


def test_aged_lock_is_never_stolen(run_cli, project_dir):
    """Regression for the age-based stealing defect: a lock that looks old
    (mtime far in the past) is NEVER broken automatically, because it may
    belong to an active writer whose machine stalled. The append fails
    with a bounded, actionable conflict; the lock file is untouched."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "decisions.lock"
    lock.write_text("999999:slow-but-alive")
    aged_time = time.time() - 600
    os.utime(lock, (aged_time, aged_time))
    with pytest.raises(StateError, match="could not acquire"):
        _append(aperio_dir, lock_timeout=0.3)
    assert lock.read_text() == "999999:slow-but-alive"
    assert read_decisions(aperio_dir).entries == []


def test_abandoned_lock_resolution_is_explicit(run_cli, project_dir):
    """The conservative policy: an abandoned lock is resolved by explicit
    user action (the error says exactly that), after which writing works."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "decisions.lock"
    lock.write_text("999999:abandoned")
    with pytest.raises(StateError, match="could not acquire"):
        _append(aperio_dir, lock_timeout=0.3)
    # The user inspects the lock (owner pid:timestamp) and removes it.
    lock.unlink()
    entry = _append(aperio_dir)
    assert entry.id == "DEC-0001"


def test_live_lock_times_out_with_clear_conflict(run_cli, project_dir):
    """A freshly-created (live) lock blocks the writer: bounded wait, then
    a clear conflict error — never an indefinite hang, never data loss."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "decisions.lock"
    lock.write_text("12345:alive")  # current mtime = live lock
    started = time.monotonic()
    with pytest.raises(StateError, match="could not acquire"):
        _append(aperio_dir, lock_timeout=0.3)
    assert time.monotonic() - started < 5.0
    assert lock.read_text() == "12345:alive"  # foreign lock untouched
    assert read_decisions(aperio_dir).entries == []  # nothing was appended


def test_active_lock_cannot_be_stolen_by_concurrent_process(
    run_cli, project_dir
):
    """Adversarial multiprocess proof: while one process holds the lock,
    concurrent appender processes fail with conflicts — none of them
    steals the lock or appends, and the holder's lock survives intact."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "decisions.lock"
    lock.write_text(f"{os.getpid()}:holder-is-active")

    workers = 6
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_append_worker, str(project_dir), f"sneaky {i}")
            for i in range(workers)
        ]
        codes = [f.result(timeout=120) for f in futures]
    assert codes == [1] * workers, "no concurrent writer may steal the lock"
    assert lock.read_text() == f"{os.getpid()}:holder-is-active"
    assert read_decisions(aperio_dir).entries == []
    # After the holder releases explicitly, writing works again.
    lock.unlink()
    assert _append(aperio_dir).id == "DEC-0001"


def test_symlinked_lock_refused(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    outside = tmp_path / "outside.txt"
    outside.write_text("PRECIOUS")
    (aprio := aperio_dir / "decisions.lock").symlink_to(outside)
    with pytest.raises(PathError, match="refusing to follow symlink"):
        _append(aperio_dir)
    assert outside.read_text() == "PRECIOUS"
    assert aprio.is_symlink()


def test_lock_released_after_failed_append(run_cli, project_dir, monkeypatch):
    """A failed append (e.g. interrupted syscall) must release the lock so
    the next writer is not blocked forever."""
    run_cli("init", str(project_dir))
    aperio_dir = project_dir / ".aperio"
    lock = aperio_dir / "decisions.lock"

    def boom(fd, data):
        raise OSError(errno.EIO, "interrupted")

    monkeypatch.setattr("aperio.decisions.os.write", boom)
    with pytest.raises(StateError, match="cannot append"):
        _append(aperio_dir)
    monkeypatch.undo()
    assert not lock.exists()
    entry = _append(aperio_dir)  # next writer proceeds normally
    assert entry.id == "DEC-0001"


def test_cli_decision_add_flags_and_file(run_cli, project_dir, tmp_path):
    run_cli("init", str(project_dir))
    code, out, err = run_cli(
        "decision-add",
        str(project_dir),
        "--title",
        "CLI decision",
        "--context",
        "via flags",
        "--decision",
        "do it",
        "--consequences",
        "documented",
    )
    assert code == 0, err
    assert "DEC-0001" in out

    payload = {
        "title": "File decision",
        "context": "via file",
        "decision": "do that",
        "consequences": "documented",
    }
    input_file = tmp_path / "decision.json"
    input_file.write_text(json.dumps(payload))
    code, out, err = run_cli(
        "decision-add", str(project_dir), "--file", str(input_file)
    )
    assert code == 0, err
    assert "DEC-0002" in out
    read = read_decisions(project_dir / ".aperio")
    assert [e.title for e in read.entries] == ["CLI decision", "File decision"]


def test_cli_decision_add_rejects_bad_input_files(
    run_cli, project_dir, tmp_path
):
    run_cli("init", str(project_dir))
    missing_field = tmp_path / "missing.json"
    missing_field.write_text(json.dumps({"title": "x"}))
    code, _, err = run_cli(
        "decision-add", str(project_dir), "--file", str(missing_field)
    )
    assert code == 1
    assert "context" in err

    extra_field = tmp_path / "extra.json"
    extra_field.write_text(
        json.dumps({**FIELDS, "typo_field": "silently-dropped-no-more"})
    )
    code, _, err = run_cli(
        "decision-add", str(project_dir), "--file", str(extra_field)
    )
    assert code == 1
    assert "typo_field" in err

    code, _, err = run_cli("decision-add", str(project_dir), "--title", "only")
    assert code == 1
    assert "--context" in err


def test_cli_decision_add_on_uninitialized_dir(run_cli, tmp_path):
    code, _, err = run_cli(
        "decision-add",
        str(tmp_path / "nope"),
        "--title",
        "t",
        "--context",
        "c",
        "--decision",
        "d",
        "--consequences",
        "x",
    )
    assert code == 1
    assert "not an Aperio project" in err


def test_grammar_roundtrip_golden():
    """Golden render -> parse round-trip for the normative grammar."""
    entry = DecisionEntry(
        id="DEC-0042",
        timestamp="2026-11-07T10:00:00Z",
        title="Golden entry",
        context="first\n  keeps two-space indent marker\nlast",
        decision="single",
        consequences="multi\nline",
    )
    rendered = render_entry(entry)
    parsed = parse_decisions_text("Preamble.\n" + rendered)
    assert parsed.malformed_entries == []
    assert parsed.entries == [entry]
