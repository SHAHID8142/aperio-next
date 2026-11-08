"""Workflow roles, project policy and actor attribution (Feature 06).

A policy is an **optional, operator-enabled** project artifact
(``.aperio/policies.json``). Projects created before Feature 06, or
projects whose operator chose not to enable governance, keep working
exactly as before: every enforcement hook below is a no-op unless the
policy file exists with ``enabled: true``.

Roles describe workflow responsibilities, never AI-provider identities:

``planner``
    Shapes plans and material revisions.
``implementer``
    Claims approved tasks, dispatches execution, reports evidence.
``reviewer``
    Approves plans/reviews independently and records decisions.

The central security boundary: an actor id supplied on the command line
is a **declaration**, not authenticated identity. This module enforces
workflow *consistency* for declared actors (a declared planner cannot
approve the revision they authored; a declared implementer cannot
approve their own review). It cannot and does not prove who ran the
command. No role label supplied by a caller is trusted: roles are always
looked up in the configured actor mapping.

All mutations use a single ``policy.lock``. Audit events are appended to
``policy-audit.json``; the file is append-only from Aperio's side and
records the declared actor, role, action, target, revision, timestamp,
outcome, and review reference. No raw external diagnostics, command
arguments, environment content, or paths are stored.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from aperio import __version__
from aperio.errors import PathError, StateError, ValidationError
from aperio.locks import acquire_lock, release_lock
from aperio.project import require_supported_state
from aperio.schema import utc_now_iso

POLICY_FILE_NAME = "policies.json"
AUDIT_FILE_NAME = "policy-audit.json"
POLICY_SCHEMA_VERSION = 1
POLICY_LOCK_NAME = "policy.lock"
AUDIT_TMP_NAME = "policy-audit.json.tmp"

ROLES: tuple[str, ...] = ("planner", "implementer", "reviewer")

#: Workflow actions each role is allowed to perform. A policy may narrow
#: this list per role, never widen it.
ROLE_ACTIONS: dict[str, tuple[str, ...]] = {
    "planner": (
        "plan.create",
        "plan.revise",
        "plan.request_approval",
        "plan.approve",
    ),
    "implementer": (
        "task.claim",
        "task.status",
        "execution.dispatch",
        "review.submit",
        "handoff.write",
    ),
    "reviewer": (
        "plan.approve",
        "review.decide",
        "task.verify",
    ),
}

ALL_ACTIONS: frozenset[str] = frozenset(
    action for actions in ROLE_ACTIONS.values() for action in actions
)

_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MAX_TEXT = 300


def policy_path(aperio_dir: Path) -> Path:
    return aperio_dir / POLICY_FILE_NAME


def audit_path(aperio_dir: Path) -> Path:
    return aperio_dir / AUDIT_FILE_NAME


def policy_lock_path(aperio_dir: Path) -> Path:
    return aperio_dir / POLICY_LOCK_NAME


def _reject_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise PathError(
            f"refusing to follow symlink at {path} ({what}); remove it and "
            "re-run 'aperio init' to repair"
        )


def _read_json(path: Path, what: str) -> dict | None:
    _reject_symlink(path, what)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StateError(
            f"cannot read {path}: {exc.strerror or exc}; nothing was modified"
        ) from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise StateError(
            f"{path} is corrupt: not valid UTF-8; aperio will not modify it"
        ) from None
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is corrupt (invalid JSON at line {exc.lineno}: "
            f"{exc.msg}); aperio will not overwrite it"
        ) from None
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: expected a JSON object")
    return data


def _check_actor_id(value: object, *, where: str = "actor") -> str:
    if not isinstance(value, str) or not _ACTOR_RE.match(value):
        raise ValidationError(
            f"{where}: actor id must be 1-64 characters of letters, digits, "
            "'.', '_', ':', '-' and start with a letter or digit"
        )
    return value


def _check_text(value: object, *, where: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValidationError(f"{where} is required")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{where} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise ValidationError(f"{where} must not be empty")
        return None
    if any(ch in text for ch in "\r\n\x00"):
        raise ValidationError(f"{where} must be a single line without NULs")
    if len(text) > _MAX_TEXT:
        raise ValidationError(f"{where} must be at most {_MAX_TEXT} characters")
    return text


def _check_role(value: object) -> str:
    if value not in ROLES:
        raise ValidationError(
            f"unknown role {value!r}; valid roles: {', '.join(ROLES)}"
        )
    return str(value)


def _schema_version(data: dict, path: Path) -> int:
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError(f"{path} is corrupt: 'schema_version' must be an integer")
    if version > POLICY_SCHEMA_VERSION:
        raise StateError(
            f"{path} uses policy schema version {version}, newer than "
            f"supported version {POLICY_SCHEMA_VERSION}; this aperio will not "
            "interpret or convert it. Upgrade aperio before enabling or "
            "changing governance."
        )
    if version != POLICY_SCHEMA_VERSION:
        raise StateError(
            f"{path} uses unsupported policy schema version {version}; no "
            "migration is implemented for it"
        )
    return version


def validate_policy_document(data: object, path: Path | str) -> dict:
    """Validate a parsed policy document (unknown extension keys allowed)."""
    if not isinstance(data, dict):
        raise StateError(f"{path} is corrupt: policy must be a JSON object")
    _schema_version(data, Path(str(path)))
    version = data.get("policy_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise StateError(
            f"{path} is corrupt: 'policy_version' must be a positive integer"
        )
    if not isinstance(data.get("enabled"), bool):
        raise StateError(f"{path} is corrupt: 'enabled' must be a boolean")

    roles = data.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise StateError(
            f"{path} is corrupt: 'roles' must define exactly {', '.join(ROLES)}"
        )
    for role in ROLES:
        entry = roles[role]
        if not isinstance(entry, dict):
            raise StateError(f"{path} is corrupt: roles.{role} must be an object")
        actions = entry.get("actions")
        if not isinstance(actions, list) or not all(
            isinstance(action, str) for action in actions
        ):
            raise StateError(
                f"{path} is corrupt: roles.{role}.actions must be a list of strings"
            )
        if len(set(actions)) != len(actions):
            raise StateError(
                f"{path} is corrupt: roles.{role}.actions contains duplicates"
            )
        extra = set(actions) - set(ROLE_ACTIONS[role])
        if extra:
            raise StateError(
                f"{path} is corrupt: roles.{role} claims unsupported actions: "
                + ", ".join(sorted(extra))
            )

    actors = data.get("actors")
    if not isinstance(actors, dict):
        raise StateError(f"{path} is corrupt: 'actors' must be an object")
    for actor, assignment in actors.items():
        if not isinstance(actor, str) or not _ACTOR_RE.match(actor):
            raise StateError(f"{path} is corrupt: invalid actor id {actor!r}")
        if not isinstance(assignment, dict):
            raise StateError(
                f"{path} is corrupt: actors.{actor} must be an object"
            )
        _check_role_for_document(assignment.get("role"), path, actor)
        for field in ("assigned_at", "assigned_by"):
            value = assignment.get(field)
            if value is not None and not isinstance(value, str):
                raise StateError(
                    f"{path} is corrupt: actors.{actor}.{field} must be a string"
                )

    history = data.get("actor_history", [])
    if not isinstance(history, list):
        raise StateError(f"{path} is corrupt: 'actor_history' must be a list")
    for event in history:
        if not isinstance(event, dict):
            raise StateError(
                f"{path} is corrupt: actor_history entries must be objects"
            )
        for field in ("actor", "from_role", "to_role", "changed_at"):
            if not isinstance(event.get(field), str):
                raise StateError(
                    f"{path} is corrupt: actor_history.{field} must be a string"
                )

    separation = data.get("separation", {})
    if not isinstance(separation, dict):
        raise StateError(f"{path} is corrupt: 'separation' must be an object")
    for key, value in separation.items():
        if not isinstance(value, bool):
            raise StateError(
                f"{path} is corrupt: separation.{key} must be a boolean"
            )

    review = data.get("review", {})
    if not isinstance(review, dict):
        raise StateError(f"{path} is corrupt: 'review' must be an object")
    for key, value in review.items():
        if not isinstance(value, bool):
            raise StateError(
                f"{path} is corrupt: review.{key} must be a boolean"
            )
    return data


def _check_role_for_document(value: object, path: Path, actor: str) -> None:
    if value not in ROLES:
        raise StateError(
            f"{path} is corrupt: actors.{actor}.role must be one of "
            f"{', '.join(ROLES)}"
        )


def read_policy(aperio_dir: Path) -> dict | None:
    """Return the validated policy, or None when governance is not configured.

    A newer/unsupported schema is an error for mutating callers; read-only
    summaries use :func:`policy_status` instead.
    """
    data = _read_json(policy_path(aperio_dir), "project policy")
    if data is None:
        return None
    return validate_policy_document(data, policy_path(aperio_dir))


def policy_enabled(aperio_dir: Path) -> bool:
    """True when governance is explicitly enabled (never raises on schema)."""
    data = _read_json(policy_path(aperio_dir), "project policy")
    return bool(data and data.get("enabled") is True)


def load_policy(aperio_dir: Path) -> dict | None:
    """Policy for enforcement: required when the file exists."""
    policy = read_policy(aperio_dir)
    if policy is not None and policy.get("enabled") is not True:
        raise StateError(
            "project governance is configured but disabled "
            f"({policy_path(aperio_dir)}); enable it with "
            "'aperio policy-init' or remove governance for this project"
        )
    return policy


def _write_policy(aperio_dir: Path, data: dict, snapshot: dict | None) -> None:
    path = policy_path(aperio_dir)
    _reject_symlink(path, "project policy")
    tmp = aperio_dir / (POLICY_FILE_NAME + ".tmp")
    _reject_symlink(tmp, "policy temporary file")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise StateError(
            f"a leftover temporary file exists at {tmp}; it is never removed "
            "automatically. Inspect it and remove it yourself, then retry."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        if snapshot is not None:
            current = read_policy(aperio_dir)
            if current is not None and current.get("policy_version") != \
                    snapshot.get("policy_version"):
                raise StateError(
                    "the project policy changed while this update was "
                    "preparing; nothing was modified. Re-read it and retry."
                )
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_roles() -> dict:
    return {
        role: {"actions": list(ROLE_ACTIONS[role])}
        for role in ROLES
    }


def _empty_audit() -> dict:
    return {"schema_version": POLICY_SCHEMA_VERSION, "events": []}


def _read_audit(aperio_dir: Path) -> dict:
    path = audit_path(aperio_dir)
    data = _read_json(path, "policy audit log")
    if data is None:
        return _empty_audit()
    version = data.get("schema_version")
    if version != POLICY_SCHEMA_VERSION or not isinstance(data.get("events"), list):
        raise StateError(
            f"{path} is corrupt or uses an unsupported schema "
            f"({version!r}); aperio will not append to it"
        )
    return data


def _write_audit(aperio_dir: Path, data: dict) -> None:
    path = audit_path(aperio_dir)
    _reject_symlink(path, "policy audit log")
    tmp = aperio_dir / AUDIT_TMP_NAME
    _reject_symlink(tmp, "audit temporary file")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise StateError(
            f"a leftover temporary file exists at {tmp}; inspect and remove "
            "it manually, then retry."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_event(
    aperio_dir: Path,
    *,
    actor: str,
    role: str,
    action: str,
    target: str,
    revision: int | None,
    outcome: str,
    review_id: str | None = None,
    note: str | None = None,
) -> None:
    """Append one governed-action record (no-op when no policy is enabled).

    Called while the caller may already hold the plan/executions lock, so
    this function takes only ``policy.lock`` (later in the global order).
    """
    if not policy_enabled(aperio_dir):
        return
    actor = _check_actor_id(actor)
    role = _check_role(role)
    target = _check_text(target, where="target", required=True) or ""
    outcome = _check_text(outcome, where="outcome", required=True) or ""
    note = _check_text(note, where="note")
    token = acquire_lock(policy_lock_path(aperio_dir), timeout=5.0,
                         artifact="project policy")
    try:
        audit = _read_audit(aperio_dir)
        audit["events"].append({
            "actor": actor,
            "role": role,
            "action": action,
            "target": target,
            "revision": revision,
            "timestamp": utc_now_iso(),
            "outcome": outcome,
            "review_id": review_id,
            "note": note,
        })
        _write_audit(aperio_dir, audit)
    finally:
        release_lock(policy_lock_path(aperio_dir), token)


def initialize_policy(
    aperio_dir: Path,
    *,
    assignments: list[tuple[str, str]] | None = None,
    by: str | None = None,
) -> dict:
    """Create and enable governance. Refuses to replace an existing policy."""
    require_supported_state(aperio_dir)
    by = _check_text(by, where="--by")
    parsed: dict[str, dict] = {}
    for actor, role in assignments or []:
        actor = _check_actor_id(actor)
        role = _check_role(role)
        if actor in parsed:
            raise ValidationError(f"actor {actor!r} is assigned more than once")
        parsed[actor] = {
            "role": role,
            "assigned_at": utc_now_iso(),
            "assigned_by": by,
        }
    token = acquire_lock(policy_lock_path(aperio_dir), timeout=5.0,
                         artifact="project policy")
    try:
        if policy_path(aperio_dir).exists():
            raise StateError(
                f"a project policy already exists at {policy_path(aperio_dir)}; "
                "aperio never replaces it silently. Use 'aperio policy-assign' "
                "to change actors."
            )
        now = utc_now_iso()
        document = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "policy_version": 1,
            "enabled": True,
            "roles": _default_roles(),
            "actors": parsed,
            "actor_history": [],
            "separation": {
                "plan_approval_requires_independent_actor": True,
                "review_requires_independent_reviewer": True,
            },
            "review": {"require_independent_review_for_completion": True},
            "created_at": now,
            "updated_at": now,
            "aperio_version": __version__,
        }
        validate_policy_document(document, policy_path(aperio_dir))
        _write_policy(aperio_dir, document, None)
        audit = _read_audit(aperio_dir)
        audit["events"].append({
            "actor": "system",
            "role": "planner",
            "action": "policy.init",
            "target": POLICY_FILE_NAME,
            "revision": 1,
            "timestamp": now,
            "outcome": "enabled",
            "review_id": None,
            "note": f"{len(parsed)} actor assignment(s)",
        })
        _write_audit(aperio_dir, audit)
        return document
    finally:
        release_lock(policy_lock_path(aperio_dir), token)


def assign_actor(
    aperio_dir: Path, actor: str, role: str, *, by: str | None = None
) -> dict:
    """Assign or change a declared actor's role, preserving history."""
    actor = _check_actor_id(actor)
    role = _check_role(role)
    by = _check_text(by, where="--by")
    require_supported_state(aperio_dir)
    token = acquire_lock(policy_lock_path(aperio_dir), timeout=5.0,
                         artifact="project policy")
    try:
        policy = read_policy(aperio_dir)
        if policy is None:
            raise StateError(
                "no project policy is configured; run 'aperio policy-init' "
                "first"
            )
        existing = policy["actors"].get(actor)
        if existing is not None and existing.get("role") == role:
            return policy
        updated = dict(policy)
        actors = dict(policy["actors"])
        history = list(policy.get("actor_history", []))
        if existing is not None:
            history.append({
                "actor": actor,
                "from_role": existing.get("role"),
                "to_role": role,
                "changed_at": utc_now_iso(),
                "changed_by": by,
            })
        actors[actor] = {
            "role": role,
            "assigned_at": utc_now_iso(),
            "assigned_by": by,
        }
        updated["actors"] = actors
        updated["actor_history"] = history
        updated["policy_version"] = policy["policy_version"] + 1
        updated["updated_at"] = utc_now_iso()
        updated["aperio_version"] = __version__
        validate_policy_document(updated, policy_path(aperio_dir))
        _write_policy(aperio_dir, updated, policy)
    finally:
        release_lock(policy_lock_path(aperio_dir), token)
    append_event(
        aperio_dir,
        actor=by or actor,
        role=role,
        action="policy.assign",
        target=f"actor:{actor}",
        revision=updated["policy_version"],
        outcome="assigned",
    )
    return updated


