"""Explicit, versioned schema-upgrade foundation (Feature 05).

Aperio detects schema versions independently for each artifact:

===========  ==========================  ====================
artifact     file                        version source
===========  ==========================  ====================
state        ``.aperio/state.json``      ``schema_version``
plan         ``.aperio/plan.json``       ``schema_version``
executions   ``.aperio/executions.json`` ``schema_version``
handoff      ``.aperio/handoff.md``      ``<!-- aperio:handoff:vN -->``
===========  ==========================  ====================

The framework is deliberately conservative:

- **Nothing is upgraded implicitly.** ``aperio upgrade`` is a dry-run
  preview; ``--apply`` is required to write.
- **Only explicitly registered migrations run.** A migration is a named
  function for one ``(artifact, from_version, to_version)`` edge. There is
  no inference, no "best effort" rewrite, and no fabricated migration
  path: as of this release no older schema exists, so the registry is
  empty and every detected version is either supported or refused.
- **Unknown future versions are refused.** A newer schema is never
  converted, never downgraded, and never edited; the error tells the
  operator to upgrade aperio instead.
- **Backups precede writes.** Any apply that would migrate copies the
  original bytes to ``.aperio/local/backups/upgrade-<utc>-<pid>/`` first
  (machine-local, git-ignored), records SHA-256 checksums, and restores
  the original bytes if a write fails. The backup is never deleted
  automatically.
- **Extension fields survive.** Migrations receive the full parsed JSON
  object (unknown keys included) and must return the full object; only the
  fields a migration intentionally changes are different.
- **Handoff is text, not JSON.** Its format version is detected and
  reported, but no text migration is implemented; a newer handoff format
  is preserved verbatim (Feature 02 semantics).

Crash consistency matches the rest of aperio: atomic rename per file, no
``fsync``. A crash between two artifact upgrades leaves earlier artifacts
upgraded and later ones untouched — the backup and the preview make that
state explicit rather than silent.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aperio.artifacts import check_artifact_dir
from aperio.errors import (
    AperioError,
    NotInitializedError,
    PathError,
    StateError,
)
from aperio.handoff import handoff_path, parse_handoff_text
from aperio.paths import resolve_target
from aperio.planstore import plan_path
from aperio.project import aperio_dir_for
from aperio.runtime import executions_path
from aperio.schema import state_path, utc_now_iso

# Versions this aperio can read and write, per artifact.
SUPPORTED_SCHEMAS: dict[str, int] = {"state": 1, "plan": 1, "executions": 1}

_JSON_ARTIFACTS: dict[str, Callable[[Path], Path]] = {
    "state": lambda aperio_dir: aperio_dir / "state.json",
    "plan": plan_path,
    "executions": executions_path,
}


@dataclass(frozen=True)
class Migration:
    """One explicitly implemented schema edge.

    ``apply`` receives the full parsed JSON document (unknown extension
    fields included) and must return the full document with
    ``schema_version`` set to ``to_version``. It is only called after the
    originals have been backed up.
    """

    artifact: str
    from_version: int
    to_version: int
    description: str
    apply: Callable[[dict], dict]


# No older schema exists in this release. A future migration is added here
# with its tests; nothing is inferred.
MIGRATIONS: tuple[Migration, ...] = ()


def _migration_for(artifact: str, from_version: int) -> Migration | None:
    matches = [
        migration
        for migration in MIGRATIONS
        if migration.artifact == artifact
        and migration.from_version == from_version
        and migration.to_version == from_version + 1
    ]
    if len(matches) > 1:
        raise StateError(
            f"schema upgrade is ambiguous: {len(matches)} migrations are "
            f"registered for {artifact} v{from_version}→v{from_version + 1}"
        )
    return matches[0] if matches else None


def _read_json_version(path: Path) -> tuple[int | None, str | None]:
    """Return ``(schema_version, error)`` for one artifact file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{path.name} is not valid UTF-8 JSON ({exc})"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a JSON object"
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None, f"{path.name} has no integer schema_version"
    return version, None


