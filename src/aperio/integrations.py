"""Universal agent integrations: install Aperio Next skill assets into the
documented extension points of supported coding-agent hosts.

This is the implementation of the "native skill" layer: for every host that
documents a skills/commands mechanism, aperio writes ONE self-contained
skill asset (the activation protocol in ``integration_assets/``) into the
host's documented location, so the host's agent can activate Aperio Next
natively and invoke the real engine.

Honesty rules (same policy as :mod:`aperio.hostcompat`):

- Only officially documented host locations are used. Nothing here invents
  an interface: every path in ``HOSTS`` is the host's published skills or
  commands directory.
- Installing a skill file is **registration + discoverability**, nothing
  more. Whether the host's agent actually activates the skill and runs the
  engine is a live-session fact this module can never observe, so result
  records carry explicit ``verified`` dimensions and leave activation
  honestly untested. Never claim "activated" from a file write.
- The skill is *cooperative* by nature: it instructs the host's agent to
  run the runtime-enforced ``aperio`` CLI. Enforcement lives in the engine,
  not in the skill text.

Safety rules (same policy as :mod:`aperio.project` / :mod:`aperio.paths`):

- A managed asset carries the ``MANAGED_MARK_BEGIN`` / ``MANAGED_MARK_END``
  comment pair. A existing file at the target that lacks the pair is
  FOREIGN content — install refuses and names the file; remove refuses
  too. User content can never be clobbered or silently adopted.
- Writes are atomic: exclusive temp file in the destination directory,
  then ``os.replace``. A symlinked target or destination directory is
  refused, live or dangling.
- Removal deletes only the managed files aperio created, then an aperio
  owned directory if it is empty. Host directories themselves are never
  deleted.
- Idempotent: reinstalling replaces only aperio-managed content (upgrade
  path), everything else is left untouched.

Scope layout:

- ``user`` scope — the host's global skills/commands directory (applies to
  every project).
- ``project`` scope — the host's workspace skills directory inside a
  project that is already Aperio-managed (``.aperio/`` state present).
  Refusing unmanaged projects keeps integration files out of random
  directories; managed projects already opted in via onboarding consent.
"""

from __future__ import annotations

import os
import shutil
import string
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from aperio.errors import AperioError, StateError, ValidationError

__all__ = [
    "HOSTS",
    "HostSpec",
    "IntegrationError",
    "InstallReport",
    "RemoveReport",
    "StatusRow",
    "host_status",
    "install_for_host",
    "install_integrations",
    "managed_asset_body",
    "remove_integrations",
    "remove_for_host",
    "resolve_engine_path",
]

INTEGRATION_FORMAT = "aperio-host-integration"
INTEGRATION_VERSION = 1

#: Identity of the managed asset. The skill directory name and the command
#: file stem are both ``SKILL_ID`` so every host exposes the same handle.
SKILL_ID = "aperio-next"

MANAGED_MARK_BEGIN = "<!-- aperio-next:managed v1 -->"
MANAGED_MARK_END = "<!-- /apario-next:managed v1 -->"

_ASSETS_SUBDIR = "integration_assets"
_SKILL_TEMPLATE = "aperio-next-skill.md"
_COMMAND_TEMPLATE_MD = "aperio-next-command.md"
_COMMAND_TEMPLATE_TOML = "aperio-next-command.toml"


class IntegrationError(AperioError):
    """Raised when an integration operation is refused or fails."""