def policy_status(aperio_dir: Path) -> dict:
    """Read-only policy summary; never raises for a future schema."""
    path = policy_path(aperio_dir)
    data = _read_json(path, "project policy")
    if data is None:
        return {"present": False, "enabled": False}
    try:
        validate_policy_document(data, path)
    except StateError as exc:
        return {
            "present": True,
            "enabled": False,
            "unsupported": True,
            "schema_version": data.get("schema_version"),
            "detail": str(exc),
        }
    return {
        "present": True,
        "enabled": bool(data.get("enabled")),
        "schema_version": data["schema_version"],
        "policy_version": data["policy_version"],
        "actors": {
            actor: assignment["role"]
            for actor, assignment in data["actors"].items()
        },
        "review": dict(data.get("review", {})),
        "separation": dict(data.get("separation", {})),
    }


def _role_for(policy: dict, actor: str) -> str:
    assignment = policy["actors"].get(actor)
    if assignment is None:
        raise StateError(
            f"actor {actor!r} is not assigned in the project policy. Declared "
            "actor ids are not authenticated identities; add the assignment "
            "explicitly with 'aperio policy-assign' (roles are never guessed)."
        )
    return assignment["role"]


def require_action(
    aperio_dir: Path,
    actor: str | None,
    action: str,
    *,
    target: str,
    revision: int | None = None,
) -> str | None:
    """Validate that a declared actor may perform ``action``.

    Returns the actor's role when governance is enabled, or None when no
    policy exists (legacy projects: unchanged behavior). A caller-supplied
    role is never accepted; the mapping is the only source of truth.
    """
    policy = load_policy(aperio_dir)
    if policy is None:
        return None
    if actor is None:
        raise StateError(
            f"governance is enabled: {action} requires --actor <id>. "
            "Declared actor ids are workflow declarations, not authenticated "
            "identity."
        )
    actor = _check_actor_id(actor, where="--actor")
    role = _role_for(policy, actor)
    if action not in policy["roles"][role]["actions"]:
        raise StateError(
            f"actor {actor!r} has role {role!r}, which is not permitted to "
            f"perform {action}. A role label supplied by a caller is never "
            "trusted; update the policy mapping if this is intended."
        )
    return role


def latest_plan_author(aperio_dir: Path, revision: int) -> str | None:
    """Author of a plan revision from the append-only audit log."""
    if not policy_enabled(aperio_dir):
        return None
    audit = _read_audit(aperio_dir)
    author = None
    for event in audit["events"]:
        if (
            event.get("action") in ("plan.create", "plan.revise")
            and event.get("revision") == revision
        ):
            author = event.get("actor")
    return author


def policy_summary(aperio_dir: Path) -> dict:
    """Governance block for the portable memory API (additive schema)."""
    try:
        status = policy_status(aperio_dir)
    except StateError as exc:
        return {
            "enabled": False,
            "policy_version": None,
            "actors": {},
            "unsupported": True,
            "detail": str(exc),
        }
    if not status.get("present"):
        return {"enabled": False, "policy_version": None, "actors": {}}
    if status.get("unsupported"):
        return {
            "enabled": False,
            "policy_version": None,
            "actors": {},
            "unsupported_schema": status.get("schema_version"),
            "detail": status.get("detail"),
        }
    return {
        "enabled": status["enabled"],
        "policy_version": status["policy_version"],
        "actors": status.get("actors", {}),
        "review": status.get("review", {}),
        "separation": status.get("separation", {}),
    }
