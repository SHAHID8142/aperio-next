"""Roadmap persistence and operations (master-roadmap planning level).

``.aperio/roadmap.json`` is created explicitly (``aperio roadmap-create``)
— it is not part of ``init`` and, like the plan, deliberately not
reported by ``status`` (use ``roadmap-show``).

Every mutation of an EXISTING roadmap (revise, approve) follows the same
single-snapshot protocol as the plan (see :mod:`aperio.planstore`):
boundary validation, one snapshot read under the advisory
``roadmap.lock``, rule checks against that snapshot, mutation of only
aprio-owned fields (unknown on-disk keys preserved), exclusive temp file
(``roadmap.json.tmp``), byte compare-and-swap, atomic rename. ``create``
is create-only: exclusive temp file plus an atomic hard-link publish.

Error mapping mirrors the plan: invalid input shape → ValidationError;
rule and state conflicts (approval gate, stale revisions, corrupt or
unsupported files) → StateError. A missing ``roadmap.json`` is a normal
state (the roadmap is optional), reported as StateError with
``roadmap-create`` guidance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aperio import __version__
from aperio.errors import PathError, StateError, ValidationError
from aperio.inputs import check_no_foreign_line_separators
from aperio.locks import LOCK_TIMEOUT_SECONDS, acquire_lock, release_lock
from aperio.policy import (
    append_event as _policy_event,
    roadmap_author_events,
    require_action,
)
from aperio.planstore import _author_record
from aperio.roadmapmodel import (
    ROADMAP_SCHEMA_VERSION,
    validate_roadmap_document,
    validate_roadmap_input,
)
from aperio.schema import utc_now_iso

ROADMAP_FILE_NAME = "roadmap.json"
ROADMAP_TMP_NAME = "roadmap.json.tmp"
ROADMAP_LOCK_NAME = "roadmap.lock"


def roadmap_path(aperio_dir: Path) -> Path:
    """Path of the roadmap file inside an ``.aperio/`` directory."""
    return aperio_dir / ROADMAP_FILE_NAME


def _read_snapshot(path: Path) -> bytes:
    """Read the roadmap file, refusing symlinks (portable-read boundary)."""
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading the "
            "roadmap (it could point at local or private data); remove it "
            "and re-run 'aperio roadmap-create' to restore the roadmap"
        )
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise StateError(
            f"no roadmap at {path}; create one with 'aperio roadmap-create'"
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}"
        ) from None


def _parse_snapshot(raw: bytes, path: Path) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file "
            "manually."
        ) from None
    return validate_roadmap_document(data, path)


def read_roadmap(aperio_dir: Path) -> dict:
    """Read and validate the roadmap document.

    :raises StateError: when the roadmap does not exist (with
        roadmap-create guidance), is corrupt, or declares an unsupported
        schema version.
    :raises PathError: when the roadmap file is a symlink.
    """
    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    return _parse_snapshot(
        _read_snapshot(roadmap_path(aperio_dir)), roadmap_path(aperio_dir)
    )


def roadmap_exists(aperio_dir: Path) -> bool:
    """True when a readable roadmap file exists (symlinks are refused by
    the read path, so this is only a light existence probe)."""
    path = roadmap_path(aperio_dir)
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while reading the "
            "roadmap; remove it and re-run 'aperio roadmap-create' to "
            "restore the roadmap"
        )
    return path.exists()


def _discard_created_tmp(tmp: Path, created_tmp: bool) -> None:
    if created_tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # a leftover is a user-resolved conflict


def _publish(aperio_dir: Path, data: dict, snapshot: bytes, path: Path, tmp: Path) -> None:
    """Validate, write to an exclusive temp file, byte-CAS, atomic rename."""
    validate_roadmap_document(data, path)
    try:
        payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError as exc:
        raise StateError(
            f"cannot serialize the roadmap at {path}: {exc}; the document "
            "contains non-standard JSON values (NaN/Infinity, likely in "
            "extension fields). Resolve them manually; nothing was modified."
        ) from None
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot prepare roadmap write at {tmp}: {exc.strerror or exc}; "
            "the temp name is occupied by a concurrent aperio write (retry "
            "shortly) or a leftover from a crashed write (removing it is "
            "your decision); no existing content was modified"
        ) from None

    try:
        current = path.read_bytes()
    except FileNotFoundError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"the roadmap at {path} disappeared during the write; nothing "
            "was modified. Re-run 'aperio roadmap-create' if appropriate."
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot read {path} before publishing: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    if current != snapshot:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"the roadmap at {path} changed during the write (a tool "
            "outside aperio's coordination may have written it); nothing "
            "was modified. Re-read the roadmap and retry."
        )

    try:
        os.replace(tmp, path)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish roadmap at {path}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None


def _empty_approval() -> dict:
    return {
        "status": "draft",
        "approved_revision": None,
        "approved_at": None,
        "approved_by": None,
    }


def _author_entry(actor: str, role: str, method: str) -> dict:
    return {
        "actor": actor,
        "role": role,
        "recorded_at": utc_now_iso(),
        "method": method,
    }


def _record_attribution_event(
    aperio_dir: Path,
    *,
    actor: str,
    role: str,
    action: str,
    revision: int,
    outcome: str,
    note: str | None = None,
) -> None:
    """Write an attribution event, failing with a recovery instruction.

    Same contract as the plan: the roadmap document (written atomically
    first) is the durable source of authorship; the audit log is the
    append-only history that approval cross-checks.
    """
    try:
        _policy_event(
            aperio_dir,
            actor=actor,
            role=role,
            action=action,
            target="roadmap",
            revision=revision,
            outcome=outcome,
            note=note,
        )
    except StateError as exc:
        raise StateError(
            f"roadmap revision {revision} was published with durable author "
            f"attribution, but its audit record could not be written ({exc}). "
            "Approval is blocked until the attribution history is repaired. "
            "The roadmap itself was not rolled back."
        ) from None


def _require_approval_attribution(
    aperio_dir: Path, snapshot: dict, revision: int
) -> dict:
    """Fail-closed authorship check for a governed roadmap approval.

    Requires (1) a durable author record inside the atomically published
    roadmap and (2) exactly one matching attribution event in the audit
    log, naming the same actor. Adoption of pre-governance roadmaps is
    refused: the roadmap is optional, so the honest repair for an
    attribution-less roadmap revision is a fresh create+revise under
    governance, never a guessed author.
    """
    record = _author_record(snapshot, revision)
    if record is None:
        raise StateError(
            f"roadmap revision {revision} has no durable author attribution, "
            "so it cannot be approved. Recreate it under governance with "
            "'aperio roadmap-create' (authorship is never inferred). "
            "Nothing was modified."
        )
    events = roadmap_author_events(aperio_dir, revision)
    if not events:
        raise StateError(
            f"roadmap revision {revision} has durable author attribution "
            f"({record.get('actor')!r}) but no matching audit record; "
            "approval is blocked rather than trusting partial history. "
            "Nothing was modified."
        )
    actors = {event_actor for _action, event_actor in events}
    if actors != {record.get("actor")}:
        raise StateError(
            f"roadmap revision {revision} has conflicting author "
            "attribution between the roadmap and the audit log "
            f"({', '.join(sorted(actors))}); approval is blocked. Nothing "
            "was modified."
        )
    return record


def _mutate_under_lock(
    aperio_dir: Path,
    artifact: str,
    mutate,
    lock_timeout: float,
):
    """Run ``mutate(snapshot)`` -> (document, changed) under roadmap.lock."""
    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    path = roadmap_path(aperio_dir)
    tmp = aperio_dir / ROADMAP_TMP_NAME
    lock_path = aperio_dir / ROADMAP_LOCK_NAME
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio roadmap-create' to repair"
        )
    if os.path.islink(tmp):
        raise PathError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run the command to repair"
        )
    token = acquire_lock(lock_path, timeout=lock_timeout, artifact=artifact)
    try:
        snapshot = _read_snapshot(path)
        document = _parse_snapshot(snapshot, path)
        new_document, changed = mutate(document)
        if changed:
            new_document["updated_at"] = utc_now_iso()
            new_document["aperio_version"] = __version__
            _publish(aperio_dir, new_document, snapshot, path, tmp)
        return new_document, changed
    finally:
        release_lock(lock_path, token)


# --- operations -----------------------------------------------------------------


def create_roadmap(
    aperio_dir: Path, content: dict, *, actor: str | None = None
) -> dict:
    """Create the roadmap (create-only). The roadmap starts at revision 1,
    status draft.

    :raises ValidationError: for invalid input content.
    :raises StateError: when a roadmap already exists (use roadmap-revise).
    """
    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "roadmap.create", target="roadmap", revision=None
    )
    path = roadmap_path(aperio_dir)
    tmp = aperio_dir / ROADMAP_TMP_NAME
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio roadmap-create' to repair"
        )
    if path.exists():
        raise StateError(
            f"a roadmap already exists at {path}; revise it with "
            "'aperio roadmap-revise' instead of creating a new one"
        )
    if os.path.islink(tmp):
        raise PathError(
            f"refusing to follow symlink at {tmp}; "
            "remove it and re-run 'aperio roadmap-create' to repair"
        )

    roadmap_content = validate_roadmap_input(content)
    now = utc_now_iso()
    document: dict = {"schema_version": ROADMAP_SCHEMA_VERSION}
    document.update(roadmap_content)
    document["roadmap_version"] = 1
    document["approval"] = _empty_approval()
    document["created_at"] = now
    document["updated_at"] = utc_now_iso()
    document["aperio_version"] = __version__
    if role is not None:
        document["authors"] = {"1": _author_entry(actor, role, "created")}
    validate_roadmap_document(document, path)

    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            "cannot prepare roadmap creation at "
            f"{tmp}: {exc.strerror or exc}; no existing content was modified"
        ) from None
    try:
        os.link(tmp, path)
    except FileExistsError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"a roadmap appeared at {path} during creation; use 'aperio "
            "roadmap-revise' on the existing roadmap. Nothing was modified."
        ) from None
    except OSError as exc:
        _discard_created_tmp(tmp, created_tmp)
        raise StateError(
            f"cannot publish roadmap at {path}: {exc.strerror or exc}; "
            "no existing content was modified"
        ) from None
    try:
        os.unlink(tmp)
    except OSError:
        pass  # leftover is a user-resolved conflict
    if role is not None:
        _record_attribution_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="roadmap.create",
            revision=document["roadmap_version"],
            outcome="created",
        )
    return document


def revise_roadmap(
    aperio_dir: Path,
    content: dict,
    revises: int,
    *,
    actor: str | None = None,
) -> dict:
    """Replace the roadmap content with a new draft revision.

    ``revises`` must equal the current ``roadmap_version`` (stale →
    conflict). Every revision resets approval to draft: renewed (human)
    approval is required before the roadmap counts as the approved master
    plan again. There is no lifecycle state to carry: milestones hold no
    execution state.
    """
    if isinstance(revises, bool) or not isinstance(revises, int) or revises < 1:
        raise ValidationError(
            "roadmap-revise: --revises must be a positive integer "
            "(the roadmap_version you are revising; see 'aperio roadmap-show')"
        )
    roadmap_content = validate_roadmap_input(content)
    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "roadmap.revise", target="roadmap", revision=revises
    )

    def mutate(snapshot: dict):
        current_version = snapshot["roadmap_version"]
        if revises != current_version:
            raise StateError(
                f"roadmap moved on: current roadmap_version is "
                f"{current_version}, but the revision declares {revises} "
                "(a stale revision would silently revert milestone "
                "planning). Re-read the roadmap with 'aperio roadmap-show "
                "--json', re-apply your changes, and retry. Nothing was "
                "modified."
            )
        new_document = dict(snapshot)  # preserves unknown keys
        new_document.update(roadmap_content)
        new_document["roadmap_version"] = current_version + 1
        new_document["approval"] = _empty_approval()
        if role is not None:
            authors = dict(snapshot.get("authors") or {})
            authors[str(current_version + 1)] = _author_entry(
                actor, role, "revised"
            )
            new_document["authors"] = authors
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "roadmap revision", mutate, LOCK_TIMEOUT_SECONDS
    )
    if role is not None:
        _record_attribution_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="roadmap.revise",
            revision=document["roadmap_version"],
            outcome="revised",
        )
    return document


def approve_roadmap(
    aperio_dir: Path,
    revision: int | None = None,
    by: str | None = None,
    *,
    actor: str | None = None,
) -> dict:
    """Record roadmap approval.

    This is an operational marker recording that the approval decision
    was made — it is NOT cryptographic proof of human authorization; the
    operator is responsible for genuine review. Approval never bumps
    ``roadmap_version``. Re-approving an already-approved roadmap is
    allowed and re-stamps the metadata.

    :param revision: approve a specific roadmap_version; the current one
        when omitted. A mismatch is a conflict (the roadmap moved on).
    """
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
    ):
        raise ValidationError(
            "roadmap-approve: --revision must be a positive integer"
        )
    approved_by = None
    if by is not None:
        if not by.strip():
            raise ValidationError(
                "roadmap-approve: --by must be a non-empty string"
            )
        approved_by = by.strip()
        check_no_foreign_line_separators("roadmap.json", "approved_by", approved_by)
        if "\n" in approved_by:
            raise ValidationError("roadmap-approve: --by must be a single line")

    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    role = require_action(
        aperio_dir, actor, "roadmap.approve", target="roadmap", revision=revision
    )

    def mutate(snapshot: dict):
        current_version = snapshot["roadmap_version"]
        if revision is not None and revision != current_version:
            raise StateError(
                f"roadmap moved on: current roadmap_version is "
                f"{current_version}, but approval was requested for "
                f"revision {revision}. Nothing was modified."
            )
        if role is not None:
            author_record = _require_approval_attribution(
                aperio_dir, snapshot, current_version
            )
            author = author_record["actor"]
            if author == actor:
                raise StateError(
                    f"separation of duties: actor {actor!r} authored roadmap "
                    f"revision {current_version} and cannot approve it. Have "
                    "an independent declared planner or reviewer approve "
                    "this revision. Nothing was modified."
                )
        new_document = dict(snapshot)
        new_document["approval"] = {
            "status": "approved",
            "approved_revision": current_version,
            "approved_at": utc_now_iso(),
            "approved_by": (actor if role is not None else approved_by),
        }
        return new_document, True

    document, _ = _mutate_under_lock(
        aperio_dir, "roadmap approval", mutate, LOCK_TIMEOUT_SECONDS
    )
    if role is not None:
        _policy_event(
            aperio_dir,
            actor=actor,
            role=role,
            action="roadmap.approve",
            target="roadmap",
            revision=document["roadmap_version"],
            outcome="approved",
            note="author differs",
        )
    return document


def roadmap_summary(aperio_dir: Path) -> dict | None:
    """Concise roadmap summary for display and the portable memory API.

    Returns None when no roadmap exists (a normal state). File-level
    damage (non-UTF-8, invalid JSON) is fatal; a newer roadmap schema
    version is non-fatal: a degraded summary with an ``unsupported``
    marker is returned so consumers keep working with an unreadable-but-
    present roadmap. Live symlinks are refused.
    """
    from aperio.project import require_supported_state

    require_supported_state(aperio_dir)
    path = roadmap_path(aperio_dir)
    if os.path.islink(path) and path.exists():
        raise PathError(
            f"refusing to follow symlink at {path} while reading the "
            "roadmap (it could point at local or private data); remove it "
            "and re-run 'aperio roadmap-create' to restore the roadmap"
        )
    if not path.exists():
        return None
    raw = _read_snapshot(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it. "
            "Inspect or restore the file manually."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: {exc.msg}); "
            "aperio will not overwrite it. Inspect or restore the file "
            "manually."
        ) from None

    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if (
        isinstance(data, dict)
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version > ROADMAP_SCHEMA_VERSION
    ):
        return {
            "present": True,
            "unsupported_schema": True,
            "schema_version": schema_version,
        }
    roadmap = validate_roadmap_document(data, path)
    approval = roadmap["approval"]
    return {
        "present": True,
        "unsupported_schema": False,
        "schema_version": ROADMAP_SCHEMA_VERSION,
        "roadmap_version": roadmap["roadmap_version"],
        "approval_status": approval["status"],
        "approved_revision": approval["approved_revision"],
        "approval_required": approval["status"] != "approved",
        "objective": roadmap["objective"],
        "lifecycle": roadmap["lifecycle"],
        "milestone_ids": [m["id"] for m in roadmap["milestones"]],
    }