@dataclass(frozen=True)
class HostSpec:
    """One host's documented extension points.

    All directory fields are ``~``-relative for user scope, or
    project-root-relative for project scope. ``detect_dirs`` and
    ``detect_executables`` are the passive presence signals reported by
    :func:`host_status` — checking a directory's existence never writes
    anything. ``command_extension`` selects the slash-command format the
    host documents (``md`` or ``toml``).
    """

    host_id: str
    display: str
    #: User-scope directory holding ``<SKILL_ID>/SKILL.md`` (None = the
    #: host documents no user-scope skills directory).
    user_skill_dir: str | None
    #: User-scope directory holding ``<SKILL_ID>.<command_extension>`` as a
    #: slash command (None = no documented command mechanism).
    user_command_dir: str | None
    #: Project-scope skills directory inside a managed project root.
    project_skill_dir: str | None
    #: Passive presence signals (directories, relative to home).
    detect_dirs: tuple[str, ...]
    #: Passive presence signals (executable names looked up on PATH).
    detect_executables: tuple[str, ...]
    #: How the user activates the integration in this host (truthful text).
    invocation: str
    #: Where the host documents this mechanism.
    docs: str
    command_extension: str = "md"
    note: str = ""


#: The documented host catalog. Every path below is that host's published
#: skills/commands location (verified against official documentation
#: 2026-11-12: opencode.ai/docs, learn.chatgpt.com/docs (Codex),
#: geminicli.com/docs, cursor.com/docs, code.claude.com/docs, and ZCode's
#: bundled configuration guide). Codex standardized on the cross-tool
#: ``~/.agents/skills`` path; several other hosts also scan it as an alias,
#: so a Codex install makes the skill visible there too — reported per host
#: from each host's own documented path. Hosts whose mechanism aperio has
#: not verified against a live session still get honest ``invocation`` text.
HOSTS: tuple[HostSpec, ...] = (
    HostSpec(
        host_id="zcode",
        display="ZCode",
        user_skill_dir=".zcode/skills",
        user_command_dir=".zcode/commands",
        project_skill_dir=".zcode/skills",
        detect_dirs=(".zcode",),
        detect_executables=("zcode",),
        invocation=(
            "mention the aperio-next skill, or run /aperio-next in the "
            "input box"
        ),
        docs="ZCode bundled configuration guide: user skills at "
             "~/.zcode/skills/<name>/SKILL.md (frontmatter keys name, "
             "description, when_to_use, license, metadata; description "
             "over 1024 chars is dropped), commands at "
             "~/.zcode/commands/<name>.md; ~/.agents/* scanned as aliases",
    ),
    HostSpec(
        host_id="claude-code",
        display="Claude Code",
        user_skill_dir=".claude/skills",
        user_command_dir=".claude/commands",
        project_skill_dir=".claude/skills",
        detect_dirs=(".claude",),
        detect_executables=("claude",),
        invocation=(
            "type /aperio-next, or the skill auto-triggers when the "
            "request matches its description"
        ),
        docs="Claude Code docs (code.claude.com/docs/en/skills, "
             "/slash-commands): personal skills at "
             "~/.claude/skills/<name>/SKILL.md, personal commands at "
             "~/.claude/commands/<name>.md",
    ),
    HostSpec(
        host_id="opencode",
        display="OpenCode",
        user_skill_dir=".config/opencode/skills",
        user_command_dir=".config/opencode/commands",
        project_skill_dir=".opencode/skills",
        detect_dirs=(".config/opencode", ".opencode"),
        detect_executables=("opencode",),
        invocation=(
            "type /aperio-next (command), load explicitly with "
            "@aperio-next, or let the skill auto-trigger"
        ),
        docs="OpenCode docs (opencode.ai/docs/skills, /commands): global "
             "skills at ~/.config/opencode/skills/, commands at "
             "~/.config/opencode/commands/ (plural; singular legacy also "
             "discovered), project scope in .opencode/",
    ),
    HostSpec(
        host_id="codex-cli",
        display="OpenAI Codex CLI",
        user_skill_dir=".agents/skills",
        user_command_dir=None,
        project_skill_dir=None,
        detect_dirs=(".codex", ".agents"),
        detect_executables=("codex",),
        invocation=(
            "type $aperio-next (skill mention) or /skills to list; skills "
            "also auto-trigger when a task matches the description"
        ),
        docs="Codex docs (learn.chatgpt.com/docs/build-skills): user skills "
             "at $HOME/.agents/skills, repo skills at .agents/skills; "
             "custom prompts (~/.codex/prompts) are documented deprecated",
        note="Codex scans the cross-tool ~/.agents/skills path; the same "
             "file is visible to other hosts that alias that path",
    ),
    HostSpec(
        host_id="gemini-cli",
        display="Gemini CLI",
        user_skill_dir=".gemini/skills",
        user_command_dir=".gemini/commands",
        project_skill_dir=None,
        detect_dirs=(".gemini",),
        detect_executables=("gemini",),
        invocation=(
            "type /aperio-next (custom command) or approve the "
            "activate_skill prompt when a task matches"
        ),
        docs="Gemini CLI docs (geminicli.com/docs/cli/custom-commands, "
             "/skills): user skills at ~/.gemini/skills/ (alias "
             "~/.agents/skills/), custom commands as TOML at "
             "~/.gemini/commands/; skills support announced in preview",
        command_extension="toml",
    ),
    HostSpec(
        host_id="cursor",
        display="Cursor",
        user_skill_dir=".cursor/skills",
        user_command_dir=None,
        project_skill_dir=".cursor/skills",
        detect_dirs=(".cursor",),
        detect_executables=("cursor-agent", "cursor"),
        invocation=(
            "type /aperio-next or @-select the skill; also auto-triggers "
            "on matching requests"
        ),
        docs="Cursor docs (cursor.com/docs/skills): project skills at "
             ".cursor/skills/<name>/SKILL.md, user skills at "
             "~/.cursor/skills/; global ~/.cursor/commands is not "
             "documented, so commands are not installed for Cursor",
    ),
)


