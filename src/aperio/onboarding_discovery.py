"""Read-only workspace discovery for onboarding (discovery-result v1).

Step 01 first action: classify the workspace before anything else happens.
Discovery performs **no mutations of any kind** — it opens directories for
listing, stats entries without following symlinks, and optionally probes
``.aperio/state.json`` with the existing schema reader. Every finding it
reports names the evidence that supports it, so an operator or a
reviewer can audit why a workspace was classified the way it was.

Classifications (directive Step 01, Rule 4):

- ``empty_new``            A. Empty / new project (no entries at all).
- ``existing_non_aperio``  B. Content exists, no ``.aperio/`` directory.
- ``aperio_project``       C. Valid Aperio project (state.json readable).
- ``aperio_partial``       D. ``.aperio/`` exists but ``state.json`` is
                             missing — partially initialized.
- ``legacy_orchestrated``  E. No valid Aperio state, but evidence of a
                             previous orchestration framework (AI
                             instruction/config markers).
- ``ambiguous``            F. Corrupt, unsupported, symlinked, oversized,
                             or otherwise unsafe-to-interpret state.

A directory containing files but no ``.aperio`` directory is **never**
classified ``empty_new`` (directive Rule 4).

Safety boundaries (implemented, not aspirational):

- **Bounded scanning**: at most ``MAX_SCAN_ENTRIES`` entries and
  ``MAX_SCAN_DEPTH`` directory levels are visited; hitting a bound sets
  ``scan_truncated`` and the classification still stands on the evidence
  gathered, with the truncation reported honestly.
- **Symlinks are never followed** during the walk. A symlink entry is
  recorded as a finding (name + link target string). A link whose target
  resolves outside the workspace is flagged ``escapes_workspace`` — a risk
  signal, not an action trigger.
- **Irrelevant artifacts are skipped** (caches, dependency trees, build
  output) by name, so a ``node_modules`` tree cannot exhaust the scan
  budget. Skipped names are still reported as present.
- **No project file content is read** except ``.aperio/state.json``
  (through a bounded probe) — discovery never executes or even opens
  discovered scripts.
- **Repository content is untrusted data.** File *names* observed here are
  evidence for classification only; they are never treated as instructions
  (see ``onboarding_reconstruct`` for the document-boundary policy).

The result is a plain dataclass with a ``to_dict()`` rendering suitable for
``--json`` output and for the machine-readable onboarding protocol.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from aperio.errors import PathError
from aperio.paths import resolve_target
from aperio.schema import (
    SCHEMA_VERSION,
    STATE_FILE_NAME,
    ProjectState,
    parse_state_bytes,
)

DISCOVERY_RESULT_FORMAT = "aperio-onboarding-discovery"
DISCOVERY_RESULT_VERSION = 1

# Classification values (stable, machine-readable).
CLASS_EMPTY_NEW = "empty_new"
CLASS_EXISTING_NON_APERIO = "existing_non_aperio"
CLASS_APERIO_PROJECT = "aperio_project"
CLASS_APERIO_PARTIAL = "aperio_partial"
CLASS_LEGACY_ORCHESTRATED = "legacy_orchestrated"
CLASS_AMBIGUOUS = "ambiguous"

CLASSIFICATIONS = (
    CLASS_EMPTY_NEW,
    CLASS_EXISTING_NON_APERIO,
    CLASS_APERIO_PROJECT,
    CLASS_APERIO_PARTIAL,
    CLASS_LEGACY_ORCHESTRATED,
    CLASS_AMBIGUOUS,
)

APERIO_DIR_NAME = ".aperio"
ONBOARDING_SESSION_REL = "onboarding/session.json"

# Scan bounds. These are generous for real projects but finite by design:
# discovery must terminate even inside a pathological workspace.
MAX_SCAN_ENTRIES = 5000
MAX_SCAN_DEPTH = 4
MAX_TOP_LEVEL_NAMES = 200
MAX_STATE_PROBE_BYTES = 1024 * 1024  # 1 MiB: refuse-to-interpret, not truncate

# Directory names never descended into (build artifacts, caches, dependency
# trees, VCS internals, Aperio's own context directory handled separately).
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        "site-packages",
        "dist",
        "build",
        "target",
        "vendor",
        ".cache",
        ".parcel-cache",
        ".next",
        ".nuxt",
        ".turbo",
        "coverage",
        ".gradle",
        ".idea",
    }
)

# Noise files that never make a workspace "meaningful content" on their own
# but also never let it be classified empty (directive Rule 4).
NOISE_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".localized"})

SOURCE_EXTENSIONS = frozenset(
    {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
        ".java", ".kt", ".kts", ".swift", ".rb", ".php", ".c", ".h", ".cpp",
        ".hpp", ".cs", ".sh", ".bash", ".zsh", ".sql", ".dart", ".scala",
        ".lua", ".pl", ".ex", ".exs", ".erl", ".hs", ".vue", ".svelte",
    }
)

DOC_EXTENSIONS = frozenset({".md", ".rst", ".adoc", ".org", ".tex"})

CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"})

# Evidence of a previous orchestration framework or AI-host configuration.
# Detection is name-based and deliberately conservative: these are *claims
# about configuration that exists*, never instructions to act on.
LEGACY_ORCHESTRATOR_MARKERS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursorrules",
    ".clinerules",
    ".windsurfrules",
    ".mcp.json",
    "mcp.json",
    ".mcp",
    ".cursor",
    ".codex",
    ".claude",
    ".opencode",
    ".continue",
    ".aider.conf.yml",
    ".github/copilot-instructions.md",
)

# Task-style documents that may hold pending work (inventory only; items in
# them are never treated as approved tasks — see onboarding_reconstruct).
TASK_INVENTORY_NAMES = ("TODO", "ROADMAP", "BACKLOG", "TASKS", "PLAN", "MASTER-PLAN")


@dataclass(frozen=True)
class SymlinkFinding:
    """A symlink observed during the scan (never followed)."""

    path: str  # workspace-relative
    target: str  # raw link target string, as stored
    escapes_workspace: bool


@dataclass(frozen=True)
class DiscoveryResult:
    """Structured, inspectable result of workspace discovery."""

    workspace: Path
    scanned_at: str
    classification: str
    classification_reasons: tuple[str, ...]
    is_empty: bool
    has_aperio_dir: bool
    aperio_dir_is_symlink: bool
    aperio_state_status: str  # absent|valid|corrupt|unsupported_version|oversized|unreadable|symlinked|unknown
    aperio_state: ProjectState | None
    missing_scaffolding: tuple[str, ...]
    existing_onboarding_session: bool
    legacy_orchestrator_evidence: tuple[str, ...]
    documentation: tuple[str, ...]
    ai_instructions: tuple[str, ...]
    task_documents: tuple[str, ...]
    source_files_count: int
    doc_files_count: int
    config_files_count: int
    other_files_count: int
    top_level_names: tuple[str, ...]
    symlinks: tuple[SymlinkFinding, ...]
    scan_truncated: bool
    scan_limits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-safe rendering (paths are workspace-relative strings)."""
        return {
            "format": DISCOVERY_RESULT_FORMAT,
            "version": DISCOVERY_RESULT_VERSION,
            "workspace": str(self.workspace),
            "scanned_at": self.scanned_at,
            "classification": self.classification,
            "classification_reasons": list(self.classification_reasons),
            "is_empty": self.is_empty,
            "has_aperio_dir": self.has_aperio_dir,
            "aperio_dir_is_symlink": self.aperio_dir_is_symlink,
            "aperio_state_status": self.aperio_state_status,
            "aperio_state": (
                {
                    "name": self.aperio_state.name,
                    "phase": self.aperio_state.phase,
                    "aperio_version": self.aperio_state.aperio_version,
                    "schema_version": self.aperio_state.schema_version,
                }
                if self.aperio_state is not None
                else None
            ),
            "missing_scaffolding": list(self.missing_scaffolding),
            "existing_onboarding_session": self.existing_onboarding_session,
            "legacy_orchestrator_evidence": list(self.legacy_orchestrator_evidence),
            "documentation": list(self.documentation),
            "ai_instructions": list(self.ai_instructions),
            "task_documents": list(self.task_documents),
            "counts": {
                "source_files": self.source_files_count,
                "doc_files": self.doc_files_count,
                "config_files": self.config_files_count,
                "other_files": self.other_files_count,
            },
            "top_level_names": list(self.top_level_names),
            "symlinks": [
                {"path": s.path, "target": s.target, "escapes_workspace": s.escapes_workspace}
                for s in self.symlinks
            ],
            "scan_truncated": self.scan_truncated,
            "scan_limits": dict(self.scan_limits),
        }


