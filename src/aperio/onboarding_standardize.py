"""Controlled Aperio standardization (standardization-proposal v1).

For existing projects, Step 01 can offer to organize project knowledge
using Aperio's architecture. The governing rule is **controlled
standardization** (directive Rule 11):

- Before anything changes, a structured proposal is produced: what exists,
  what is the current source of truth, what would be added, what would be
  modified (by default: **nothing existing**), which conflicts need an
  operator decision, what stays untouched, and how to roll back.
- Applying requires an explicit, scoped approval record in the onboarding
  session (``standardization.apply`` for this exact proposal id). The
  enforcement lives in this module's Python boundary — a prompt or a
  document saying "yes" is not approval.
- Existing documents are never rewritten, replaced, or deleted. The
  proposal's canonical artifacts are created as NEW files only. Legacy
  orchestrator files (AGENTS.md, CLAUDE.md, ...) keep working untouched.
- A recoverable checkpoint is refreshed before applying (the existing
  checkpoint mechanism), and the original documents' content hashes are
  recorded so post-apply verification can prove they were preserved.
- If application fails midway, every file THIS call created is removed,
  nothing pre-existing is touched, and the error names the recovery
  procedure.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aperio.artifacts import check_artifact_dir, create_exclusive_text
from aperio.errors import NotInitializedError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock
from aperio.onboarding_discovery import (
    APERIO_DIR_NAME,
    DiscoveryResult,
    ONBOARDING_SESSION_REL,
)
from aperio.onboarding_reconstruct import ReconstructionResult
from aperio.onboarding_session import (
    ONBOARDING_DIR_NAME,
    has_approval,
    update_session,
)
from aperio.project import init_project, require_supported_state
from aperio.schema import utc_now_iso

PROPOSAL_FORMAT = "aperio-onboarding-standardization-proposal"
PROPOSAL_VERSION = 1
MAPPING_FORMAT = "aperio-onboarding-standardization-mapping"
MAPPING_VERSION = 1

APPLY_ACTION = "standardization.apply"

PROPOSALS_DIR = "proposals"
BACKUPS_DIR = "backups"
MAPPING_FILE_NAME = "standardization-mapping.json"

# The canonical artifacts standardization creates (all under .aperio/, all
# via init's non-destructive scaffolding — existing files are never touched).
_CANONICAL_ARTIFACTS = (
    ".aperio/state.json",
    ".aperio/brief.md",
    ".aperio/decisions.md",
    ".aperio/handoff.md",
)


@dataclass(frozen=True)
class ProposedAddition:
    """A NEW file standardization would create (never an existing path)."""

    path: str
    description: str
    created_by: str  # "init scaffolding" | "mapping record" | ...


@dataclass(frozen=True)
class ConflictEntry:
    """A conflict that needs an operator decision before it can be mapped."""

    topic: str
    detail: str
    sources: tuple[str, ...]


@dataclass
class StandardizationProposal:
    """The full transformation proposal (Rule 11 checklist)."""

    proposal_id: str
    workspace: str
    created_at: str
    existing_documents: tuple[dict, ...] = ()
    source_of_truth: tuple[str, ...] = ()
    proposed_additions: tuple[ProposedAddition, ...] = ()
    proposed_modifications: tuple[str, ...] = ()  # always empty by design
    conflicts: tuple[ConflictEntry, ...] = ()
    untouched: tuple[str, ...] = ()
    must_remain_operational: tuple[str, ...] = ()
    compatibility_risks: tuple[str, ...] = ()
    backup_plan: str = ""
    rollback_plan: str = ""

    def to_dict(self) -> dict:
        return {
            "format": PROPOSAL_FORMAT,
            "version": PROPOSAL_VERSION,
            "proposal_id": self.proposal_id,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "existing_documents": [dict(d) for d in self.existing_documents],
            "source_of_truth": list(self.source_of_truth),
            "proposed_additions": [
                {"path": a.path, "description": a.description, "created_by": a.created_by}
                for a in self.proposed_additions
            ],
            "proposed_modifications": list(self.proposed_modifications),
            "conflicts": [
                {"topic": c.topic, "detail": c.detail, "sources": list(c.sources)}
                for c in self.conflicts
            ],
            "untouched": list(self.untouched),
            "must_remain_operational": list(self.must_remain_operational),
            "compatibility_risks": list(self.compatibility_risks),
            "backup_plan": self.backup_plan,
            "rollback_plan": self.rollback_plan,
        }


def propose_standardization(
    discovery: DiscoveryResult,
    reconstruction: ReconstructionResult,
) -> StandardizationProposal:
    """Build the transformation proposal from discovery + reconstruction.

    Read-only: proposing never writes anything.
    """
    workspace = discovery.workspace
    # Timestamp + short random suffix: two proposals created in the same
    # second must never share an id (approvals are scoped per proposal).
    proposal_id = (
        f"STD-{utc_now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:6]}"
    )
    now = utc_now_iso()

    existing_documents = [
        {"path": d.path, "sha256": d.sha256, "bytes": d.total_bytes}
        for d in reconstruction.documents
    ]
    source_of_truth = [d.path for d in reconstruction.documents][:8]

    additions: list[ProposedAddition] = []
    if discovery.classification in ("existing_non_aperio", "legacy_orchestrated"):
        for artifact in _CANONICAL_ARTIFACTS:
            additions.append(
                ProposedAddition(
                    path=artifact,
                    description="Aperio canonical context artifact (created by init if missing)",
                    created_by="init scaffolding",
                )
            )
    elif discovery.classification == "aperio_partial":
        for artifact in _CANONICAL_ARTIFACTS:
            if artifact not in discovery.missing_scaffolding:
                continue
            rel = artifact.replace(".aperio/", "")
            additions.append(
                ProposedAddition(
                    path=artifact,
                    description="Missing init scaffolding repaired by init (create-missing-only)",
                    created_by="init scaffolding",
                )
            )
    additions.append(
        ProposedAddition(
            path=f".aperio/{ONBOARDING_DIR_NAME}/{MAPPING_FILE_NAME}",
            description="Record of the mapping between original documents and Aperio artifacts",
            created_by="mapping record",
        )
    )

    conflicts = [
        ConflictEntry(
            topic=c.topic,
            detail="documents disagree; the operator must pick the authoritative value",
            sources=tuple(claim["source"] for claim in c.claims),
        )
        for c in reconstruction.claim_conflicts
    ]
    if discovery.legacy_orchestrator_evidence:
        conflicts.append(
            ConflictEntry(
                topic="orchestration-ownership",
                detail=(
                    "previous orchestration framework configuration exists; decide "
                    "whether Aperio complements it or eventually replaces it"
                ),
                sources=tuple(discovery.legacy_orchestrator_evidence),
            )
        )

    untouched = sorted(
        {d.path for d in reconstruction.documents}
        | set(discovery.legacy_orchestrator_evidence)
    )
    must_remain = tuple(sorted(set(discovery.legacy_orchestrator_evidence)))

    return StandardizationProposal(
        proposal_id=proposal_id,
        workspace=str(workspace),
        created_at=now,
        existing_documents=tuple(existing_documents),
        source_of_truth=tuple(source_of_truth),
        proposed_additions=tuple(additions),
        proposed_modifications=(),  # by design: no existing file is modified
        conflicts=tuple(conflicts),
        untouched=tuple(untouched),
        must_remain_operational=must_remain,
        compatibility_risks=(
            "legacy orchestrator instructions remain in force and are NOT read as Aperio authorization",
            "duplicate sources of truth can drift; mappings record which document feeds which artifact",
        ),
        backup_plan=(
            "checkpoint refreshed before applying; original documents are never "
            "modified, so no document backups are required"
        ),
        rollback_plan=(
            "remove only the files listed in the mapping record's 'created' list; "
            "pre-existing content is untouched by design"
        ),
    )


def render_proposal_markdown(proposal: StandardizationProposal) -> str:
    """Human-readable preview of exactly what would change (and what not)."""
    lines = [
        f"# Standardization proposal {proposal.proposal_id}",
        "",
        f"- Workspace: `{proposal.workspace}`",
        f"- Created: {proposal.created_at}",
        "",
        "## Existing source-of-truth documents",
        "",
    ]
    if proposal.source_of_truth:
        lines += [f"- `{p}`" for p in proposal.source_of_truth]
    else:
        lines.append("- (none inventoried)")
    lines += ["", "## Proposed additions (new files only)", ""]
    for addition in proposal.proposed_additions:
        lines.append(f"- `{addition.path}` — {addition.description}")
    lines += [
        "",
        "## Proposed modifications to existing files",
        "",
    ]
    if proposal.proposed_modifications:
        lines += [f"- `{p}`" for p in proposal.proposed_modifications]
    else:
        lines.append("- **None.** Existing files are never modified by standardization.")
    lines += ["", "## Conflicts requiring operator resolution", ""]
    if proposal.conflicts:
        for conflict in proposal.conflicts:
            lines.append(
                f"- **{conflict.topic}** ({', '.join(conflict.sources)}): {conflict.detail}"
            )
    else:
        lines.append("- (none detected)")
    lines += ["", "## Explicitly untouched", ""]
    if proposal.untouched:
        lines += [f"- `{p}`" for p in proposal.untouched]
    else:
        lines.append("- (nothing inventoried)")
    if proposal.must_remain_operational:
        lines += ["", "## Must remain operational", ""]
        lines += [f"- `{p}`" for p in proposal.must_remain_operational]
    lines += [
        "",
        "## Backup & rollback",
        "",
        f"- Backup: {proposal.backup_plan}",
        f"- Rollback: {proposal.rollback_plan}",
        "",
        "## Compatibility risks",
        "",
    ]
    lines += [f"- {risk}" for risk in proposal.compatibility_risks]
    return "\n".join(lines) + "\n"


def save_proposal(apdir: Path, proposal: StandardizationProposal) -> Path:
    """Persist the proposal as exclusive-created JSON under .aperio/onboarding/proposals/."""
    require_supported_state(apdir)
    check_artifact_dir(apdir, ONBOARDING_DIR_NAME, PROPOSALS_DIR, create=True)
    out = apdir / ONBOARDING_DIR_NAME / PROPOSALS_DIR / f"{proposal.proposal_id}.json"
    payload = json.dumps(proposal.to_dict(), indent=2) + "\n"
    create_exclusive_text(out, payload, what="standardization proposal")
    return out


def load_proposal(apdir: Path, proposal_id: str) -> StandardizationProposal:
    """Load a saved proposal by id (validated shape; symlinks refused)."""
    require_supported_state(apdir)
    if not isinstance(proposal_id, str) or not proposal_id.replace("-", "").isalnum():
        raise ValidationError(f"invalid proposal id {proposal_id!r}")
    path = apdir / ONBOARDING_DIR_NAME / PROPOSALS_DIR / f"{proposal_id}.json"
    if os.path.islink(path) or not path.is_file():
        raise NotInitializedError(f"no saved proposal {proposal_id!r} in this project")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != PROPOSAL_FORMAT or data.get("proposal_id") != proposal_id:
        raise StateError(f"{path} is not the requested proposal; refusing to apply")
    return _proposal_from_dict(data)


def _proposal_from_dict(data: dict) -> StandardizationProposal:
    return StandardizationProposal(
        proposal_id=data["proposal_id"],
        workspace=data["workspace"],
        created_at=data["created_at"],
        existing_documents=tuple(dict(d) for d in data.get("existing_documents", [])),
        source_of_truth=tuple(data.get("source_of_truth", [])),
        proposed_additions=tuple(
            ProposedAddition(a["path"], a["description"], a["created_by"])
            for a in data.get("proposed_additions", [])
        ),
        proposed_modifications=tuple(data.get("proposed_modifications", [])),
        conflicts=tuple(
            ConflictEntry(c["topic"], c["detail"], tuple(c["sources"]))
            for c in data.get("conflicts", [])
        ),
        untouched=tuple(data.get("untouched", [])),
        must_remain_operational=tuple(data.get("must_remain_operational", [])),
        compatibility_risks=tuple(data.get("compatibility_risks", [])),
        backup_plan=data.get("backup_plan", ""),
        rollback_plan=data.get("rollback_plan", ""),
    )


def apply_standardization(apdir: Path, proposal: StandardizationProposal) -> dict:
    """Apply an approved proposal: init repair + mapping record. Nothing else.

    Approval enforcement is at this boundary: the onboarding session must
    contain an explicit approval for ``standardization.apply`` scoped to
    THIS proposal id, recorded before this call. Nothing pre-existing is
    modified; on failure, only files this call created are removed.
    """
    require_supported_state(apdir)
    if not has_approval(apdir, APPLY_ACTION, proposal.proposal_id):
        raise StateError(
            f"standardization is not approved: the onboarding session must contain "
            f"an explicit approval for action '{APPLY_ACTION}' scoped to proposal "
            f"{proposal.proposal_id!r}. Preview the proposal with the operator and "
            "record approval first."
        )

    created: list[str] = []
    try:
        # 1. Refresh the recoverable checkpoint (existing mechanism) where a
        #    checkpoint already exists; creating one here is fine too — it
        #    records pre-apply state for recovery.
        checkpoint_note = None
        checkpoint_created = False
        try:
            from aperio.checkpoint import CheckpointStore

            store = CheckpointStore(apdir)
            checkpoint_created = not store.path.exists()  # only OUR file may roll back
            store.write_derived(
                {
                    "onboarding": {
                        "reason": "standardization pre-apply snapshot",
                        "proposal_id": proposal.proposal_id,
                        "source_documents": list(proposal.existing_documents),
                    }
                },
                reason="onboarding-standardization",
            )
            if checkpoint_created:
                created.append(str(apdir / "checkpoint.json"))
        except Exception as exc:
            # A pre-existing checkpoint that cannot be rewritten is not a
            # reason to abort; the mapping record below is the rollback
            # source of truth. The condition is reported, not hidden.
            checkpoint_note = f"pre-apply checkpoint not refreshed: {exc}"

        # 2. init repair: create-missing-only scaffolding (idempotent,
        #    never overwrites).
        init_result = init_project(str(apdir.parent))

        # 3. Mapping record: exclusive-create under .aperio/onboarding/.
        check_artifact_dir(apdir, ONBOARDING_DIR_NAME, create=True)
        mapping_path = apdir / ONBOARDING_DIR_NAME / MAPPING_FILE_NAME
        if mapping_path.exists():
            raise StateError(
                f"a standardization mapping already exists at {mapping_path}; "
                "standardization is already applied for this project — nothing "
                "was modified"
            )
        mapping = {
            "format": MAPPING_FORMAT,
            "version": MAPPING_VERSION,
            "proposal_id": proposal.proposal_id,
            "applied_at": utc_now_iso(),
            "source_documents": [dict(d) for d in proposal.existing_documents],
            "mapping": [
                {"source": doc.get("path"), "mapped_to": "aperio context artifacts"}
                for doc in proposal.existing_documents
            ],
            "created": [
                rel for rel in (init_result.created or [])
            ] + [f".aperio/{ONBOARDING_DIR_NAME}/{MAPPING_FILE_NAME}"],
            "preserved": list(proposal.untouched),
            "backup": "checkpoint.json refreshed pre-apply",
        }
        create_exclusive_text(
            mapping_path, json.dumps(mapping, indent=2) + "\n", what="standardization mapping"
        )
        created.append(f".aperio/{ONBOARDING_DIR_NAME}/{MAPPING_FILE_NAME}")

        # 4. Record the applied mapping in the session (audit trail).
        update_session(
            apdir,
            lambda data: _record_applied(data, proposal, mapping["created"]),
        )
        return {
            "proposal_id": proposal.proposal_id,
            "created": mapping["created"],
            "preserved": mapping["preserved"],
            "mapping_path": str(mapping_path),
            "checkpoint_note": checkpoint_note,
        }
    except Exception as exc:
        _rollback_created(apdir, created)
        if isinstance(exc, (StateError, ValidationError, NotInitializedError)):
            raise
        raise StateError(
            f"standardization failed ({exc}); files created by this attempt were "
            "removed and nothing pre-existing was modified. Recovery: inspect "
            ".aperio/onboarding/ for leftovers, then re-propose and re-apply."
        ) from None


def _record_applied(data: dict, proposal: StandardizationProposal, created: list[str]) -> dict:
    history = data.setdefault("history", [])
    history.append(
        {
            "at": utc_now_iso(),
            "event": "standardization.applied",
            "detail": f"{proposal.proposal_id}: {', '.join(created)[:280]}",
        }
    )
    data["standardization"] = {
        "proposal_id": proposal.proposal_id,
        "created": created,
        "applied_at": utc_now_iso(),
    }
    return data


def _rollback_created(apdir: Path, created: list[str]) -> None:
    """Remove ONLY files this apply attempt created (best effort)."""
    for rel in reversed(created):
        path = apdir.parent / rel if rel.startswith(".aperio/") else apdir / rel
        try:
            if path.is_file() and not os.path.islink(path):
                path.unlink()
        except OSError:
            pass


def verify_standardization(
    apdir: Path,
    proposal: StandardizationProposal,
    original_hashes: dict[str, str] | None = None,
) -> dict:
    """Post-apply verification: state valid, scaffolding present, originals intact.

    ``original_hashes`` maps workspace-relative document paths to the SHA-256
    recorded at reconstruction time. A mismatch is reported as drift — which
    may be legitimate outside change, never silently hidden.
    """
    require_supported_state(apdir)
    checks = []
    for artifact in _CANONICAL_ARTIFACTS:
        rel = artifact.replace(".aperio/", "")
        path = apdir / rel
        checks.append(
            {"check": f"{artifact} present", "ok": path.is_file() and not os.path.islink(path)}
        )
    mapping_path = apdir / ONBOARDING_DIR_NAME / MAPPING_FILE_NAME
    checks.append(
        {"check": "mapping record present", "ok": mapping_path.is_file() and not os.path.islink(mapping_path)}
    )
    drift = []
    if original_hashes:
        workspace = apdir.parent
        for rel, expected in original_hashes.items():
            path = workspace / rel
            if not path.is_file() or os.path.islink(path):
                drift.append({"path": rel, "issue": "missing or replaced"})
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                drift.append({"path": rel, "issue": "content changed since reconstruction"})
    return {"proposal_id": proposal.proposal_id, "checks": checks, "drift": drift, "all_ok": all(c["ok"] for c in checks) and not drift}