def host_by_id(host_id: str) -> HostSpec:
    for spec in HOSTS:
        if spec.host_id == host_id:
            return spec
    raise IntegrationError(
        f"unknown host {host_id!r}; known hosts: "
        + ", ".join(s.host_id for s in HOSTS)
    )


# --------------------------------------------------------------------------
# asset rendering
# --------------------------------------------------------------------------

def _load_template(name: str) -> string.Template:
    # Same loading pattern as skills.builtin_skill_packs: a data subpackage
    # directory under the `aperio` package, shipped via package-data.
    return string.Template(
        resources.files("aperio")
        .joinpath(_ASSETS_SUBDIR, name)
        .read_text(encoding="utf-8")
    )


_HOST_INVOCATION = {
    "zcode": "Activated in ZCode: mention this skill, or run /aperio-next.",
    "claude-code": (
        "Activated in Claude Code: type /aperio-next, or the skill "
        "auto-triggers when a request matches the description below."
    ),
    "opencode": (
        "Activated in OpenCode: type /aperio-next, load explicitly with "
        "@aperio-next, or let this skill auto-trigger."
    ),
    "codex-cli": (
        "Activated in Codex CLI: type $aperio-next, or the skill "
        "auto-triggers when a task matches the description below."
    ),
    "gemini-cli": (
        "Activated in Gemini CLI: type /aperio-next, or approve the "
        "activate_skill prompt when a task matches the description below."
    ),
    "cursor": (
        "Activated in Cursor: type /aperio-next, @-select this skill, or "
        "let it auto-trigger on a matching request."
    ),
}


def render_skill_body(
    spec: HostSpec, *, engine_path: str, version: str
) -> str:
    """Render the full SKILL.md text for one host."""
    invocation = _HOST_INVOCATION.get(spec.host_id, "")
    text = _load_template(_SKILL_TEMPLATE).substitute(
        APERIO_CLI=engine_path,
        VERSION=version,
        HOST_INVOCATION=invocation,
    )
    return text.rstrip() + "\n"