def discover_workspace(raw_path: str) -> DiscoveryResult:
    """Classify a workspace for onboarding. Strictly read-only.

    :raises PathError: when the path is empty, unsafe, missing, or not a
        directory.
    """
    workspace = resolve_target(raw_path, for_writing=False)
    if not workspace.exists():
        raise PathError(f"project directory does not exist: {workspace}")
    if not workspace.is_dir():
        raise PathError(f"target path exists and is not a directory: {workspace}")

    scan = _scan_tree(workspace)
    aperio_probe = _probe_aperio_dir(workspace)
    evidence = _collect_evidence(scan)

    classification, reasons = _classify(scan, aperio_probe, evidence)

    return DiscoveryResult(
        workspace=workspace,
        scanned_at=_utc_now(),
        classification=classification,
        classification_reasons=tuple(reasons),
        is_empty=scan.total_entries == 0,
        has_aperio_dir=aperio_probe["has_aperio_dir"],
        aperio_dir_is_symlink=aperio_probe["aperio_dir_is_symlink"],
        aperio_state_status=aperio_probe["state_status"],
        aperio_state=aperio_probe["state"],
        missing_scaffolding=tuple(aperio_probe["missing_scaffolding"]),
        existing_onboarding_session=aperio_probe["existing_onboarding_session"],
        legacy_orchestrator_evidence=tuple(evidence["legacy_markers"]),
        documentation=tuple(evidence["documentation"]),
        ai_instructions=tuple(evidence["ai_instructions"]),
        task_documents=tuple(evidence["task_documents"]),
        source_files_count=scan.source_files,
        doc_files_count=scan.doc_files,
        config_files_count=scan.config_files,
        other_files_count=scan.other_files,
        top_level_names=tuple(scan.top_level_names[:MAX_TOP_LEVEL_NAMES]),
        symlinks=tuple(scan.symlinks),
        scan_truncated=scan.truncated,
        scan_limits={
            "max_entries": MAX_SCAN_ENTRIES,
            "max_depth": MAX_SCAN_DEPTH,
            "visited_entries": scan.total_entries,
        },
    )


