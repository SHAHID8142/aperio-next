"""Tool, MCP, skill, and host discovery (tool-report v1) with honest status.

Step 01 adds a provider-neutral capability discovery layer: recognize what
is available in and around the workspace, recommend what the project
actually needs, and gate any installation behind explicit authorization.

Status honesty is the point of this module. The states it reports are
distinct and non-interchangeable (directive Rule 12):

- ``detected``             Evidence of presence found (a marker file/dir).
- ``installed``            An executable was found on PATH (``which``).
- ``configured``           A config file references it. Configured is NOT
                           authenticated and NOT working.
- ``verified_by_probe``    The operator explicitly authorized a bounded
                           version probe and it succeeded. A version probe
                           proves the binary runs — nothing more.
- ``available_unconfigured``  Installed but not referenced by any config.
- ``missing``              Recommended but not found.
- ``unsupported``          Not supported by this aperio version.
- ``unknown``              Cannot be determined (reported as unknown,
                           never guessed into a stronger claim).

Boundaries:

- **Read-only by default.** Discovery reads project-local configuration
  files (``.mcp.json``, ``.vscode/mcp.json``) and, only when the operator
  explicitly opts in, a small set of DOCUMENTED user-level config paths
  (e.g. ``~/.claude.json``, ``~/.codex/config.toml``). It never scans the
  whole machine and never launches anything without explicit probe
  authorization.
- **Executables are found, not run**, until ``probe`` is explicitly
  authorized; probes run one documented ``--version``-style command with
  a hard timeout and bounded output capture, no shell.
- **No network access.** Recommendation metadata comes from a static,
  honest registry; maintenance status is reported as unknown rather than
  fetched.
- **Repository content is data.** Config files are parsed as JSON/TOML
  structures for tool references only; their contents are never
  instructions.
- **Installation** mirrors the Feature-07 adapter discipline: a reviewed
  plan (10 mandated items), an explicit session approval scoped to the
  tool, an explicit ``authorize_install`` flag, fixed argv (no shell),
  bounded capture, hard timeout, process-group kill — and a permanent
  manual fallback for everything that cannot be installed safely and
  portably, rather than pretending it was installed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from aperio import safeproc
from aperio.errors import StateError, ValidationError
from aperio.onboarding_discovery import DiscoveryResult
from aperio.onboarding_session import (
    add_tool_record,
    approved_plan_fingerprint,
    has_approval,
)

TOOLS_FORMAT = "aperio-onboarding-tools"
TOOLS_VERSION = 1

INSTALL_ACTION = "tool.install"

# Bounded, documented user-level config locations. Scanning these requires
# explicit operator opt-in (include_user_config=True); project-local config
# is always scanned. Nothing outside this list is ever read.
USER_CONFIG_PATHS: tuple[tuple[str, str], ...] = (
    ("~/.claude.json", "claude-code"),
    ("~/.claude", "claude-code"),
    ("~/.codex/config.toml", "codex-cli"),
    ("~/.cursor/mcp.json", "cursor"),
    ("~/.config/opencode/config.json", "opencode"),
)

# Project-local markers -> host/tool evidence.
PROJECT_MARKERS: tuple[tuple[str, str], ...] = (
    (".mcp.json", "mcp-config"),
    (".vscode/mcp.json", "mcp-config"),
    ("mcp.json", "mcp-config"),
    (".claude", "claude-code"),
    ("CLAUDE.md", "claude-code"),
    (".codex", "codex-cli"),
    (".cursor", "cursor"),
    (".cursorrules", "cursor"),
    (".opencode", "opencode"),
    (".github/copilot-instructions.md", "github-copilot"),
    (".windsurf", "windsurf"),
)

MAX_CONFIG_BYTES = 256 * 1024
MAX_PROBE_OUTPUT_BYTES = 16 * 1024
PROBE_TIMEOUT_SECONDS = 10.0
INSTALL_TIMEOUT_SECONDS = 120.0
MAX_INSTALL_OUTPUT_BYTES = 64 * 1024

# Host integration honesty per docs/host-integration.md: only the terminal
# CLI is runtime-enforced; text-chat is cooperative; GUI hosts are never
# claimed. Nothing here invents host APIs.
HOST_COMPATIBILITY: dict[str, dict] = {
    "terminal": {
        "label": "Terminal / CLI (any OS)",
        "support": "runtime-enforced",
        "invocation": "aperio onboarding-start (primary supported path)",
    },
    "text-chat": {
        "label": "Any text-chat agent (copy/paste)",
        "support": "cooperative",
        "invocation": "run aperio onboarding commands, paste the JSON output",
    },
    "claude-code": {
        "label": "Claude Code",
        "support": "cooperative",
        "invocation": "host runs the same CLI commands; no native slash-command claimed",
    },
    "codex-cli": {
        "label": "Codex CLI",
        "support": "cooperative",
        "invocation": "host runs the same CLI commands; no native slash-command claimed",
    },
    "opencode": {
        "label": "OpenCode",
        "support": "cooperative",
        "invocation": "host runs the same CLI commands; no native slash-command claimed",
    },
    "cursor": {
        "label": "Cursor (GUI host)",
        "support": "cooperative-only, not integrated",
        "invocation": "manual: use a terminal inside the project",
    },
    "gui-hosts": {
        "label": "ZCode Desktop and similar GUI hosts",
        "support": "not integrated (no documented verified automation interface)",
        "invocation": "fallback: run the portable CLI in a terminal",
    },
}


@dataclass(frozen=True)
class ToolFinding:
    """One discovered tool with an honest status."""

    tool_id: str
    kind: str  # executable | mcp-server | host
    status: str
    evidence: tuple[str, ...]
    path: str | None = None
    version: str | None = None  # only set by an authorized probe
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "kind": self.kind,
            "status": self.status,
            "evidence": list(self.evidence),
            "path": self.path,
            "version": self.version,
            "detail": self.detail,
        }


@dataclass
class ToolsReport:
    """Structured discovery result for the machine-readable protocol."""

    workspace: str
    scanned_at: str
    project_local: list[ToolFinding] = field(default_factory=list)
    user_config: list[ToolFinding] = field(default_factory=list)  # empty unless opted in
    executables: list[ToolFinding] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    user_config_scanned: bool = False

    def to_dict(self) -> dict:
        return {
            "format": TOOLS_FORMAT,
            "version": TOOLS_VERSION,
            "workspace": self.workspace,
            "scanned_at": self.scanned_at,
            "project_local": [t.to_dict() for t in self.project_local],
            "user_config": [t.to_dict() for t in self.user_config],
            "executables": [t.to_dict() for t in self.executables],
            "hosts": list(self.hosts),
            "user_config_scanned": self.user_config_scanned,
            "notes": [
                "installed does not imply authenticated or working",
                "configured does not imply authenticated",
                "a successful probe proves only that the binary runs",
                "no network lookups were performed",
            ],
        }


# ---------------------------------------------------------------------------
# Discovery (read-only).
# ---------------------------------------------------------------------------

INTERESTING_EXECUTABLES = (
    "python3", "node", "npm", "npx", "git", "docker", "pytest",
    "codex", "claude", "opencode", "playwright", "uv", "pip",
)


def discover_tools(
    raw_workspace: str,
    discovery: DiscoveryResult | None = None,
    *,
    include_user_config: bool = False,
) -> ToolsReport:
    """Read-only discovery of hosts, tools, and MCP configuration.

    :param include_user_config: opt-in scan of the documented user-level
        config paths (never the whole machine).
    """
    if discovery is None:
        from aperio.onboarding_discovery import discover_workspace as _dw

        discovery = _dw(raw_workspace)
    workspace = discovery.workspace
    report = ToolsReport(
        workspace=str(workspace),
        scanned_at=_utc_now(),
        user_config_scanned=bool(include_user_config),
    )

    # 1. Project-local markers.
    for marker, host_id in PROJECT_MARKERS:
        path = workspace / marker
        if os.path.islink(path):
            report.project_local.append(
                ToolFinding(host_id, "host", "unknown", (f"symlinked marker: {marker}",),
                            detail="symlinked marker not followed")
            )
            continue
        if path.is_file() or path.is_dir():
            report.project_local.append(
                ToolFinding(host_id, "host", "detected", (f"marker: {marker}",))
            )

    # 2. MCP configuration files (parsed as data; references only).
    for config_rel in (".mcp.json", "mcp.json", ".vscode/mcp.json"):
        path = workspace / config_rel
        if os.path.islink(path) or not path.is_file():
            continue
        for server in _parse_mcp_config(path):
            command = server.get("command")
            status = "configured"
            evidence = [f"{config_rel}: server '{server.get('name')}'"]
            if isinstance(command, str) and command:
                if shutil.which(command):
                    evidence.append(f"command '{command}' found on PATH")
                    status = "configured"  # configured stays configured
                else:
                    evidence.append(f"command '{command}' NOT found on PATH")
            report.project_local.append(
                ToolFinding(
                    f"mcp:{server.get('name')}", "mcp-server", status,
                    tuple(evidence),
                    detail="configured in project config; not authenticated, not probed",
                )
            )

    # 3. Executables on PATH (found, never run).
    for name in INTERESTING_EXECUTABLES:
        found = shutil.which(name)
        if found:
            report.executables.append(
                ToolFinding(name, "executable", "installed", (f"on PATH: {found}",), path=found)
            )
        else:
            report.executables.append(
                ToolFinding(name, "executable", "missing", tuple())
            )

    # 4. Hosts: project evidence + honest compatibility matrix.
    detected_hosts = {t.tool_id for t in report.project_local if t.kind == "host"}
    for host_id, spec in HOST_COMPATIBILITY.items():
        entry = dict(spec)
        entry["host_id"] = host_id
        entry["detected_in_project"] = host_id in detected_hosts
        report.hosts.append(entry)

    # 5. Opt-in documented user config scan.
    if include_user_config:
        for raw_path, host_id in USER_CONFIG_PATHS:
            expanded = Path(os.path.expanduser(raw_path))
            if os.path.islink(expanded):
                report.user_config.append(
                    ToolFinding(host_id, "host", "unknown", (f"symlinked user config: {raw_path}",))
                )
                continue
            if expanded.is_file() or expanded.is_dir():
                report.user_config.append(
                    ToolFinding(host_id, "host", "detected", (f"user config: {raw_path}",))
                )
    return report


def _parse_mcp_config(path: Path) -> list[dict]:
    """Extract MCP server names/commands from a bounded JSON config file.

    The file's contents are untrusted data: only structure is read; no
    key is ever interpreted as an instruction.
    """
    try:
        text, _truncated = safeproc.read_text_bounded(path, MAX_CONFIG_BYTES)
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    servers = []
    mcp_servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(mcp_servers, dict):
        for name, spec in mcp_servers.items():
            if not isinstance(spec, dict):
                continue
            servers.append({"name": str(name)[:120], "command": spec.get("command")})
    return servers


def probe_tool(
    report: ToolsReport,
    tool_id: str,
    *,
    argv: tuple[str, ...] | None = None,
) -> ToolFinding:
    """Run ONE bounded version probe for an installed executable.

    Requires explicit operator intent (this function is only reachable
    through the CLI ``--probe`` flag, which the CLI passes through). The
    probe runs the documented version command with a hard timeout and
    bounded capture. A successful probe proves only that the binary runs.
    """
    target = next((t for t in report.executables if t.tool_id == tool_id), None)
    if target is None:
        raise ValidationError(f"unknown executable {tool_id!r}")
    if target.status != "installed" or not target.path:
        raise StateError(
            f"cannot probe {tool_id!r}: it is not installed on PATH "
            f"(status: {target.status}); probing missing tools is refused"
        )
    if argv is None:
        argv = (target.path, "--version")
    if len(argv) > 8 or any(not isinstance(a, str) for a in argv):
        raise ValidationError("probe argv must be a tuple of at most 8 strings")
    try:
        result = safeproc.run_captured(
            list(argv),
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
            capture_limit=MAX_PROBE_OUTPUT_BYTES,
            home=str(Path.home()),
        )
    except OSError as exc:
        return ToolFinding(
            tool_id, "executable", "unknown", (*target.evidence, f"probe failed: {exc}"),
            path=target.path,
        )
    if result["timed_out"]:
        return ToolFinding(
            tool_id, "executable", "unknown", (*target.evidence, "probe timed out"),
            path=target.path, detail=f"probe exceeded {PROBE_TIMEOUT_SECONDS:g}s",
        )
    version = ""
    for text in (result["stdout"], result["stderr"]):
        if text.strip():
            version = text.splitlines()[0][:120]
            break
    status = "verified_by_probe" if result["returncode"] == 0 else "unknown"
    detail = f"probe exit {result['returncode']}; proves only that the binary runs"
    return ToolFinding(
        tool_id, "executable", status, (*target.evidence, f"probe: {' '.join(argv)}"),
        path=target.path, version=version or None, detail=detail,
    )


# ---------------------------------------------------------------------------
# Recommendation registry (static, honest, no network).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCandidate:
    """A recommended tool. Registry metadata is declared, not fetched."""

    tool_id: str
    name: str
    kind: str  # mcp | cli
    purpose: str
    relevant_when: tuple[str, ...]  # profile ids or coverage topics
    declared_source: str
    install_argv: tuple[str, ...] | None  # None = manual-only (no safe portable install)
    prerequisites: tuple[str, ...]  # executables that must be on PATH
    security_notes: str
    removal: str
    # Coverage topics only trigger this candidate when the project profile
    # is one of these (empty tuple = topics never apply). None = topics
    # apply to every category.
    topic_categories: tuple[str, ...] | None = None
    # How far the declared install command actually gets us. An MCP server
    # *launch* (`npx pkg`) is not a host registration, so it must never be
    # reported as an installed integration.
    integration_level: str = "manual-registration"  # launch-only | manual-registration | none
    # True only when the recorded install command proves the capability is
    # registered AND reachable from the host. False for every current
    # candidate, because aperio does not edit host MCP config files.
    verifies_installation: bool = False

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "kind": self.kind,
            "purpose": self.purpose,
            "relevant_when": list(self.relevant_when),
            "declared_source": self.declared_source,
            "maintenance_status": "unknown (no network lookup performed)",
            "install_argv": list(self.install_argv) if self.install_argv else None,
            "prerequisites": list(self.prerequisites),
            "security_notes": self.security_notes,
            "removal": self.removal,
            "integration_level": self.integration_level,
            "verifies_installation": self.verifies_installation,
        }


CANDIDATES: tuple[ToolCandidate, ...] = (
    ToolCandidate(
        "playwright-mcp",
        "Playwright MCP server",
        "mcp",
        "Browser automation and end-to-end functional testing in real browsers.",
        ("web_app", "website", "mobile_app", "saas", "testing"),
        "Declared: Microsoft Playwright project (npm package @playwright/mcp); verify before trusting",
        ("npx", "@playwright/mcp@latest"),
        ("node", "npm", "npx"),
        "Runs local browsers; browser automation can act on any page it is pointed at. "
        "Scope it deliberately and never point it at credentials-bearing sessions.",
        "Remove the dependency from package.json / MCP config; no global changes made by aperio.",
        ("web_app", "website", "saas", "mobile_app"),
        integration_level="launch-only",
        verifies_installation=False,
    ),
    ToolCandidate(
        "chrome-devtools-mcp",
        "Chrome DevTools MCP server",
        "mcp",
        "Chromium inspection, debugging, network analysis, and performance diagnostics.",
        ("web_app", "website", "hosting"),
        "Declared: Google Chrome DevTools team (npm package chrome-devtools-mcp); verify before trusting",
        ("npx", "chrome-devtools-mcp@latest"),
        ("node", "npm", "npx"),
        "Connects to a local Chromium instance; can read page contents and network traffic. "
        "Do not attach it to sessions containing credentials.",
        "Remove the dependency / MCP config entry; aperio makes no global changes.",
        ("web_app", "website"),
        integration_level="launch-only",
        verifies_installation=False,
    ),
    ToolCandidate(
        "pytest",
        "pytest",
        "cli",
        "Automated Python testing; produces verification evidence.",
        ("testing",),
        "Declared: pytest.org (pip package pytest)",
        None,  # project-local installs depend on the environment: manual fallback only
        ("python3",),
        "Standard test runner; no special privileges.",
        "Uninstall with the environment's package manager; aperio does not install it.",
    ),
)


def recommend_tools(
    discovery: DiscoveryResult,
    reconstruction,
    report: ToolsReport | None = None,
    project_category: str | None = None,
) -> list[dict]:
    """Recommend candidates relevant to THIS project, with honest caveats.

    Relevance is deterministic: the project's category/profile and the
    reconstruction coverage hints select candidates; already-installed
    tools are reported as present rather than recommended for install.
    """
    from aperio.onboarding_interview import profile_for_category

    profile_id = None
    if project_category:
        try:
            profile_id = profile_for_category(project_category).profile_id
        except ValidationError:
            profile_id = None

    coverage_topics = set(reconstruction.missing_topics)
    if report is None:
        report = discover_tools(str(discovery.workspace), discovery)

    installed_ids = {t.tool_id for t in report.executables if t.status == "installed"}
    recommendations = []
    for candidate in CANDIDATES:
        relevant = False
        if profile_id and profile_id in candidate.relevant_when:
            relevant = True
        if candidate.topic_categories != () and any(
            topic in candidate.relevant_when for topic in coverage_topics
        ):
            # Topic-based relevance is category-gated: browser tooling is
            # never suggested for desktop automation just because no doc
            # mentions 'hosting'.
            if candidate.topic_categories is None or (
                profile_id in candidate.topic_categories
            ):
                relevant = True
        if not relevant:
            continue
        missing_prereq = [p for p in candidate.prerequisites if p not in installed_ids]
        entry = candidate.to_dict()
        entry["relevance"] = (
            f"profile {profile_id}" if profile_id and profile_id in candidate.relevant_when
            else "missing coverage: " + ", ".join(t for t in candidate.relevant_when if t in coverage_topics)
        )
        if missing_prereq:
            entry["compatibility"] = f"incompatible now: prerequisites missing ({', '.join(missing_prereq)})"
            entry["installable_by_aperio"] = False
        elif candidate.install_argv is None:
            entry["compatibility"] = "compatible; aperio provides plan-only guidance (manual install)"
            entry["installable_by_aperio"] = False
        else:
            entry["compatibility"] = "compatible (prerequisites found)"
            entry["installable_by_aperio"] = True
        recommendations.append(entry)
    return recommendations


# ---------------------------------------------------------------------------
# Install plan + authorization-gated execution.
# ---------------------------------------------------------------------------

def _prerequisites_satisfied(
    prerequisites: tuple[str, ...], report: ToolsReport
) -> tuple[bool, list[str]]:
    """All declared prerequisites must be installed; return the missing ones.

    Finding *one* prerequisite is not enough: ``node`` without ``npm`` or
    ``npx`` cannot run an ``npx``-based install, so that combination must be
    classified incompatible rather than compatible.
    """
    installed = {t.tool_id for t in report.executables if t.status == "installed"}
    missing = [p for p in prerequisites if p not in installed]
    return (not missing), missing


def install_plan_fingerprint(plan: dict) -> str:
    """Hash of the security-relevant fields of an install plan.

    Approval is bound to this value so a plan cannot be swapped for an
    unrelated executable between approval and execution.
    """
    import hashlib

    material = {
        "tool_id": plan.get("tool_id"),
        "command": plan.get("8_install_command"),
        "source": plan.get("3_source"),
        "permissions": plan.get("6_required_permissions"),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_install_plan(
    tool_id: str,
    report: ToolsReport,
    *,
    authorize_probe: bool = False,
) -> dict:
    """The ten mandated plan items (Rule 14). Producing a plan installs nothing."""
    candidate = next((c for c in CANDIDATES if c.tool_id == tool_id), None)
    if candidate is None:
        raise ValidationError(f"unknown tool candidate {tool_id!r}")

    satisfied, missing = _prerequisites_satisfied(candidate.prerequisites, report)
    prereq_status = "found" if satisfied else "missing"

    plan = {
        "format": "aperio-onboarding-install-plan",
        "version": 1,
        "tool_id": candidate.tool_id,
        "1_name": candidate.name,
        "2_intended_purpose": candidate.purpose,
        "3_source": candidate.declared_source,
        "4_candidate_version": "unspecified; latest at install time (aperio does not fetch versions)",
        "5_compatibility": prereq_status,
        "6_required_permissions": "writes project-local dependency files only (no global changes)",
        "7_required_network_access": "the package registry (npm) — required by the install itself, not by aperio",
        "8_install_command": list(candidate.install_argv) if candidate.install_argv else None,
        "9_security_considerations": candidate.security_notes,
        "10_removal": candidate.removal,
        "manual_fallback": (
            "run the install command yourself in a terminal; then re-run "
            "'aperio onboarding-discover-tools' to reassess"
        ),
        "execution": (
            "plan-only by default. Execution requires BOTH a recorded session "
            f"approval (action '{INSTALL_ACTION}' scoped to '{candidate.tool_id}', "
            "bound to this plan's fingerprint) AND the --authorize-install flag."
        ),
        "integration_level": candidate.integration_level,
        "verifies_installation_claim": (
            "the install command completes a package install/launch only; aperio "
            "does not edit host MCP configuration, so this is NOT a claim that a "
            "host is wired to the server"
            if candidate.integration_level == "launch-only"
            else "no installation claim is made"
        ),
        "_missing_prerequisites": missing,
    }
    plan["_fingerprint"] = install_plan_fingerprint(plan)
    if candidate.install_argv is None:
        plan["execution"] = (
            "manual-only: no safe portable install is implemented for this tool; "
            "aperio will never pretend it was installed."
        )
    if prereq_status == "missing":
        plan["execution"] = (
            "blocked: prerequisites missing (" + ", ".join(missing) + "); install them first"
        )
    return plan


def execute_install(
    apdir: Path,
    plan: dict,
    *,
    authorize_install: bool = False,
) -> dict:
    """Execute a plan's exact pre-declared command, or refuse.

    Two independent gates must both be present:
    1. a recorded session approval for action ``tool.install`` scoped to
       this tool id (operator decision, durably recorded), and
    2. the explicit ``authorize_install`` flag on this invocation.

    The command runs with fixed argv (no shell), stdin closed, bounded
    capture, a hard timeout, and a process-group kill on overrun. After a
    successful run, installed state is re-checked honestly; capability
    claims stay unverified unless a probe is run separately.
    """
    tool_id = plan.get("tool_id")
    if not isinstance(tool_id, str) or not tool_id:
        raise ValidationError("install plan is missing a tool id")
    if not has_approval(apdir, INSTALL_ACTION, tool_id):
        raise StateError(
            f"installation is not approved: record an explicit approval for action "
            f"'{INSTALL_ACTION}' scoped to '{tool_id}' first"
        )
    if not authorize_install:
        raise StateError(
            "installation requires the explicit --authorize-install flag in "
            "addition to the recorded approval; nothing was executed"
        )
    argv = plan.get("8_install_command")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise StateError(
            "this tool has no executable install command (manual-only); "
            "aperio will not pretend it was installed"
        )
    if plan.get("5_compatibility") != "found":
        missing = plan.get("_missing_prerequisites") or []
        detail = (" (" + ", ".join(missing) + ")") if missing else ""
        raise StateError(
            f"install blocked: prerequisites missing{detail}; nothing was executed"
        )

    # A13: approval is bound to this plan's fingerprint. A plan whose command,
    # source or permissions changed after approval must be refused outright,
    # and an approval that carries no fingerprint at all (a "legacy" approval
    # recorded before this binding existed) must NOT authorize execution — the
    # operator has to re-approve the exact reviewed plan.
    fingerprint = install_plan_fingerprint(plan)
    approved_fingerprint = approved_plan_fingerprint(apdir, INSTALL_ACTION, tool_id)
    if approved_fingerprint is None:
        raise StateError(
            "the recorded approval is not bound to a specific install plan "
            "(no fingerprint); re-approve the exact plan before running it. "
            "Nothing was executed."
        )
    if approved_fingerprint != fingerprint:
        raise StateError(
            "install plan does not match the approved plan (command, source or "
            "permissions changed after approval); nothing was executed"
        )

    started = time.monotonic()
    try:
        result = safeproc.run_captured(
            argv,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            capture_limit=MAX_INSTALL_OUTPUT_BYTES,
            cwd=str(apdir.parent),
            home=str(Path.home()),
        )
    except FileNotFoundError:
        record = {
            "name": tool_id, "action": "install", "status": "failed",
            "detail": f"command not found: {argv[0]}", "recorded_at": _utc_now(),
        }
        add_tool_record(apdir, record)
        raise StateError(
            f"install command {argv[0]!r} not found; nothing was executed"
        ) from None
    except OSError as exc:
        raise StateError(f"install failed to start: {exc}; nothing was executed") from None

    duration = round(time.monotonic() - started, 2)
    if result["timed_out"]:
        record = {
            "name": tool_id,
            "action": "install",
            "status": "failed_timeout",
            "detail": f"install exceeded {INSTALL_TIMEOUT_SECONDS:g}s and was killed",
            "recorded_at": _utc_now(),
        }
        add_tool_record(apdir, record)
        raise StateError(
            f"install of {tool_id!r} exceeded {INSTALL_TIMEOUT_SECONDS:g}s and was "
            "terminated; no completion claim is made"
        ) from None

    returncode = result["returncode"]
    # Only a redacted tail is retained; the raw stream never leaves this frame.
    combined = (result["stdout"] or "") + (result["stderr"] or "")
    tail = safeproc.redact_output(combined[-400:], home=str(Path.home()))
    if returncode == 0:
        status = "install_command_completed"
        detail = f"exit 0 in {duration}s; installed state NOT verified by this result alone"
    else:
        status = "install_command_failed"
        detail = f"exit {returncode} in {duration}s"
    record = {
        "name": tool_id,
        "action": "install",
        "status": status,
        "detail": detail,
        "command": list(argv),
        "output_tail": tail,
        "output_redacted": True,
        "integration_level": plan.get("integration_level"),
        "recorded_at": _utc_now(),
    }
    add_tool_record(apdir, record)
    if returncode != 0:
        raise StateError(
            f"install command exited {returncode}; aperio makes no "
            "installed-state claim. Re-run 'aperio onboarding-discover-tools' "
            "to reassess honestly."
        )
    return record


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