def render_command_body(spec: HostSpec, *, version: str) -> str:
    """Render the slash-command wrapper text for one host."""
    template_name = (
        _COMMAND_TEMPLATE_TOML
        if spec.command_extension == "toml"
        else _COMMAND_TEMPLATE_MD
    )
    text = _load_template(template_name).substitute(VERSION=version)
    return text.rstrip() + "\n"


def managed_asset_body(text: str) -> str:
    """Attach the managed markers to an asset body (idempotent)."""
    if MANAGED_MARK_BEGIN in text:
        return text
    return (
        text.rstrip()
        + f"\n\n{MANAGED_MARK_BEGIN}\n{MANAGED_MARK_END}\n"
    )


def is_managed_asset(path: Path) -> bool:
    """True when ``path`` exists and carries aperio's managed markers."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return MANAGED_MARK_BEGIN in content and MANAGED_MARK_END in content


# --------------------------------------------------------------------------
# engine path resolution
# --------------------------------------------------------------------------

def resolve_engine_path() -> str:
    """Best-effort absolute path of the installed ``aperio`` CLI.

    Prefers PATH, then the interpreter's own bin directory (a venv/tool
    install), then the bare name. The result is embedded into skill assets
    so an agent on a restricted PATH can still call the engine.
    """
    found = shutil.which("aperio")
    if found:
        return found
    candidate = Path(sys.executable).with_name("aperio")
    if candidate.is_file():
        return str(candidate)
    return "aperio"


# --------------------------------------------------------------------------
# atomic, ownership-checked writes
# --------------------------------------------------------------------------

def _reject_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise IntegrationError(
            f"refusing to write {what}: {path} is a symlink"
        )


def _atomic_write(path: Path, text: str, *, what: str) -> None:
    """Replace ``path`` atomically; refuses symlinked target or parent dir."""
    _reject_symlink(path, what)
    directory = path.parent
    _reject_symlink(directory, f"{what} directory")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".aperio-tmp", dir=str(directory)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise IntegrationError(f"cannot write {what} to {path}: {exc}") from None


def _prepare_managed_file(path: Path, text: str, *, what: str) -> bool:
    """Create or replace ``path`` under the ownership rules.

    Returns True when the file was written. An existing file without
    aperio's managed markers is foreign content and is never touched.
    """
    if path.exists():
        if not is_managed_asset(path):
            raise IntegrationError(
                f"refusing to {what}: {path} exists and was not created by "
                f"aperio (no {MANAGED_MARK_BEGIN} marker); rename it or "
                f"remove it yourself, then re-run"
            )
    else:
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IntegrationError(
                f"cannot create {what} directory {directory}: {exc}"
            ) from None
        _reject_symlink(directory, f"{what} directory")
    _atomic_write(path, managed_asset_body(text), what=what)
    return True


def _delete_managed_file(path: Path, *, what: str) -> bool:
    if not path.exists():
        return False
    if not is_managed_asset(path):
        raise IntegrationError(
            f"refusing to remove {what}: {path} was not created by aperio "
            f"(no {MANAGED_MARK_BEGIN} marker)"
        )
    path.unlink()
    return True


def _prune_owned_dirs(paths: list[Path]) -> list[str]:
    """Remove aperio-owned directories that are now empty (deepest first).

    Only directories whose name is ``SKILL_ID`` (or that contain nothing
    but it) are removed; the host's own directories are left in place.
    """
    removed: list[str] = []
    for directory in sorted(
        {p.parent for p in paths}, key=lambda d: len(d.parts), reverse=True
    ):
        try:
            next(directory.iterdir())
        except (OSError, StopIteration):
            pass
        else:
            continue  # not empty — leave it
        if directory.name == SKILL_ID:
            try:
                directory.rmdir()
                removed.append(str(directory))
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------
# install / remove / status
# --------------------------------------------------------------------------

#: Install scopes. ``user`` = the host's global extension points; ``project``
#: = a managed project's workspace extension points; ``both`` = both.
SCOPES: tuple[str, ...] = ("user", "project", "both")


def _targets_for(
    spec: HostSpec,
    *,
    home: Path,
    project_root: Path | None,
    scope: str,
) -> list[tuple[Path, str, str]]:
    """(path, kind, scope) triples for one host at the requested scopes."""
    if scope not in SCOPES:
        raise ValidationError(
            f"invalid scope {scope!r}; expected one of {', '.join(SCOPES)}"
        )
    include_user = scope in ("user", "both")
    include_project = scope in ("project", "both")
    targets: list[tuple[Path, str, str]] = []
    if include_user and spec.user_skill_dir:
        targets.append((
            home / spec.user_skill_dir / SKILL_ID / "SKILL.md",
            "skill",
            "user",
        ))
    if include_user and spec.user_command_dir:
        targets.append((
            home / spec.user_command_dir
            / f"{SKILL_ID}.{spec.command_extension}",
            "command",
            "user",
        ))
    if include_project and project_root is not None and spec.project_skill_dir:
        targets.append((
            project_root / spec.project_skill_dir / SKILL_ID / "SKILL.md",
            "skill",
            "project",
        ))
    return targets


@dataclass
class InstallReport:
    host_id: str
    display: str
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    engine_path: str = ""
    invocation: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "host": self.host_id,
            "display": self.display,
            "ok": self.ok,
            "installed": self.installed,
            "skipped": self.skipped,
            "engine_path": self.engine_path,
            "invocation": self.invocation,
            "warnings": self.warnings,
            "errors": self.errors,
            # Honest dimension: file registration is verified; activation
            # is a live-session fact this command cannot observe.
            "verification": {
                "registered_files_written": bool(self.installed),
                "skill_activated_in_live_session": False,
                "note": (
                    "registration verified by managed-marker file writes; "
                    "activation must be tested inside a live host session"
                ),
            },
        }


def install_for_host(
    spec: HostSpec,
    *,
    home: Path,
    engine_path: str,
    version: str,
    project_root: Path | None = None,
    scope: str = "user",
) -> InstallReport:
    """Install this host's managed assets. Idempotent; never clobbers."""
    report = InstallReport(
        host_id=spec.host_id,
        display=spec.display,
        engine_path=engine_path,
        invocation=spec.invocation,
    )
    for path, kind, target_scope in _targets_for(
        spec, home=home, project_root=project_root, scope=scope
    ):
        if (
            target_scope == "user"
            and not any((home / d).exists() for d in spec.detect_dirs)
            and not any(shutil.which(e) for e in spec.detect_executables)
        ):
            report.skipped.append(
                f"{path} (host {spec.host_id} not detected on this machine)"
            )
            continue
        try:
            if kind == "skill":
                body = render_skill_body(
                    spec, engine_path=engine_path, version=version
                )
            else:
                body = render_command_body(spec, version=version)
            _prepare_managed_file(path, body, what=f"{spec.host_id} {kind}")
        except IntegrationError as exc:
            report.errors.append(str(exc))
        else:
            report.installed.append(str(path))
    if not report.installed and not report.errors and not report.skipped:
        report.warnings.append(
            f"host {spec.host_id} documents no installable location for "
            f"this version"
        )
    return report