def _describe(artifact: str, path: Path | None, current: int | None,
              supported: int | None, error: str | None) -> dict:
    entry: dict = {
        "artifact": artifact,
        "path": None if path is None else str(path),
        "present": path is not None and path.exists(),
        "current_version": current,
        "supported_version": supported,
        "action": "none",
        "description": "",
    }
    if error is not None:
        entry["action"] = "blocked"
        entry["description"] = error
        return entry
    if path is None or not path.exists():
        entry["description"] = "not present (nothing to upgrade)"
        return entry
    if supported is None:
        entry["action"] = "blocked"
        entry["description"] = (
            "no migration support is defined for this artifact type"
        )
        return entry
    if current is not None and current > supported:
        entry["action"] = "blocked"
        entry["description"] = (
            f"schema v{current} is newer than supported v{supported}; "
            "upgrade aperio instead — aperio will not convert it"
        )
        return entry
    if current == supported:
        entry["description"] = "current"
        return entry
    if current is None:
        entry["action"] = "blocked"
        entry["description"] = "schema version is missing"
        return entry
    # Older version: only an explicitly implemented edge can migrate it.
    chain = []
    version = current
    while version < supported:
        migration = _migration_for(artifact, version)
        if migration is None:
            break
        chain.append(migration)
        version += 1
    if version != supported:
        entry["action"] = "blocked"
        entry["description"] = (
            f"no implemented migration path from v{current} to v{supported}; "
            "aperio never invents migrations"
        )
    else:
        entry["action"] = "migrate"
        entry["description"] = " → ".join(
            [f"v{current}"] + [f"v{m.to_version}" for m in chain]
        )
    return entry


def plan_upgrade(project_path: str | Path) -> dict:
    """Detect schema versions and report what an upgrade would do (read-only)."""
    target = resolve_target(str(project_path), for_writing=False)
    aperio_dir = aperio_dir_for(target)
    if os.path.islink(aperio_dir):
        raise PathError(
            f"refusing to follow symlink at {aperio_dir}; remove it and "
            "re-run 'aperio init' to repair"
        )
    if not state_path(aperio_dir).exists():
        raise NotInitializedError(
            f"not an Aperio project: no {state_path(aperio_dir)} "
            "(run 'aperio init <project-directory>')"
        )
    artifacts: list[dict] = []

    for artifact, path_fn in _JSON_ARTIFACTS.items():
        path = path_fn(aperio_dir)
        supported = SUPPORTED_SCHEMAS.get(artifact)
        if not path.exists():
            artifacts.append(_describe(artifact, path, None, supported, None))
            continue
        if path.is_symlink():
            artifacts.append(
                _describe(
                    artifact, path, None, supported,
                    f"{path.name} is a symlink; remove it and run "
                    "'aperio init' to restore a regular file",
                )
            )
            continue
        version, error = _read_json_version(path)
        artifacts.append(
            _describe(artifact, path, version, supported, error)
        )

    handoff = handoff_path(aperio_dir)
    if handoff.exists() and not handoff.is_symlink():
        try:
            parsed = parse_handoff_text(handoff.read_text(encoding="utf-8"))
            detected = (
                "legacy" if parsed.structured is False
                else f"v{parsed.version}"
            )
            action = "none"
            description = f"format {detected} (text format; preserved verbatim)"
            if parsed.structured is None:
                description = (
                    f"format v{parsed.version} is newer than this aperio "
                    "understands; preserved verbatim, never converted"
                )
        except (OSError, UnicodeDecodeError) as exc:
            action = "blocked"
            description = f"cannot read handoff.md: {exc}"
            detected = None
        except AperioError as exc:
            action = "blocked"
            description = str(exc)
            detected = None
        artifacts.append(
            {
                "artifact": "handoff",
                "path": str(handoff),
                "present": True,
                "current_version": detected,
                "supported_version": "v1",
                "action": action,
                "description": description,
            }
        )
    else:
        artifacts.append(
            {
                "artifact": "handoff",
                "path": str(handoff),
                "present": handoff.exists(),
                "current_version": None,
                "supported_version": "v1",
                "action": "blocked" if handoff.is_symlink() else "none",
                "description": (
                    "handoff.md is a symlink"
                    if handoff.is_symlink()
                    else "not present (nothing to upgrade)"
                ),
            }
        )

    ok = all(entry["action"] != "blocked" for entry in artifacts)
    return {
        "command": "upgrade",
        "dry_run": True,
        "applied": False,
        "ok": ok,
        "artifacts": artifacts,
        "backup": None,
        "warnings": [],
    }