@dataclass
class _ScanState:
    """Mutable accumulator for one bounded tree walk."""

    total_entries: int = 0
    source_files: int = 0
    doc_files: int = 0
    config_files: int = 0
    other_files: int = 0
    truncated: bool = False
    top_level_names: list[str] = field(default_factory=list)
    symlinks: list[SymlinkFinding] = field(default_factory=list)
    file_names: list[str] = field(default_factory=list)  # workspace-relative
    dir_names: list[str] = field(default_factory=list)  # workspace-relative


def _scan_tree(workspace: Path) -> _ScanState:
    """Walk the workspace within bounds; never follow symlinks; never mutate.

    Entries are iterated incrementally with ``os.scandir`` used as a context
    manager so a directory with millions of entries is never materialised as
    a Python list; the global entry cap is checked before each entry is
    recorded.
    """
    state = _ScanState()
    workspace_str = str(workspace)
    stack: list[tuple[Path, int]] = [(workspace, 0)]

    while stack:
        current, depth = stack.pop()
        if depth >= MAX_SCAN_DEPTH:
            state.truncated = True
            continue
        try:
            it = os.scandir(current)
        except OSError:
            # An unreadable directory is reported honestly via truncation;
            # classification proceeds on the evidence gathered so far.
            state.truncated = True
            continue

        with it as scan:
            for entry in scan:
                if state.total_entries >= MAX_SCAN_ENTRIES:
                    state.truncated = True
                    stack.clear()
                    break
                state.total_entries += 1

                rel = os.path.relpath(entry.path, workspace_str)
                if depth == 0:
                    state.top_level_names.append(entry.name)

                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    state.truncated = True
                    continue

                if is_link:
                    state.symlinks.append(_describe_symlink(workspace, rel, entry.path))
                    continue

                if is_dir:
                    state.dir_names.append(rel)
                    if depth == 0 or entry.name not in IGNORED_DIRECTORY_NAMES:
                        # Depth-0 subdirectories are always reported; deeper
                        # ones only when their name carries meaning (dirs
                        # inside ignored trees are unreachable by construction).
                        if entry.name not in IGNORED_DIRECTORY_NAMES:
                            stack.append((Path(entry.path), depth + 1))
                    continue

                if is_file:
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in SOURCE_EXTENSIONS:
                        state.source_files += 1
                    elif ext in DOC_EXTENSIONS:
                        state.doc_files += 1
                    elif ext in CONFIG_EXTENSIONS:
                        state.config_files += 1
                    else:
                        state.other_files += 1
                    state.file_names.append(rel)
                    continue

                # Not a link, dir, or regular file (device, fifo, socket):
                # report as an entry but do not interact with it.
                state.other_files += 1

    return state