def install_integrations(
    host_ids: list[str] | None,
    *,
    home: Path,
    version: str,
    project_root: Path | None = None,
    engine_path: str | None = None,
    scope: str = "user",
) -> list[InstallReport]:
    """Install managed assets for the named hosts (default: all)."""
    specs = (
        [host_by_id(h) for h in host_ids] if host_ids else list(HOSTS)
    )
    engine = engine_path or resolve_engine_path()
    reports: list[InstallReport] = []
    for spec in specs:
        reports.append(
            install_for_host(
                spec,
                home=home,
                engine_path=engine,
                version=version,
                project_root=project_root,
                scope=scope,
            )
        )
    return reports


@dataclass
class RemoveReport:
    host_id: str
    display: str
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    pruned_dirs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "host": self.host_id,
            "display": self.display,
            "ok": self.ok,
            "removed": self.removed,
            "kept": self.kept,
            "pruned_dirs": self.pruned_dirs,
            "errors": self.errors,
        }


def remove_for_host(
    spec: HostSpec,
    *,
    home: Path,
    project_root: Path | None = None,
    scope: str = "user",
) -> RemoveReport:
    """Remove only aperio-managed files for this host."""
    report = RemoveReport(host_id=spec.host_id, display=spec.display)
    touched: list[Path] = []
    for path, kind, _target_scope in _targets_for(
        spec, home=home, project_root=project_root, scope=scope
    ):
        if not path.exists():
            continue
        try:
            if _delete_managed_file(path, what=f"{spec.host_id} {kind}"):
                report.removed.append(str(path))
                touched.append(path)
            else:
                report.kept.append(str(path))
        except IntegrationError as exc:
            report.errors.append(str(exc))
    report.pruned_dirs = _prune_owned_dirs(touched)
    return report