def _backup_dir(aperio_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = check_artifact_dir(aperio_dir, "local", "backups")
    candidate = base / f"upgrade-{stamp}-{os.getpid()}"
    try:
        candidate.mkdir()
    except OSError as exc:
        raise StateError(
            f"cannot create the upgrade backup directory {candidate}: "
            f"{exc.strerror or exc}; nothing was upgraded"
        ) from None
    if os.path.islink(candidate) or not candidate.is_dir():
        raise PathError(
            f"{candidate} is not a safe backup directory; nothing was upgraded"
        )
    return candidate


def _atomic_replace(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".upgrade.tmp")
    if os.path.islink(tmp):
        raise PathError(f"refusing to follow symlink at {tmp}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except OSError as exc:
        raise StateError(
            f"cannot prepare the upgrade of {path.name}: {exc.strerror or exc}; "
            f"the temporary name {tmp} may be occupied and is never deleted "
            "automatically"
        ) from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise StateError(
            f"cannot write the upgraded {path.name}: {exc.strerror or exc}; "
            "the original bytes are preserved in the backup"
        ) from None


def apply_upgrade(project_path: str | Path) -> dict:
    """Apply only implemented migrations, with backup + rollback on failure."""
    preview = plan_upgrade(project_path)
    target = resolve_target(str(project_path), for_writing=False)
    aperio_dir = aperio_dir_for(target)

    if not preview["ok"]:
        blocked = [
            a["artifact"] for a in preview["artifacts"] if a["action"] == "blocked"
        ]
        raise StateError(
            "schema upgrade is not possible: "
            + ", ".join(blocked)
            + " cannot be upgraded safely. Nothing was modified; the preview "
            "explains each artifact ('aperio upgrade <dir>')."
        )
    to_migrate = [a for a in preview["artifacts"] if a["action"] == "migrate"]
    if not to_migrate:
        preview["dry_run"] = False
        preview["applied"] = False
        return preview

    # Build every migrated payload before touching disk, so a failing
    # migration cannot leave a half-upgraded project.
    prepared: list[tuple[dict, bytes, bytes]] = []
    for entry in to_migrate:
        artifact = entry["artifact"]
        path = Path(entry["path"])
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise StateError(
                f"cannot read {path.name} for upgrade: {exc.strerror or exc}; "
                "nothing was modified"
            ) from None
        data = json.loads(original.decode("utf-8"))
        current = entry["current_version"]
        supported = entry["supported_version"]
        while current < supported:
            migration = _migration_for(artifact, current)
            if migration is None:  # pragma: no cover - preview guaranteed it
                raise StateError(
                    f"migration for {artifact} v{current} disappeared; nothing "
                    "was modified"
                )
            migrated = migration.apply(dict(data))
            if not isinstance(migrated, dict):
                raise StateError(
                    f"migration {migration.description!r} did not return a JSON "
                    "object; nothing was modified"
                )
            if migrated.get("schema_version") != migration.to_version:
                raise StateError(
                    f"migration {migration.description!r} did not set "
                    f"schema_version to {migration.to_version}; nothing was "
                    "modified"
                )
            data = migrated
            current = migration.to_version
        prepared.append(
            (
                entry,
                original,
                (json.dumps(data, indent=2) + "\n").encode("utf-8"),
            )
        )

    backup_dir = _backup_dir(aperio_dir)
    backup_manifest: dict = {}
    for entry, original, _ in prepared:
        backup_path = backup_dir / Path(entry["path"]).name
        try:
            with backup_path.open("xb") as fh:
                fh.write(original)
        except OSError as exc:
            raise StateError(
                f"cannot write the upgrade backup at {backup_path}: "
                f"{exc.strerror or exc}; nothing was upgraded"
            ) from None
        backup_manifest[entry["artifact"]] = {
            "path": entry["path"],
            "sha256": hashlib.sha256(original).hexdigest(),
            "skipped": False,
        }
    manifest_path = backup_dir / "backup.json"
    try:
        with manifest_path.open("xb") as fh:
            fh.write(
                (
                    json.dumps(
                        {
                            "created_at": utc_now_iso(),
                            "artifacts": backup_manifest,
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8")
            )
    except OSError as exc:
        raise StateError(
            f"cannot write the upgrade backup manifest at {manifest_path}: "
            f"{exc.strerror or exc}; nothing was upgraded"
        ) from None

    written: list[tuple[Path, bytes]] = []
    try:
        for entry, original, payload in prepared:
            path = Path(entry["path"])
            try:
                current_bytes = path.read_bytes()
            except OSError as exc:
                raise StateError(
                    f"cannot re-read {path.name} before upgrading: "
                    f"{exc.strerror or exc}; the backup contains the original"
                ) from None
            if current_bytes != original:
                raise StateError(
                    f"{path.name} changed while the upgrade was being prepared; "
                    f"nothing was modified. The backup is at {backup_dir}."
                )
            _atomic_replace(path, payload)
            written.append((path, original))
    except BaseException:
        for path, original in reversed(written):
            try:
                _atomic_replace(path, original)
            except AperioError:  # pragma: no cover - best effort
                pass
        raise

    preview["dry_run"] = False
    preview["applied"] = True
    preview["backup"] = str(backup_dir)
    preview["warnings"].append(
        "a backup of the original schema was written under .aperio/local/ "
        "and is never deleted automatically"
    )
    return preview


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SUPPORTED_SCHEMAS",
    "apply_upgrade",
    "plan_upgrade",
]