def _describe_symlink(workspace: Path, rel: str, entry_path: str) -> SymlinkFinding:
    """Describe a symlink without following it."""
    try:
        target = os.readlink(entry_path)
    except OSError:
        target = "<unreadable link target>"
    escapes = _link_escapes_workspace(workspace, entry_path, target)
    return SymlinkFinding(path=rel, target=target, escapes_workspace=escapes)


def _link_escapes_workspace(workspace: Path, entry_path: str, target: str) -> bool:
    """Whether a link's target resolves outside the workspace (best effort).

    Dangling links are evaluated lexically (join + normalize) because there
    is nothing at the destination to resolve; this is a risk *signal* for
    reporting, not a security boundary claim.
    """
    if os.path.isabs(target):
        candidate = target
    else:
        candidate = os.path.join(os.path.dirname(entry_path), target)
    normalized = os.path.normpath(candidate)
    if os.path.exists(normalized):
        resolved = os.path.realpath(normalized)
    else:
        resolved = normalized
    try:
        return not (resolved == str(workspace) or resolved.startswith(str(workspace) + os.sep))
    except (ValueError, TypeError):
        return True


def _probe_aperio_dir(workspace: Path) -> dict:
    """Probe ``.aperio/`` presence and state readability without mutating.

    The state probe reads at most ``MAX_STATE_PROBE_BYTES``; an oversized
    state file is reported ``oversized`` and treated as ambiguous rather
    than being parsed or modified.
    """
    aperio_dir = workspace / APERIO_DIR_NAME
    probe: dict = {
        "has_aperio_dir": False,
        "aperio_dir_is_symlink": False,
        "state_status": "unknown",
        "state": None,
        "missing_scaffolding": [],
        "existing_onboarding_session": False,
    }

    if os.path.islink(aperio_dir):
        probe["has_aperio_dir"] = True
        probe["aperio_dir_is_symlink"] = True
        probe["state_status"] = "symlinked"
        return probe
    if aperio_dir.is_dir():
        probe["has_aperio_dir"] = True
    elif aperio_dir.exists():
        # .aperio exists but is not a directory: unsafe to interpret.
        probe["state_status"] = "unknown"
        return probe
    else:
        probe["state_status"] = "absent"
        return probe

    state_file = aperio_dir / STATE_FILE_NAME
    if os.path.islink(state_file):
        probe["state_status"] = "symlinked"
    elif not state_file.is_file():
        probe["state_status"] = "missing"
    else:
        try:
            raw = state_file.read_bytes()
        except OSError:
            probe["state_status"] = "unreadable"
        else:
            if len(raw) > MAX_STATE_PROBE_BYTES:
                probe["state_status"] = "oversized"
            else:
                probe["state_status"] = _classify_state_bytes(raw, state_file)
                if probe["state_status"] == "valid":
                    probe["state"] = parse_state_bytes(raw, state_file)

    probe["missing_scaffolding"] = _missing_scaffolding(aperio_dir)
    probe["existing_onboarding_session"] = os.path.isfile(
        aperio_dir / ONBOARDING_SESSION_REL
    ) and not os.path.islink(aperio_dir / ONBOARDING_SESSION_REL)
    return probe