def remove_integrations(
    host_ids: list[str] | None,
    *,
    home: Path,
    project_root: Path | None = None,
    scope: str = "user",
) -> list[RemoveReport]:
    specs = [host_by_id(h) for h in host_ids] if host_ids else list(HOSTS)
    return [
        remove_for_host(s, home=home, project_root=project_root, scope=scope)
        for s in specs
    ]


@dataclass
class StatusRow:
    """One host's integration state, machine-verifiable dimensions only."""

    host_id: str
    display: str
    detected: bool
    engine_path: str
    files: dict[str, str] = field(default_factory=dict)
    invocation: str = ""
    docs: str = ""

    def to_dict(self) -> dict:
        return {
            "host": self.host_id,
            "display": self.display,
            "host_detected": self.detected,
            "engine_path": self.engine_path,
            "files": self.files,
            "invocation": self.invocation,
            "docs": self.docs,
            "verification": {
                # Honest: only file-level facts are machine-checkable here.
                "registered_files_written": any(
                    v == "managed" for v in self.files.values()
                ),
                "skill_activated_in_live_session": False,
                "note": (
                    "activation/engine-invocation/workflow/persistence are "
                    "live-session dimensions; test them inside the host "
                    "(see docs/integrations.md)"
                ),
            },
        }


def host_status(
    spec: HostSpec,
    *,
    home: Path,
    project_root: Path | None = None,
    scope: str = "user",
) -> StatusRow:
    """Read-only per-host status. Never writes."""
    detected = any((home / d).exists() for d in spec.detect_dirs) or any(
        shutil.which(e) for e in spec.detect_executables
    )
    files: dict[str, str] = {}
    for path, kind, _target_scope in _targets_for(
        spec, home=home, project_root=project_root, scope=scope
    ):
        if not path.exists():
            files[str(path)] = "absent"
        elif is_managed_asset(path):
            files[str(path)] = "managed"
        else:
            files[str(path)] = "foreign"
    return StatusRow(
        host_id=spec.host_id,
        display=spec.display,
        detected=detected,
        engine_path=resolve_engine_path(),
        files=files,
        invocation=spec.invocation,
        docs=spec.docs,
    )


def integrations_status(
    *,
    home: Path,
    project_root: Path | None = None,
    scope: str = "user",
) -> list[StatusRow]:
    return [
        host_status(s, home=home, project_root=project_root, scope=scope)
        for s in HOSTS
    ]


def require_managed_project(project_root: Path) -> Path:
    """Project-scope installs are allowed only in Aperio-managed projects."""
    apario_dir = project_root / ".aperio"
    if not apario_dir.is_dir():
        raise StateError(
            f"{project_root} is not an Aperio-managed project (no .aperio/ "
            "state); run 'aperio next' / onboarding first, or install at "
            "user scope"
        )
    return apario_dir
