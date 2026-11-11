"""Tests for universal agent integrations (host skill registration).

Contract under test:

- Install writes ONLY into each host's documented skills/commands location,
  and only for hosts detected on the machine (directory or executable).
- Every written file carries aperio's managed markers; a pre-existing file
  without them is foreign content and is never overwritten or removed.
- Install is idempotent (upgrade path); remove deletes only managed files.
- Templates render fully: no leftover ``string.Template`` placeholders, and
  ``$$``-escaped shell variables become single ``$``.
- Project scope is refused outside Aperio-managed projects.
- Status is read-only and reports absent/managed/foreign per file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aperio.integrations import (
    HOSTS,
    MANAGED_MARK_BEGIN,
    SKILL_ID,
    install_integrations,
    integrations_status,
    remove_integrations,
    render_skill_body,
    require_managed_project,
    resolve_engine_path,
)
from aperio.errors import AperioError, StateError
from aperio.cli import main


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Resolve the engine path deterministically for assertions.
    engine = home / "bin" / "aperio"
    engine.parent.mkdir()
    engine.write_text("#!/bin/sh\n")
    return home


def _install(home, hosts=("zcode",), **kwargs):
    return install_integrations(
        list(hosts), home=home, version="9.9.9test",
        engine_path=str(home / "bin" / "aperio"), **kwargs
    )


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def test_install_writes_managed_assets_for_detected_host(fake_home):
    (fake_home / ".zcode").mkdir()
    reports = _install(fake_home, hosts=("zcode",))
    (report,) = reports
    assert report.ok, report.errors
    skill = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    command = fake_home / ".zcode" / "commands" / f"{SKILL_ID}.md"
    assert report.installed == [str(skill), str(command)]
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: aperio-next\n")
    assert MANAGED_MARK_BEGIN in text and "<!-- /apario-next:managed v1 -->" in text
    assert "9.9.9test" in text
    assert str(fake_home / "bin" / "aperio") in text
    assert command.read_text(encoding="utf-8").startswith("---\ndescription:")


def test_install_skips_undetected_host(fake_home):
    reports = _install(fake_home, hosts=("cursor",))
    (report,) = reports
    assert report.ok
    assert report.installed == []
    assert len(report.skipped) == 1
    assert "not detected" in report.skipped[0]


def test_install_is_idempotent(fake_home):
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    skill = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    before = skill.read_text(encoding="utf-8")
    reports = _install(fake_home, hosts=("zcode",))
    assert reports[0].ok
    assert skill.read_text(encoding="utf-8") == before


def test_install_refuses_foreign_file(fake_home):
    zcode = fake_home / ".zcode"
    zcode.mkdir()
    skill = zcode / "skills" / SKILL_ID / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# my own notes, do not touch\n", encoding="utf-8")
    reports = _install(fake_home, hosts=("zcode",))
    (report,) = reports
    assert not report.ok
    assert any("was not created by aperio" in e for e in report.errors)
    assert skill.read_text(encoding="utf-8") == "# my own notes, do not touch\n"


def test_command_format_follows_host_docs(fake_home):
    (fake_home / ".gemini").mkdir()
    (fake_home / ".codex").mkdir()
    reports = _install(fake_home, hosts=("gemini-cli", "codex-cli"))
    by_host = {r.host_id: r for r in reports}
    assert all(r.ok for r in reports)
    # Gemini documents TOML commands.
    toml_cmd = fake_home / ".gemini" / "commands" / f"{SKILL_ID}.toml"
    assert str(toml_cmd) in by_host["gemini-cli"].installed
    toml_text = toml_cmd.read_text(encoding="utf-8")
    assert toml_text.lstrip().startswith("description =")
    assert "prompt = " in toml_text
    assert (fake_home / ".gemini" / "skills" / SKILL_ID / "SKILL.md").exists()
    # Codex documents skills on the cross-tool ~/.agents/skills path and no
    # command mechanism (prompts are deprecated), so no command is written.
    assert (fake_home / ".agents" / "skills" / SKILL_ID / "SKILL.md").exists()
    assert by_host["codex-cli"].installed == [
        str(fake_home / ".agents" / "skills" / SKILL_ID / "SKILL.md")
    ]


def test_templates_render_without_leftover_placeholders(fake_home):
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    for path in (fake_home / ".zcode").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "$HOST_INVOCATION" not in text
            assert "$VERSION" not in text
            assert "$APERIO_CLI" not in text
            # $$-escaping: shell variables survive as single $.
            assert '"$PROJECT"' in text


def test_project_scope_requires_managed_project(tmp_path, fake_home):
    unmanaged = tmp_path / "plain"
    unmanaged.mkdir()
    with pytest.raises(StateError):
        require_managed_project(unmanaged)


def test_project_scope_writes_into_managed_project(tmp_path, fake_home):
    managed = tmp_path / "proj"
    (managed / ".aperio").mkdir(parents=True)
    (fake_home / ".zcode").mkdir()
    reports = _install(
        fake_home, hosts=("zcode",), project_root=managed, scope="both"
    )
    (report,) = reports
    assert report.ok, report.errors
    assert len(report.installed) == 3
    project_skill = managed / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    assert project_skill.exists()
    assert MANAGED_MARK_BEGIN in project_skill.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------

def test_remove_deletes_only_managed_files(fake_home):
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    foreign = fake_home / ".zcode" / "commands" / "other.md"
    foreign.write_text("mine\n", encoding="utf-8")
    reports = remove_integrations(["zcode"], home=fake_home)
    (report,) = reports
    assert report.ok and len(report.removed) == 2
    assert not (fake_home / ".zcode" / "skills" / SKILL_ID).exists()
    assert foreign.exists()


def test_remove_keeps_foreign_file_and_reports_error(fake_home):
    skill = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("user content\n", encoding="utf-8")
    reports = remove_integrations(["zcode"], home=fake_home)
    (report,) = reports
    assert not report.ok
    assert any("was not created by aperio" in e for e in report.errors)
    assert skill.exists()


def test_remove_when_absent_is_a_clean_noop(fake_home):
    reports = remove_integrations(["zcode"], home=fake_home)
    (report,) = reports
    assert report.ok and report.removed == []


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def test_status_reports_absent_managed_foreign(fake_home):
    rows = {r.host_id: r for r in integrations_status(home=fake_home)}
    zcode = rows["zcode"]
    assert not zcode.detected
    (skill_path,) = [p for p in zcode.files if p.endswith("SKILL.md")]
    assert zcode.files[skill_path] == "absent"
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    rows = {r.host_id: r for r in integrations_status(home=fake_home)}
    zcode = rows["zcode"]
    assert zcode.detected
    assert all(v == "managed" for v in zcode.files.values())
    # a foreign file shows as foreign, never silently adopted
    cmd = fake_home / ".zcode" / "commands" / f"{SKILL_ID}.md"
    cmd.write_text("foreign\n", encoding="utf-8")
    rows = {r.host_id: r for r in integrations_status(home=fake_home)}
    assert rows["zcode"].files[str(cmd)] == "foreign"


def test_status_is_read_only(fake_home):
    before = sorted(str(p) for p in fake_home.rglob("*"))
    integrations_status(home=fake_home)
    after = sorted(str(p) for p in fake_home.rglob("*"))
    assert before == after


# --------------------------------------------------------------------------
# engine path
# --------------------------------------------------------------------------

def test_resolve_engine_path_prefers_path_hit(fake_home, monkeypatch):
    engine = fake_home / "bin" / "aperio"
    engine.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_home / "bin"))
    resolved = resolve_engine_path()
    assert resolved == str(engine)


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------

def test_cli_integrations_status_json(fake_home, run_cli, capsys):
    (fake_home / ".zcode").mkdir()
    code, out, err = run_cli("integrations", "status", "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["command"] == "integrations-status"
    assert {row["host"] for row in payload["hosts"]} == {
        s.host_id for s in HOSTS
    }


def test_cli_integrations_install_and_remove_roundtrip(fake_home, run_cli):
    (fake_home / ".zcode").mkdir()
    code, out, err = run_cli("integrations", "install", "--host", "zcode")
    assert code == 0, err
    assert "installed" in out
    code, out, err = run_cli("integrations", "remove", "--host", "zcode")
    assert code == 0, err
    assert "removed" in out
    assert not (fake_home / ".zcode" / "skills" / SKILL_ID).exists()


def test_cli_integrations_project_scope_requires_directory(fake_home, run_cli):
    code, out, err = run_cli(
        "integrations", "install", "--scope", "project"
    )
    assert code != 0
    assert "project" in err.lower()


def test_unknown_host_is_refused(fake_home):
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        main(["integrations", "install", "--host", "nonexistent"])


def test_every_host_has_documented_paths_or_an_explicit_note():
    for spec in HOSTS:
        assert spec.docs, spec.host_id
        assert spec.invocation, spec.host_id
        assert spec.user_skill_dir or spec.user_command_dir or spec.note, (
            spec.host_id
        )


def test_skill_frontmatter_stays_within_host_limits():
    """ZCode drops a skill whose description exceeds 1024 chars; Claude Code
    truncates description+when_to_use at 1536. Regressions here would make
    the skill silently undiscoverable."""
    import re

    from aperio.integrations import host_by_id

    for host_id in ("zcode", "claude-code"):
        body = render_skill_body(
            host_by_id(host_id), engine_path="aperio", version="t"
        )
        frontmatter = body.split("---", 2)[1]

        def _folded(key: str) -> str:
            m = re.search(rf"{key}: >-\n((?:  .*\n)+)", frontmatter)
            assert m, f"{host_id}: missing {key}"
            return " ".join(
                line.strip() for line in m.group(1).strip().splitlines()
            )

        description = _folded("description")
        assert len(description) <= 1024, (host_id, len(description))
        when_to_use = _folded("when_to_use")
        assert len(description) + len(when_to_use) <= 1536, (
            host_id,
            len(description) + len(when_to_use),
        )
        assert "name: aperio-next" in frontmatter