def _classify_state_bytes(raw: bytes, path: Path) -> str:
    """Distinguish valid / corrupt / unsupported state without raising."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "corrupt"
    if not isinstance(data, dict):
        return "corrupt"
    version = data.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        return "unsupported_version"
    try:
        parse_state_bytes(raw, path)
    except Exception:
        return "corrupt"
    return "valid"


def _missing_scaffolding(aperio_dir: Path) -> list[str]:
    """Scaffolding files a normal init creates but this project lacks."""
    expected = ("brief.md", "decisions.md", "handoff.md")
    missing = []
    for name in expected:
        candidate = aperio_dir / name
        if not candidate.is_file() or os.path.islink(candidate):
            missing.append(name)
    return missing


def _collect_evidence(scan: _ScanState) -> dict:
    """Name-based evidence collection (no file content is read)."""
    evidence: dict = {
        "legacy_markers": [],
        "documentation": [],
        "ai_instructions": [],
        "task_documents": [],
    }

    for rel in scan.file_names:
        name = os.path.basename(rel)
        upper = name.upper()
        if name in LEGACY_ORCHESTRATOR_MARKERS:
            evidence["legacy_markers"].append(rel)
            evidence["ai_instructions"].append(rel)
        elif upper.startswith(("README", "PRD", "SPEC", "REQUIREMENTS", "ARCHITECTURE", "GUIDE")):
            evidence["documentation"].append(rel)
        elif upper.startswith(TASK_INVENTORY_NAMES):
            evidence["task_documents"].append(rel)
            if upper.startswith(("PLAN", "MASTER-PLAN")):
                # Plan documents often indicate orchestrated work history.
                if rel not in evidence["legacy_markers"]:
                    evidence["legacy_markers"].append(rel)

    for rel in scan.dir_names:
        if rel in LEGACY_ORCHESTRATOR_MARKERS:
            evidence["legacy_markers"].append(rel)
        elif rel == "docs":
            # Do not descend (bounds); note the directory as documentation.
            evidence["documentation"].append(rel + "/")
    # .github/copilot-instructions.md requires depth 2; covered by the file
    # walk above when depth permits. Detection is best-effort and honest:
    # truncation is reported via scan_truncated.
    return evidence


def _classify(scan: _ScanState, aperio: dict, evidence: dict) -> tuple[str, list[str]]:
    """Decision function over scan + probe + evidence; reasons are audit text."""
    reasons: list[str] = []
    has_aperio = aperio["has_aperio_dir"]

    if scan.total_entries == 0:
        reasons.append("workspace contains no entries at all")
        if has_aperio:
            # An .aperio-only directory is not empty; fall through below.
            reasons.append("but an .aperio directory is present, so it is not treated as empty")
        else:
            return CLASS_EMPTY_NEW, reasons

    if has_aperio:
        if aperio["aperio_dir_is_symlink"]:
            reasons.append(".aperio is a symlink; aperio refuses to interpret it")
            return CLASS_AMBIGUOUS, reasons
        status = aperio["state_status"]
        if status == "valid":
            reasons.append("valid .aperio/state.json found")
            if aperio["missing_scaffolding"]:
                reasons.append(
                    "init scaffolding incomplete: " + ", ".join(aperio["missing_scaffolding"])
                )
            if evidence["legacy_markers"]:
                reasons.append(
                    "previous orchestration evidence also present: "
                    + ", ".join(evidence["legacy_markers"])
                )
            return CLASS_APERIO_PROJECT, reasons
        if status == "missing":
            reasons.append(".aperio directory exists but state.json is missing")
            return CLASS_APERIO_PARTIAL, reasons
        if status == "symlinked":
            reasons.append("state.json is a symlink; aperio refuses to interpret it")
            return CLASS_AMBIGUOUS, reasons
        if status == "oversized":
            reasons.append("state.json exceeds the probe size bound; not interpreted")
            return CLASS_AMBIGUOUS, reasons
        if status == "unreadable":
            reasons.append("state.json could not be read; not interpreted")
            return CLASS_AMBIGUOUS, reasons
        if status == "unsupported_version":
            reasons.append("state.json uses a newer schema version than this aperio supports")
            return CLASS_AMBIGUOUS, reasons
        # corrupt
        reasons.append("state.json is corrupt; aperio will not modify it")
        return CLASS_AMBIGUOUS, reasons

    if evidence["legacy_markers"]:
        reasons.append(
            "no .aperio directory; previous orchestration framework evidence: "
            + ", ".join(evidence["legacy_markers"])
        )
        return CLASS_LEGACY_ORCHESTRATED, reasons

    reasons.append("files exist but no .aperio directory: not an empty project")
    if scan.top_level_names and all(
        name in NOISE_FILE_NAMES for name in scan.top_level_names
    ):
        reasons.append("only OS noise files are present (" + ", ".join(scan.top_level_names) + ")")
    return CLASS_EXISTING_NON_APERIO, reasons


def _utc_now() -> str:
    """Current UTC time, ISO-8601 with Z suffix (same rendering as schema.py)."""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
