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
    host_by_id,
    install_integrations,
    integrations_status,
    remove_integrations,
    render_skill_body,
    require_managed_project,
    resolve_engine_path,
)
from aperio.errors import AperioError, StateError
from aperio.cli import main


def test_symlinked_host_ancestor_never_writes_or_removes_outside_home(tmp_path):
    from aperio.integrations import host_by_id, install_for_host, remove_for_host, managed_asset_body
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".claude").symlink_to(outside, target_is_directory=True)
    spec = host_by_id("claude-code")
    report = install_for_host(spec, home=home, engine_path="/fixture/aperio", version="1")
    assert report.errors
    assert not (outside / "skills").exists()
    asset = outside / "skills" / "aperio-next" / "SKILL.md"
    asset.parent.mkdir(parents=True)
    asset.write_text(managed_asset_body("preserve me"))
    removal = remove_for_host(spec, home=home)
    assert removal.errors
    assert asset.exists()


def test_codex_project_registration_uses_documented_repo_skill_path(fake_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    reports = _install(fake_home, hosts=("codex-cli",), project_root=project, scope="project")
    assert reports[0].ok
    assert reports[0].installed == [str(project / ".agents/skills/aperio-next/SKILL.md")]


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


def _fake_engine(path: Path, *, product: str) -> Path:
    """Write an executable that answers only `core-identity --json`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = core-identity ]; then\n"
        f"  printf '%s' '{{\"product\": \"{product}\", \"version\": \"x\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    path.chmod(0o755)
    return path


def test_resolve_engine_path_prefers_isolated_install_over_path(
    tmp_path, monkeypatch
):
    """A legacy `aperio` earlier on PATH must not win over ~/.aperio-next."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)

    legacy_dir = tmp_path / "legacy"
    legacy = legacy_dir / "aperio"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\nexit 0\n")
    legacy.chmod(0o755)
    monkeypatch.setenv("PATH", str(legacy_dir))

    isolated = _fake_engine(
        home / ".aperio-next" / "bin" / "aperio", product="aperio-next"
    )

    resolved = resolve_engine_path()
    assert resolved == str(isolated)
    assert str(legacy) not in resolved


def test_resolve_engine_path_validates_product(tmp_path, monkeypatch):
    """With validate=True a non-aperio-next executable is never returned."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)

    legacy_dir = tmp_path / "legacy"
    legacy = _fake_engine(legacy_dir / "aperio", product="aperio-legacy-v2")
    monkeypatch.setenv("PATH", str(legacy_dir))

    # Without validation the PATH hit is returned as-is.
    assert resolve_engine_path() == str(legacy)
    # With validation the wrong-product executable on PATH is skipped.
    monkeypatch.setattr(
        "aperio.integrations.Path.with_name",
        lambda self, name: tmp_path / "does-not-exist",
    )
    assert resolve_engine_path(validate=True) != str(legacy)


def test_resolve_engine_path_uses_isolated_env_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    custom = tmp_path / "custom-home"
    isolated = _fake_engine(
        custom / "bin" / "aperio", product="aperio-next"
    )
    monkeypatch.setenv("APERIO_NEXT_HOME", str(custom))
    monkeypatch.setenv("PATH", "")
    assert resolve_engine_path(validate=True) == str(isolated)


def test_quote_for_shell_preserves_plain_and_quotes_spaces():
    from aperio.integrations import _quote_for_shell

    assert _quote_for_shell("/usr/local/bin/aperio") == "/usr/local/bin/aperio"
    assert _quote_for_shell("/p a t h/aperio") == "'/p a t h/aperio'"
    assert _quote_for_shell("/w'x/aperio") == "'/w'\\''x/aperio'"


def test_engine_repair_hint_mentions_install_for_bare_name():
    from aperio.integrations import engine_repair_hint

    assert "aperio-next install" in engine_repair_hint("aperio")


def test_install_refuses_when_engine_is_unvalidated_bare_name(fake_home, monkeypatch):
    """No silent fallback: if no engine validates, install refuses (FIX A)."""
    from aperio.integrations import IntegrationError

    (fake_home / ".zcode").mkdir()
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)
    monkeypatch.setattr("aperio.integrations.resolve_engine_path",
                        lambda **kw: "aperio")
    with pytest.raises(IntegrationError) as excinfo:
        install_integrations(["zcode"], home=fake_home, version="9.9.9")
    assert "aperio-next install" in str(excinfo.value)
    # Nothing was written into the host slot.
    assert not (
        fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    ).exists()


def test_install_with_explicit_engine_path_is_not_validated(fake_home):
    """An explicit engine_path is trusted (used by the CLI installer flow)."""
    (fake_home / ".zcode").mkdir()
    reports = install_integrations(
        ["zcode"], home=fake_home, version="9.9.9",
        engine_path="/opt/custom/aperio",
    )
    assert any(r.installed for r in reports)


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------

def test_cli_core_identity_json(run_cli):
    code, out, err = run_cli("core-identity", "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["product"] == "aperio-next"
    assert payload["version"]
    assert "executable" in payload


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


def test_cli_install_partial_failure_exits_nonzero_json(fake_home, run_cli):
    """A foreign file on one host must make the JSON install exit nonzero
    while the other host's success is still reported."""
    (fake_home / ".zcode").mkdir()
    (fake_home / ".claude").mkdir()
    # Occupy the zcode skill slot with foreign content.
    slot = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    slot.parent.mkdir(parents=True)
    slot.write_text("user content\n")

    code, out, err = run_cli(
        "integrations", "install", "--json",
        "--host", "zcode", "--host", "claude-code",
    )
    assert code != 0, (out, err)
    payload = json.loads(out)
    assert payload["ok"] is False
    by_host = {r["host"]: r for r in payload["results"]}
    assert by_host["zcode"]["ok"] is False
    assert by_host["claude-code"]["ok"] is True
    assert by_host["claude-code"]["installed"]
    assert slot.read_text() == "user content\n"


def test_cli_install_full_failure_exits_nonzero_text(fake_home, run_cli):
    (fake_home / ".zcode").mkdir()
    slot = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    slot.parent.mkdir(parents=True)
    slot.write_text("user content\n")
    code, out, err = run_cli("integrations", "install", "--host", "zcode")
    assert code != 0
    assert "error" in out.lower()


def test_cli_remove_failure_exits_nonzero(fake_home, run_cli):
    """A real removal error must surface as a nonzero exit."""
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    skill = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    skill.unlink()
    skill.mkdir()
    (skill / "blocker").write_text("x")
    code, out, err = run_cli(
        "integrations", "remove", "--json", "--host", "zcode"
    )
    assert code != 0, (out, err)
    payload = json.loads(out)
    assert payload["ok"] is False


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


# --------------------------------------------------------------------------
# FIX A: native command templates embed the verified engine (never bare)
# --------------------------------------------------------------------------

def test_render_command_body_embeds_engine_path_md():
    from aperio.integrations import host_by_id, render_command_body

    body = render_command_body(
        host_by_id("claude-code"),
        engine_path="/opt/aperio-next/bin/aperio",
        version="9.9.9",
    )
    assert "/opt/aperio-next/bin/aperio next" in body
    assert "core-identity" in body
    # No bare ``aperio next`` remains (a legacy PATH hit) except inside the
    # quoted repair guidance text ('npx aperio-next install').
    assert "\naperio next" not in body


def test_render_command_body_embeds_engine_path_toml():
    from aperio.integrations import host_by_id, render_command_body

    body = render_command_body(
        host_by_id("gemini-cli"),
        engine_path="/opt/aperio-next/bin/aperio",
        version="9.9.9",
    )
    assert "/opt/aperio-next/bin/aperio next" in body
    assert "core-identity" in body
    assert "\naperio next" not in body


def test_render_command_body_quotes_paths_with_spaces():
    from aperio.integrations import (
        _quote_for_shell,
        host_by_id,
        render_command_body,
    )

    quoted = _quote_for_shell("/Users/me/My Tools/aperio")
    assert quoted == "'/Users/me/My Tools/aperio'"
    body = render_command_body(
        host_by_id("claude-code"), engine_path=quoted, version="1"
    )
    assert "'/Users/me/My Tools/aperio' next" in body


def test_install_command_never_embeds_legacy_path_over_isolated(
    tmp_path, monkeypatch
):
    """A legacy ``aperio`` earlier on PATH must not be embedded in the
    generated command asset when an isolated aperio-next install exists."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy = legacy_dir / "aperio"
    legacy.write_text("#!/bin/sh\nexit 0\n")
    legacy.chmod(0o755)
    monkeypatch.setenv("PATH", str(legacy_dir))

    isolated = _fake_engine(
        home / ".aperio-next" / "bin" / "aperio", product="aperio-next"
    )

    reports = install_integrations(
        ["claude-code"], home=home, version="1"
    )
    report = reports[0]
    # Claude Code has both a skill and a command; read the command asset.
    cmd = home / ".claude" / "commands" / f"{SKILL_ID}.md"
    assert cmd.is_file(), report.installed
    text = cmd.read_text()
    assert str(isolated) in text
    assert str(legacy) not in text
    assert "\naperio next" not in text


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


# --------------------------------------------------------------------------
# project instructions (P0-D: durable managed-project pointer)
# --------------------------------------------------------------------------

def _managed_project(tmp_path, name="proj"):
    from aperio.project import init_project

    proj = tmp_path / name
    proj.mkdir()
    init_project(str(proj))
    assert (proj / ".aperio").is_dir()
    return proj


def test_project_instructions_requires_managed_project(tmp_path):
    from aperio.integrations import install_project_instructions

    unmanaged = tmp_path / "loose"
    unmanaged.mkdir()
    with pytest.raises(StateError):
        install_project_instructions(
            unmanaged, engine_path="aperio", version="1"
        )


def test_project_instructions_create_agents_md_section(tmp_path):
    from aperio.integrations import (
        install_project_instructions,
        remove_project_instructions,
    )

    proj = _managed_project(tmp_path)
    report = install_project_instructions(
        proj, engine_path="/opt/aperio-next/bin/aperio", version="9.9.9"
    )
    assert report["errors"] == []
    agents = proj / "AGENTS.md"
    text = agents.read_text()
    assert "aperio-next:project-instructions:begin" in text
    assert "/opt/aperio-next/bin/aperio" in text
    assert "9.9.9" in text

    # Idempotent: a second install is byte-stable.
    install_project_instructions(
        proj, engine_path="/opt/aperio-next/bin/aperio", version="9.9.9"
    )
    assert agents.read_text() == text

    # Remove strips only our section and deletes the file we created.
    cleaned = remove_project_instructions(proj)
    assert cleaned["errors"] == []
    assert not agents.exists()


def test_project_instructions_preserve_existing_user_content(tmp_path):
    from aperio.integrations import (
        install_project_instructions,
        remove_project_instructions,
    )

    proj = _managed_project(tmp_path)
    agents = proj / "AGENTS.md"
    agents.write_text("# My rules\n\n- Always run tests.\n")

    install_project_instructions(proj, engine_path="aperio", version="1")
    text = agents.read_text()
    assert text.startswith("# My rules")
    assert "- Always run tests." in text
    assert "aperio-next:project-instructions:begin" in text

    remove_project_instructions(proj)
    remaining = agents.read_text()
    assert "- Always run tests." in remaining
    assert "aperio-next:project-instructions" not in remaining


def test_project_instructions_updates_claude_md_when_present(tmp_path):
    from aperio.integrations import install_project_instructions

    proj = _managed_project(tmp_path)
    claude = proj / "CLAUDE.md"
    claude.write_text("# Claude notes\n")
    install_project_instructions(
        proj, engine_path="aperio", version="1",
        files=("AGENTS.md", "CLAUDE.md"),
    )
    assert "aperio-next:project-instructions:begin" in claude.read_text()


def test_project_instructions_refuses_symlink(tmp_path):
    from aperio.integrations import install_project_instructions

    proj = _managed_project(tmp_path)
    real = tmp_path / "real-agents.md"
    real.write_text("# target\n")
    (proj / "AGENTS.md").symlink_to(real)
    report = install_project_instructions(proj, engine_path="aperio", version="1")
    assert report["errors"]
    assert "symlink" in report["errors"][0]
    # The symlink target is untouched.
    assert real.read_text() == "# target\n"


def test_cli_project_instructions_requires_project_scope(fake_home, run_cli):
    code, out, err = run_cli(
        "integrations", "install", "--project-instructions", "--host", "zcode"
    )
    assert code != 0
    assert "project" in err.lower()


def test_cli_project_instructions_roundtrip(tmp_path, run_cli, monkeypatch):
    # Project uses real tmp_path; home is redirected so no real host dirs are
    # touched.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".zcode").mkdir()
    proj = _managed_project(tmp_path, "cli-proj")

    code, out, err = run_cli(
        "integrations", "install", "--scope", "project",
        "--project-instructions", "--json", str(proj),
    )
    assert code == 0, err
    payload = json.loads(out)
    assert "project_instructions" in payload
    assert payload["project_instructions"]["errors"] == []
    assert (proj / "AGENTS.md").exists()

    # Status reports it as managed.
    code, out, err = run_cli(
        "integrations", "status", "--scope", "project", "--json", str(proj)
    )
    assert code == 0, err
    status = json.loads(out)
    assert status["project_instructions"]["AGENTS.md"] == "managed"

    # Remove cleans it.
    code, out, err = run_cli(
        "integrations", "remove", "--scope", "project",
        "--project-instructions", "--json", str(proj),
    )
    assert code == 0, err
    assert not (proj / "AGENTS.md").exists()


def test_fresh_interpreter_process_recovers_managed_project(tmp_path, monkeypatch):
    """Recovery across a *new Python process* (not a new AI-agent session).

    Scope note (honesty): this proves the durable markers are readable by a
    brand-new interpreter with zero in-process state. It is deliberately NOT
    claimed as an "AI session" test — a real new host session (OpenCode /
    Claude Code) is exercised separately in the live host checks, because a
    fresh `python -m aperio` process is a weaker condition than a new agent
    conversation.
    """
    import os
    import subprocess
    import sys

    from aperio.integrations import install_project_instructions

    proj = _managed_project(tmp_path, "recover-proj")
    install_project_instructions(proj, engine_path="aperio", version="1")

    # Durable markers exist on disk without any live session memory.
    assert (proj / ".aperio").is_dir()
    agents = (proj / "AGENTS.md").read_text()
    assert "aperio-next:project-instructions:begin" in agents

    # A genuinely separate interpreter (different PID) with no in-process
    # state classifies the project as Aperio-managed.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "aperio", "next", str(proj), "--json"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc.get("classification") == "aperio_project"
    assert doc.get("format", "").startswith("aperio")
    assert any(".aperio" in r for r in doc.get("classification_reasons", []))


# --------------------------------------------------------------------------
# FIX B: safe removal / aggregate failure propagation
# --------------------------------------------------------------------------

def test_remove_refuses_symlinked_managed_file(fake_home):
    """A managed file replaced by a symlink is refused, not silently unlinked."""
    (fake_home / ".zcode").mkdir()
    _install(fake_home, hosts=("zcode",))
    skill = fake_home / ".zcode" / "skills" / SKILL_ID / "SKILL.md"
    target = fake_home / "real-skill.md"
    target.write_text(skill.read_text())
    skill.unlink()
    skill.symlink_to(target)

    reports = remove_integrations(["zcode"], home=fake_home)
    (report,) = reports
    assert not report.ok
    assert any("symlink" in e for e in report.errors)
    # The symlink and its target both survive.
    assert skill.is_symlink()
    assert target.exists()


def test_cli_remove_project_instruction_failure_is_nonzero(
    tmp_path, run_cli, monkeypatch
):
    """Aggregate failure must include project-instruction cleanup errors even
    when every host reports OK (FIX B)."""
    from aperio.integrations import install_project_instructions

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = _managed_project(tmp_path, "pi-fail")
    install_project_instructions(proj, engine_path="aperio", version="1")

    # Turn the pointer file into a directory so reading it fails on cleanup.
    agents = proj / "AGENTS.md"
    agents.unlink()
    agents.mkdir()
    (agents / "blocker").write_text("x")

    code, out, err = run_cli(
        "integrations", "remove", "--scope", "project",
        "--project-instructions", "--json", str(proj),
    )
    assert code != 0, (out, err)
    payload = json.loads(out)
    assert payload["ok"] is False
    # Prove *this* failure was triggered: the reported error names AGENTS.md
    # and the directory-is-not-a-file cause, not some unrelated host error.
    errors = payload["project_instructions"]["errors"]
    assert errors
    joined = " ".join(errors)
    assert "AGENTS.md" in joined
    assert "directory" in joined.casefold() or "Is a directory" in joined
    # Nothing was destroyed: the blocking directory and its child survive.
    assert agents.is_dir()
    assert (agents / "blocker").read_text() == "x"


# --------------------------------------------------------------------------
# FIX C: automatic project integration at the consent gate
# --------------------------------------------------------------------------

def test_confirm_init_writes_managed_project_pointer(tmp_path, run_cli, monkeypatch):
    """Approved onboarding writes the persistent pointer with no extra command."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "auto-proj"
    proj.mkdir()

    code, out, err = run_cli(
        "onboarding-confirm-init", str(proj), "--approve", "--json"
    )
    assert code == 0, err
    doc = json.loads(out)
    assert "project_integration" in doc
    assert doc["project_integration"]["errors"] == []
    agents = proj / "AGENTS.md"
    assert agents.is_file()
    text = agents.read_text()
    assert "aperio-next:project-instructions:begin" in text
    # The embedded engine is the validated absolute path, not a bare name.
    assert "core-identity" in text


def test_confirm_init_no_project_integration_opt_out(tmp_path, run_cli, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "optout-proj"
    proj.mkdir()
    code, out, err = run_cli(
        "onboarding-confirm-init", str(proj), "--approve",
        "--no-project-integration", "--json",
    )
    assert code == 0, err
    assert not (proj / "AGENTS.md").exists()


def test_confirm_init_preserves_existing_agents_md(tmp_path, run_cli, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "existing-proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# My project rules\n\n- run tests first\n")

    code, out, err = run_cli(
        "onboarding-confirm-init", str(proj), "--approve", "--json"
    )
    assert code == 0, err
    text = (proj / "AGENTS.md").read_text()
    assert text.startswith("# My project rules")
    assert "- run tests first" in text
    assert "aperio-next:project-instructions:begin" in text


def test_confirm_init_pointer_is_idempotent(tmp_path, run_cli, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "idem-proj"
    proj.mkdir()
    run_cli("onboarding-confirm-init", str(proj), "--approve", "--json")
    first = (proj / "AGENTS.md").read_text()
    run_cli("onboarding-confirm-init", str(proj), "--approve", "--json")
    second = (proj / "AGENTS.md").read_text()
    assert first == second
    assert second.count("aperio-next:project-instructions:begin") == 1


def test_reconstruction_ignores_managed_pointer(tmp_path, run_cli, monkeypatch):
    """The pointer must not be analysed as a user-authored claim."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "recon-proj"
    (proj).mkdir()
    (proj / "README.md").write_text("# project\nlanguage: Python\n")

    run_cli("onboarding-confirm-init", str(proj), "--approve", "--json")
    code, out, err = run_cli("onboarding-reconstruct", str(proj), "--json")
    assert code == 0, err
    payload = json.loads(out)
    # Our pointer mentions "plan"/"tasks"/"checkpoint" in prose; none of it
    # should become a documented task or claim conflict.
    assert payload["pending_tasks"] == []
    assert payload["claim_conflicts"] == []


# --------------------------------------------------------------------------
# FIX A: legacy-PATH collision must never leak into skill OR command assets
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host_id,detect_dir,asset_rel",
    [
        ("zcode", ".zcode", (".zcode", "skills", SKILL_ID, "SKILL.md")),
        ("zcode", ".zcode", (".zcode", "commands", f"{SKILL_ID}.md")),
        ("claude-code", ".claude", (".claude", "skills", SKILL_ID, "SKILL.md")),
        ("claude-code", ".claude", (".claude", "commands", f"{SKILL_ID}.md")),
        ("opencode", ".config/opencode",
         (".config", "opencode", "skills", SKILL_ID, "SKILL.md")),
        ("gemini-cli", ".gemini", (".gemini", "skills", SKILL_ID, "SKILL.md")),
        ("gemini-cli", ".gemini", (".gemini", "commands", f"{SKILL_ID}.toml")),
    ],
)
def test_legacy_path_never_leaks_into_skill_or_command(
    host_id, detect_dir, asset_rel, tmp_path, monkeypatch
):
    """Both the skill AND the command asset must embed the verified isolated
    engine, never a same-named legacy ``aperio`` earlier on PATH (FIX A)."""
    home = tmp_path / "home"
    home.mkdir()
    # Ensure the host is "detected" by creating the directory it keys on.
    (home / detect_dir).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy = legacy_dir / "aperio"
    legacy.write_text("#!/bin/sh\n# legacy older aperio\n")
    legacy.chmod(0o755)
    monkeypatch.setenv("PATH", str(legacy_dir))

    isolated = _fake_engine(
        home / ".aperio-next" / "bin" / "aperio", product="aperio-next"
    )
    # resolve_engine_path must also see the isolated install even though the
    # legacy executable is earlier on PATH.
    resolved = resolve_engine_path(validate=True)
    assert str(isolated) in resolved
    assert str(legacy) not in resolved

    report = install_integrations([host_id], home=home, version="1")[0]
    asset = home.joinpath(*asset_rel)
    assert asset.is_file(), (report.installed, report.errors)
    text = asset.read_text(encoding="utf-8")
    assert str(isolated) in text, f"{asset} did not embed the verified engine"
    assert str(legacy) not in text, f"{asset} leaked the legacy PATH engine"


# --------------------------------------------------------------------------
# Expanded host catalog (FIX: universal coverage of installed agent tools)
# --------------------------------------------------------------------------

def test_every_host_id_is_unique_and_catalog_is_stable():
    ids = [h.host_id for h in HOSTS]
    assert len(ids) == len(set(ids)), "duplicate host ids"
    # The catalog must cover the original six plus the Agent Skills hosts
    # that share the ~/.<tool>/skills convention.
    for required in (
        "zcode", "claude-code", "opencode", "codex-cli", "gemini-cli",
        "cursor", "cline", "goose", "kilocode", "continue", "kiro",
        "qoder", "roo", "trae", "junie", "openhands", "augment",
        "autohand", "openclaw", "grok", "hermes", "windsurf", "rovodev",
        "copilot", "crush", "codebuddy",
    ):
        assert required in ids, f"missing host {required}"


def test_agent_skills_hosts_use_the_documented_convention(tmp_path, monkeypatch):
    """Every Agent Skills host must use ~/.<tool>/skills/<SKILL_ID>/SKILL.md
    and be installable into that exact path when the dir exists."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)
    engine = _fake_engine(
        home / ".aperio-next" / "bin" / "aperio", product="aperio-next"
    )
    agent_hosts = [
        "cline", "goose", "kilocode", "continue", "kiro", "qoder", "roo",
        "trae", "junie", "openhands", "augment", "autohand", "openclaw",
        "grok", "hermes", "windsurf", "rovodev", "copilot", "crush",
        "codebuddy",
    ]
    for hid in agent_hosts:
        spec = host_by_id(hid)
        assert spec.user_skill_dir == f".{hid}/skills", hid
        # The skill is the discovery mechanism (no documented command file).
        assert spec.user_command_dir is None, hid
        (home / spec.user_skill_dir).mkdir(parents=True, exist_ok=True)

    reports = install_integrations(agent_hosts, home=home, version="1")
    for r in reports:
        assert r.ok, (r.host_id, r.errors)
        assert len(r.installed) == 1, r.host_id
        skill = home / r.installed[0]
        assert skill.name == "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        assert str(engine) in text  # verified engine embedded
        assert MANAGED_MARK_BEGIN in text


def test_agent_skills_hosts_remove_only_managed_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APERIO_NEXT_HOME", raising=False)
    _fake_engine(
        home / ".aperio-next" / "bin" / "aperio", product="aperio-next"
    )
    spec = host_by_id("hermes")
    (home / spec.user_skill_dir).mkdir(parents=True)
    # A foreign sibling skill must survive.
    foreign = home / spec.user_skill_dir / "aperio-cms" / "SKILL.md"
    foreign.parent.mkdir()
    foreign.write_text("# mine\n")

    reports = install_integrations(["hermes"], home=home, version="1")
    assert reports[0].ok
    reports = remove_integrations(["hermes"], home=home)
    assert reports[0].ok and not reports[0].errors
    assert foreign.exists(), "foreign sibling skill was deleted"
    assert not (home / spec.user_skill_dir / "aperio-next").exists()


# --------------------------------------------------------------------------
# FIX B/E: onboarding-written pointers are recorded for launcher cleanup
# --------------------------------------------------------------------------

def test_install_project_instructions_records_project_in_manifest(
    tmp_path, monkeypatch
):
    """A project pointer written through the engine is added to the isolated
    install's manifest so `aperio-next uninstall` can clean it later."""
    import json

    root = tmp_path / "aperio-next"
    (root / "bin").mkdir(parents=True)
    manifest = root / "install-root.json"
    manifest.write_text(json.dumps({
        "product": "aperio-next", "version": "x",
        "projects": [], "hosts": [],
    }), encoding="utf-8")
    monkeypatch.setenv("APERIO_NEXT_HOME", str(root))

    proj = tmp_path / "proj"
    (proj / ".aperio").mkdir(parents=True)
    from aperio.integrations import install_project_instructions

    report = install_project_instructions(
        proj, engine_path="/abs/aperio", version="1"
    )
    assert report["errors"] == []
    assert report["written"]
    data = json.loads(manifest.read_text("utf-8"))
    assert str(proj.resolve()) in data["projects"]


def test_install_project_instructions_no_manifest_is_harmless(
    tmp_path, monkeypatch
):
    """Without an isolated-install manifest (e.g. pip install) the writer
    still succeeds and records nothing."""
    monkeypatch.setenv("APERIO_NEXT_HOME", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    proj = tmp_path / "proj"
    (proj / ".aperio").mkdir(parents=True)
    from aperio.integrations import install_project_instructions

    report = install_project_instructions(
        proj, engine_path="/abs/aperio", version="1"
    )
    assert report["errors"] == []
    assert (proj / "AGENTS.md").is_file()
