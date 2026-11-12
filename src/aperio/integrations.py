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
    "install_project_instructions",
    "managed_asset_body",
    "remove_integrations",
    "remove_for_host",
    "remove_project_instructions",
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
    # ------------------------------------------------------------------
    # Additional Agent Skills hosts. These use the open cross-tool
    # ``~/.<tool>/skills/<name>/SKILL.md`` convention, the same layout and
    # frontmatter aperio already emits. The catalog of these directories is
    # the one the open ``skills`` CLI (used by Cline via ``cline skill``)
    # targets; ``docs`` names that source rather than claiming a live
    # per-host session. A host only ever receives files when its directory
    # already exists (detect_dirs), so nothing is written to an unused path.
    # ------------------------------------------------------------------
    HostSpec(
        host_id="cline",
        display="Cline",
        user_skill_dir=".cline/skills",
        user_command_dir=None,
        project_skill_dir=".cline/skills",
        detect_dirs=(".cline",),
        detect_executables=("cline",),
        invocation=(
            "the aperio-next skill is discovered from ~/.cline/skills; ask "
            "the agent to use it (Cline loads skills by description)"
        ),
        docs="Cline skills: ~/.cline/skills/<name>/SKILL.md via the open "
             "Agent Skills convention (cline skill add/list forwards to "
             "'npx skills')",
    ),
    HostSpec(
        host_id="goose",
        display="Goose",
        user_skill_dir=".goose/skills",
        user_command_dir=None,
        project_skill_dir=".goose/skills",
        detect_dirs=(".goose",),
        detect_executables=("goose",),
        invocation=(
            "goose discovers skills from ~/.goose/skills (and ~/.agents/"
            "skills); 'goose skills list' lists them"
        ),
        docs="Goose (block) skills: 'goose skills list' reports discovery "
             "from ~/.agents/skills and ~/.goose/skills; Agent Skills "
             "SKILL.md convention",
    ),
    HostSpec(
        host_id="kilocode",
        display="Kilo Code",
        user_skill_dir=".kilocode/skills",
        user_command_dir=None,
        project_skill_dir=".kilocode/skills",
        detect_dirs=(".kilocode", ".kilo"),
        detect_executables=("kilocode", "kilo"),
        invocation=(
            "the aperio-next skill is discovered from ~/.kilocode/skills"
        ),
        docs="Kilo Code: ~/.kilocode/skills (and ~/.kilo/skills) via the "
             "open Agent Skills convention",
    ),
    HostSpec(
        host_id="continue",
        display="Continue",
        user_skill_dir=".continue/skills",
        user_command_dir=None,
        project_skill_dir=".continue/skills",
        detect_dirs=(".continue",),
        detect_executables=("continue", "cn"),
        invocation="the aperio-next skill is discovered from ~/.continue/skills",
        docs="Continue: ~/.continue/skills via the open Agent Skills "
             "convention",
    ),
    HostSpec(
        host_id="kiro",
        display="Kiro",
        user_skill_dir=".kiro/skills",
        user_command_dir=None,
        project_skill_dir=".kiro/skills",
        detect_dirs=(".kiro",),
        detect_executables=("kiro", "kiro-cli"),
        invocation="the aperio-next skill is discovered from ~/.kiro/skills",
        docs="Kiro: ~/.kiro/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="qoder",
        display="Qoder",
        user_skill_dir=".qoder/skills",
        user_command_dir=None,
        project_skill_dir=".qoder/skills",
        detect_dirs=(".qoder",),
        detect_executables=("qoder", "qodercli"),
        invocation="the aperio-next skill is discovered from ~/.qoder/skills",
        docs="Qoder: ~/.qoder/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="roo",
        display="Roo Code",
        user_skill_dir=".roo/skills",
        user_command_dir=None,
        project_skill_dir=".roo/skills",
        detect_dirs=(".roo",),
        detect_executables=("roo",),
        invocation="the aperio-next skill is discovered from ~/.roo/skills",
        docs="Roo Code: ~/.roo/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="trae",
        display="Trae",
        user_skill_dir=".trae/skills",
        user_command_dir=None,
        project_skill_dir=".trae/skills",
        detect_dirs=(".trae",),
        detect_executables=("trae",),
        invocation="the aperio-next skill is discovered from ~/.trae/skills",
        docs="Trae: ~/.trae/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="junie",
        display="JetBrains Junie",
        user_skill_dir=".junie/skills",
        user_command_dir=None,
        project_skill_dir=".junie/skills",
        detect_dirs=(".junie",),
        detect_executables=("junie",),
        invocation="the aperio-next skill is discovered from ~/.junie/skills",
        docs="Junie: ~/.junie/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="openhands",
        display="OpenHands",
        user_skill_dir=".openhands/skills",
        user_command_dir=None,
        project_skill_dir=".openhands/skills",
        detect_dirs=(".openhands",),
        detect_executables=("openhands",),
        invocation=("the aperio-next skill is discovered from "
                    "~/.openhands/skills"),
        docs="OpenHands: ~/.openhands/skills via the open Agent Skills "
             "convention",
    ),
    HostSpec(
        host_id="augment",
        display="Augment Code",
        user_skill_dir=".augment/skills",
        user_command_dir=None,
        project_skill_dir=".augment/skills",
        detect_dirs=(".augment",),
        detect_executables=("augment", "auggie"),
        invocation="the aperio-next skill is discovered from ~/.augment/skills",
        docs="Augment: ~/.augment/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="autohand",
        display="Autohand Code",
        user_skill_dir=".autohand/skills",
        user_command_dir=None,
        project_skill_dir=".autohand/skills",
        detect_dirs=(".autohand",),
        detect_executables=("autohand",),
        invocation="the aperio-next skill is discovered from ~/.autohand/skills",
        docs="Autohand Code: ~/.autohand/skills via the open Agent Skills "
             "convention",
    ),
    HostSpec(
        host_id="openclaw",
        display="OpenClaw",
        user_skill_dir=".openclaw/skills",
        user_command_dir=None,
        project_skill_dir=".openclaw/skills",
        detect_dirs=(".openclaw",),
        detect_executables=("openclaw", "claw"),
        invocation="the aperio-next skill is discovered from ~/.openclaw/skills",
        docs="OpenClaw: ~/.openclaw/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="grok",
        display="Grok CLI",
        user_skill_dir=".grok/skills",
        user_command_dir=None,
        project_skill_dir=".grok/skills",
        detect_dirs=(".grok",),
        detect_executables=("grok",),
        invocation="the aperio-next skill is discovered from ~/.grok/skills",
        docs="Grok: ~/.grok/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="hermes",
        display="Hermes Agent",
        user_skill_dir=".hermes/skills",
        user_command_dir=None,
        project_skill_dir=".hermes/skills",
        detect_dirs=(".hermes",),
        detect_executables=("hermes", "hermes-agent"),
        invocation=(
            "the aperio-next skill is discovered from ~/.hermes/skills "
            "(hermes --skills can narrow the set)"
        ),
        docs="Hermes Agent: ~/.hermes/skills via the open Agent Skills "
             "convention; 'hermes --skills' selects a subset",
    ),
    HostSpec(
        host_id="windsurf",
        display="Windsurf",
        user_skill_dir=".windsurf/skills",
        user_command_dir=None,
        project_skill_dir=".windsurf/skills",
        detect_dirs=(".windsurf",),
        detect_executables=("windsurf",),
        invocation="the aperio-next skill is discovered from ~/.windsurf/skills",
        docs="Windsurf: ~/.windsurf/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="rovodev",
        display="Rovo Dev",
        user_skill_dir=".rovodev/skills",
        user_command_dir=None,
        project_skill_dir=".rovodev/skills",
        detect_dirs=(".rovodev",),
        detect_executables=("acli", "rovodev"),
        invocation="the aperio-next skill is discovered from ~/.rovodev/skills",
        docs="Rovo Dev: ~/.rovodev/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="copilot",
        display="GitHub Copilot",
        user_skill_dir=".copilot/skills",
        user_command_dir=None,
        project_skill_dir=".copilot/skills",
        detect_dirs=(".copilot", ".github"),
        detect_executables=("copilot", "gh"),
        invocation="the aperio-next skill is discovered from ~/.copilot/skills",
        docs="GitHub Copilot: ~/.copilot/skills (and ~/.github/skills) via "
             "the open Agent Skills convention",
    ),
    HostSpec(
        host_id="crush",
        display="Crush",
        user_skill_dir=".crush/skills",
        user_command_dir=None,
        project_skill_dir=".crush/skills",
        detect_dirs=(".crush",),
        detect_executables=("crush",),
        invocation="the aperio-next skill is discovered from ~/.crush/skills",
        docs="Crush: ~/.crush/skills via the open Agent Skills convention",
    ),
    HostSpec(
        host_id="codebuddy",
        display="CodeBuddy",
        user_skill_dir=".codebuddy/skills",
        user_command_dir=None,
        project_skill_dir=".codebuddy/skills",
        detect_dirs=(".codebuddy",),
        detect_executables=("codebuddy", "codebuddy-cli"),
        invocation="the aperio-next skill is discovered from ~/.codebuddy/skills",
        docs="CodeBuddy: ~/.codebuddy/skills via the open Agent Skills convention",
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


def render_command_body(
    spec: HostSpec, *, engine_path: str, version: str
) -> str:
    """Render the slash-command wrapper text for one host.

    ``engine_path`` is embedded exactly like the skill asset so a native
    command invocation can never silently resolve a different ``aperio`` on
    PATH. It is already shell-quoted by :func:`resolve_engine_path` when it
    contains spaces or metacharacters.
    """
    template_name = (
        _COMMAND_TEMPLATE_TOML
        if spec.command_extension == "toml"
        else _COMMAND_TEMPLATE_MD
    )
    text = _load_template(template_name).substitute(
        APERIO_CLI=engine_path, VERSION=version
    )
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

def _isolated_engine_candidates() -> list[Path]:
    """Absolute paths to the Aperio Next isolated engine, most specific first.

    The npm installer creates an isolated engine under
    ``$APERIO_NEXT_HOME`` (default ``~/.aperio-next``) with a shim at
    ``<root>/bin/aperio`` (``aperio.cmd`` on Windows). That install is the
    one Aperio Next owns, so it wins over anything on PATH — a legacy
    ``aperio`` earlier on PATH must never be picked up by our own assets.
    """
    roots: list[Path] = []
    env_root = os.environ.get("APERIO_NEXT_HOME")
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path.home() / ".aperio-next")
    out: list[Path] = []
    for root in roots:
        out.append(root / "bin" / (
            "aperio.cmd" if os.name == "nt" else "aperio"
        ))
    return out


def _engine_is_aperio_next(path: str) -> bool:
    """True when ``path`` runs and reports itself as the aperio-next product.

    Runs ``<path> core-identity --json`` with a short timeout and a bounded
    read. Any failure (missing, not executable, wrong product, crash, hang)
    yields False so callers fall through to the next candidate.
    """
    import json as _json
    import subprocess

    try:
        proc = subprocess.run(
            [path, "core-identity", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        record = _json.loads(proc.stdout)
    except (ValueError, TypeError):
        return False
    return isinstance(record, dict) and record.get("product") == "aperio-next"


def _quote_for_shell(path: str) -> str:
    """Quote ``path`` so it survives in a POSIX shell and in prose.

    Paths with spaces or shell metacharacters are single-quoted; a plain
    path is returned unchanged so generated assets stay readable. Embedded
    single quotes are escaped POSIX-style (``'\''``).
    """
    if path and all(c.isalnum() or c in "-_./\\:@" for c in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def resolve_engine_path(*, validate: bool = False) -> str:
    """Best-effort absolute path of the installed ``aperio`` CLI.

    Resolution order (each candidate validated when ``validate`` is set):

    1. The Aperio Next isolated install (``$APERIO_NEXT_HOME`` or
       ``~/.aperio-next/bin/aperio``) — the install aperio-next owns. This
       is preferred so a same-named legacy executable earlier on PATH is
       never invoked by our own generated assets.
    2. ``aperio`` on PATH (validated to be the aperio-next product when
       ``validate`` is set).
    3. The interpreter's own ``bin/aperio`` sibling (a venv / tool install).
    4. The bare name ``aperio`` (last resort; the calling agent must be able
       to resolve it).

    The returned string is shell-quoted when it contains spaces or special
    characters. A caller that needs a *guaranteed* aperio-next engine (e.g.
    the installer before writing assets) passes ``validate=True`` so a legacy
    executable cannot be embedded.
    """
    candidates: list[str] = []
    for iso in _isolated_engine_candidates():
        if iso.is_file():
            candidates.append(str(iso))
    on_path = shutil.which("aperio")
    if on_path:
        candidates.append(on_path)
    sibling = Path(sys.executable).with_name(
        "aperio.exe" if os.name == "nt" else "aperio"
    )
    if sibling.is_file():
        candidates.append(str(sibling))

    for cand in candidates:
        if not validate or _engine_is_aperio_next(cand):
            return _quote_for_shell(cand)
    # Nothing validated: fall back to the bare name so assets still have a
    # callable token; status/install surface the repair guidance instead.
    return "aperio"


def engine_repair_hint(engine_path: str, *, version: str = "") -> str:
    """Actionable text when a resolved engine is missing or the wrong product."""
    raw = engine_path.strip("'")
    if raw == "aperio":
        return (
            "No validated Aperio Next engine was found. Run "
            "`aperio-next install` (or `aperio-next doctor`) to create one "
            "at ~/.aperio-next/bin/aperio, or add your install's bin dir to "
            "PATH. Do not rely on a bare `aperio` if a different Aperio is "
            "also installed."
        )
    return (
        f"The engine path embedded in these assets ({engine_path}) does not "
        "run as Aperio Next. Re-run `aperio-next install` (or "
        "`aperio-next update`) to regenerate assets with a validated engine "
        "path, then re-register with `aperio integrations install`."
    )


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
    if not path.exists() and not path.is_symlink():
        return False
    # A symlinked target is never followed or silently removed: deleting a
    # link that points elsewhere is ambiguous, so refuse and report it.
    if path.is_symlink():
        raise IntegrationError(
            f"refusing to remove {what}: {path} is a symlink"
        )
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
                body = render_command_body(
                    spec, engine_path=engine_path, version=version
                )
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
    if engine_path is None:
        engine = resolve_engine_path(validate=True)
        # Never silently embed a bare/unvalidated ``aperio``: that is exactly
        # how a legacy install earlier on PATH gets wired into our own assets.
        if engine.strip("'\"") == "aperio":
            raise IntegrationError(engine_repair_hint(engine, version=version))
    else:
        engine = engine_path
    reports: list[InstallReport] = []
    for spec in specs:
        report = install_for_host(
            spec,
            home=home,
            engine_path=engine,
            version=version,
            project_root=project_root,
            scope=scope,
        )
        reports.append(report)
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
    engine_valid: bool = True
    files: dict[str, str] = field(default_factory=dict)
    invocation: str = ""
    docs: str = ""

    def to_dict(self) -> dict:
        return {
            "host": self.host_id,
            "display": self.display,
            "host_detected": self.detected,
            "engine_path": self.engine_path,
            "engine_valid": self.engine_valid,
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
        engine_valid=resolve_engine_path(validate=True) != "aperio",
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


# --------------------------------------------------------------------------
# project-instruction pointer (AGENTS.md / CLAUDE.md owned section)
# --------------------------------------------------------------------------

#: Instruction files agents read automatically in a project. AGENTS.md is the
#: cross-tool convention (Codex, Gemini, many others); CLAUDE.md is Claude
#: Code's. aperio writes ONE owned, marker-delimited section into whichever
#: of these already exists (or AGENTS.md, creating it) — never overwriting
#: the user's own instructions outside the markers.
PROJECT_INSTRUCTION_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

_SECTION_BEGIN = "<!-- aperio-next:project-instructions:begin -->"
_SECTION_END = "<!-- aperio-next:project-instructions:end -->"


def _project_pointer_text(engine_path: str, version: str) -> str:
    body = _load_template("aperio-next-project-instructions.md").substitute(
        APERIO_CLI=engine_path, VERSION=version
    ).rstrip()
    # The template opens with an H1; demote it under the section markers so
    # the file's own heading structure stays intact.
    body = body.replace("# Aperio Next project pointer (managed)",
                        "## Aperio Next project pointer (managed)", 1)
    return f"{_SECTION_BEGIN}\n{body}\n{_SECTION_END}\n"


def _is_owned_section_file(path: Path) -> bool:
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _SECTION_BEGIN in text and _SECTION_END in text


def _upsert_section(existing: str, section: str) -> str:
    """Return ``existing`` with our section replaced/added, all else kept."""
    if _SECTION_BEGIN in existing and _SECTION_END in existing:
        pre, rest = existing.split(_SECTION_BEGIN, 1)
        _mid, post = rest.split(_SECTION_END, 1)
        # Normalize surrounding blank lines so re-install is byte-stable.
        return pre.rstrip("\n") + ("\n\n" if pre.strip() else "") + section + (
            post.lstrip("\n") if post.strip() else ""
        )
    if not existing.strip():
        return section
    return existing.rstrip("\n") + "\n\n" + section


def strip_project_instructions(text: str) -> str:
    """Return ``text`` without Aperio's owned pointer section, if present.

    Used when *analysing* user instruction files (e.g. reconstruction) so the
    machine-written pointer can never be mistaken for a user-authored claim.
    Content outside the markers is untouched.
    """
    if _SECTION_BEGIN not in text or _SECTION_END not in text:
        return text
    stripped = _strip_section(text)
    # ``_strip_section`` returns None only when markers are absent; we already
    # checked they are present.
    return stripped if stripped is not None else text


def _strip_section(existing: str) -> str | None:
    if _SECTION_BEGIN not in existing or _SECTION_END not in existing:
        return None
    pre, rest = existing.split(_SECTION_BEGIN, 1)
    _mid, post = rest.split(_SECTION_END, 1)
    combined = (pre.rstrip("\n") + ("\n" if post.strip() else "") + post.lstrip("\n"))
    return combined.strip("\n") + ("\n" if combined.strip() else "")


def install_project_instructions(
    project_root: Path,
    *,
    engine_path: str,
    version: str,
    files: tuple[str, ...] = (),
) -> dict:
    """Write the Aperio Next pointer into a managed project's instruction files.

    Consent-gated by the caller: only invoked for an already Aperio-managed
    project (``.aperio/`` present) after the user opts in. Writes only the
    owned, marker-delimited section; every other line is preserved verbatim.
    Returns a report dict. Never raises for a foreign/symlinked target: the
    problem is reported so the install summary stays accurate.
    """
    require_managed_project(project_root)
    section = _project_pointer_text(engine_path, version)
    targets = list(files) if files else ["AGENTS.md"]
    # Also update CLAUDE.md only when it ALREADY carries our section (so we
    # never invent a Claude-specific file the user did not have).
    for name in PROJECT_INSTRUCTION_FILES:
        if name not in targets and _is_owned_section_file(project_root / name):
            targets.append(name)

    report = {
        "written": [],
        "section_present": [],
        "errors": [],
        "files": targets,
    }
    for name in targets:
        path = project_root / name
        if path.is_symlink():
            report["errors"].append(
                f"refusing to write {name}: it is a symlink"
            )
            continue
        try:
            existing = path.read_text("utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            report["errors"].append(f"cannot read {path}: {exc}")
            continue
        if path.exists() and section.split("\n", 1)[1][:40] in existing \
                and _SECTION_BEGIN in existing:
            # Already up to date (idempotent fast path); still rewrite to
            # refresh the version line.
            pass
        new_text = _upsert_section(existing, section)
        if new_text == existing:
            report["section_present"].append(str(path))
            continue
        try:
            _atomic_write(path, new_text, what=f"project instructions {name}")
        except IntegrationError as exc:
            report["errors"].append(str(exc))
        else:
            report["written"].append(str(path))
            report["section_present"].append(str(path))
    # Record the project in the isolated install's manifest so a later
    # `aperio-next uninstall` (run by the launcher) cleans this pointer.
    # Without this, a project initialized via onboarding carried a dangling
    # pointer after the engine was removed. Best-effort: never fails init.
    if report["written"]:
        _record_project_in_install_manifest(project_root)
    return report


def _record_project_in_install_manifest(project_root: Path) -> None:
    """Add ``project_root`` to the isolated install's manifest, if present.

    The npm launcher keeps ``<root>/install-root.json`` with a ``projects``
    list it uses to clean project-scope assets on uninstall. A project
    onboarded directly through the engine is appended here so that a later
    launcher-driven uninstall removes the owned pointer instead of leaving
    it pointing at a vanished engine. Silently no-ops when the manifest does
    not exist (e.g. a plain pip/pipx install).
    """
    import json as _json

    roots: list[Path] = []
    env_root = os.environ.get("APERIO_NEXT_HOME")
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path.home() / ".aperio-next")
    target = str(Path(project_root).resolve())
    for root in roots:
        manifest_path = root / "install-root.json"
        try:
            data = _json.loads(manifest_path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("product") != "aperio-next":
            continue
        projects = data.get("projects")
        if not isinstance(projects, list):
            projects = []
        if target in projects:
            return
        projects.append(target)
        data["projects"] = projects
        try:
            _atomic_write(
                manifest_path,
                _json.dumps(data, indent=2) + "\n",
                what="install manifest",
            )
        except IntegrationError:
            pass
        return


def remove_project_instructions(project_root: Path) -> dict:
    """Remove only our owned section from instruction files; keep the rest."""
    report = {"cleaned": [], "errors": []}
    for name in PROJECT_INSTRUCTION_FILES:
        path = project_root / name
        if not path.exists() or path.is_symlink():
            continue
        try:
            existing = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report["errors"].append(f"cannot read {path}: {exc}")
            continue
        stripped = _strip_section(existing)
        if stripped is None:
            continue
        try:
            if stripped.strip():
                _atomic_write(path, stripped, what=f"project instructions {name}")
                report["cleaned"].append(str(path))
            else:
                # The file was only our section (we had created it): remove it.
                path.unlink()
                report["cleaned"].append(f"{path} (removed file we created)")
        except (IntegrationError, OSError) as exc:
            report["errors"].append(str(exc))
    return report


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
