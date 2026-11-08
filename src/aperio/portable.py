"""Portable project export and import (Feature 05).

Format (v1): one ZIP archive containing a versioned manifest and the
UTF-8 text files that make up a project's shareable context.

::

    manifest.json                 format + per-file size/SHA-256 + schemas
    files/state.json
    files/brief.md
    files/decisions.md
    files/handoff.md
    files/plan.json               (when the project has a plan)
    files/executions.json         (portable projection, see below)
    files/runs/RUN-0001.handoff.md (awaiting-external runs only)

Only an explicit whitelist is exported: the files above. ``.aperio/local/``
(local logs, external reports, credentials), locks, temporary files, the
generated ``.gitignore``, and anything else are excluded by construction —
the exporter never walks the directory tree.

Portable projection: execution records are normalized so that
machine-specific fields cannot travel silently. ``pid`` and ``machine``
are set to null (a pid means nothing on another machine), and
``local_log_ref`` is dropped (local logs are never exported). Everything
else — status, outcome, timestamps, handoff references, exit codes — is
preserved. Archives that contain an active (prepared/running) run are
exported without execution metadata instead, because a live process cannot
be handed off.

An imported archive may not carry active runs or any machine-local process
identity. The importer **rejects** a ``pid``, ``machine``, or
``local_log_ref`` in any execution record instead of warning: cancelling a
run signals the recorded pid, so an imported identity could otherwise
target an unrelated local process. Only a locally recorded run whose
machine matches this host can ever be signalled.

Honesty limits (documented, not implied away):

- SHA-256 in the manifest detects corruption and accidental modification.
  It does **not** authenticate the archive (no signatures, no keys) and
  cannot prove that manually authored shareable files contain no secrets.
  Review what you share.
- Import is validated completely before filesystem writes, and writes are
  rolled back on error within this process. It is not a filesystem
  transaction: a crash or power loss mid-import can leave a subset of the
  files in place. ``state.json`` is written last, so an interrupted import
  never leaves a project that merely *claims* to be imported; rerun the
  import after inspecting the destination (identical bytes are accepted as
  unchanged), or remove the partial ``.aperio/`` directory.
- Import never deletes existing files: the effective post-import context
  (archive plus retained artifacts) is validated before any write, so an
  incompatible combination is refused with every existing byte preserved.
- Whole-project operations coordinate through the writer locks in the
  fixed order documented in ``LOCK_ORDER``. Export reads under those locks
  (one consistent snapshot); import validates and applies under them and
  rejects a destination that changed while it was preparing. A crashed or
  non-cooperating external writer is still outside this contract.
- No ``fsync``: crash consistency is best-effort, matching the rest of
  aperio.

Security properties: every manifest path is re-validated against the
whitelist and canonical shape (no absolute paths, no ``..``, no
backslashes, no NULs, no hidden components, no ``local/``); archive members
are checked for directory/symlink/encrypted entries; decompressed sizes are
bounded and matched against the manifest; duplicate names (including
case-insensitive collisions) are rejected. Files are never extracted with
``ZipFile.extract``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from aperio import __version__
from aperio.artifacts import (
    check_artifact_dir,
    discard_created_file,
    open_exclusive_binary,
)
from aperio.decisions import (
    DECISIONS_LOCK_NAME,
    decisions_path,
    parse_decisions_text,
)
from aperio.errors import (
    AperioError,
    PathError,
    StateError,
    ValidationError,
)
from aperio.handoff import HANDOFF_LOCK_NAME, handoff_path, parse_handoff_text
from aperio.locks import acquire_lock, release_lock
from aperio.paths import resolve_target
from aperio.planmodel import (
    PLAN_SCHEMA_VERSION,
    validate_plan_document,
)
from aperio.policy import (
    POLICY_LOCK_NAME,
    POLICY_SCHEMA_VERSION,
    policy_path,
    validate_policy_document,
)
from aperio.planstore import PLAN_LOCK_NAME, plan_path
from aperio.reviews import (
    REVIEW_LOCK_NAME,
    REVIEWS_SCHEMA_VERSION,
    reviews_path,
    validate_reviews_document,
)
from aperio.progress import STATE_LOCK_NAME
from aperio.project import (
    aperio_dir_for,
    ensure_local_scaffolding,
    require_supported_state,
)
from aperio.runtime import (
    EXECUTIONS_SCHEMA_VERSION,
    dispatch_lock_path,
    executions_lock_path,
    read_executions,
    read_run_handoff,
    validate_executions_document,
)
from aperio.schema import parse_state_bytes, utc_now_iso

EXPORT_FORMAT = "aperio-portable-export"
EXPORT_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = (1,)

MANIFEST_NAME = "manifest.json"
MEMBER_DIR = "files"

MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 200
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
# Generous ceiling for a single archive file on disk; the decompressed
# limits above are the real protection.
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
IMPORT_LOCK_TIMEOUT = 5.0

#: Global lock order for whole-project operations (export/import). Every
#: other Aperio writer takes exactly one of these locks, so acquiring them
#: in this fixed order cannot deadlock: a caller never holds a later lock
#: while waiting for an earlier one.
LOCK_ORDER: tuple[str, ...] = (
    "import",
    "dispatch",
    "plan",
    "executions",
    "state",
    "handoff",
    "decisions",
    "reviews",
    "policy",
)

TOP_LEVEL_FILES: tuple[str, ...] = (
    "state.json",
    "brief.md",
    "decisions.md",
    "handoff.md",
    "plan.json",
    "executions.json",
    "policies.json",
    "reviews.json",
    "policy-audit.json",
)

RUN_HANDOFF_PATTERN = re.compile(r"^runs/RUN-[0-9]+\.handoff\.md$")
_RESERVED_DIR_NAMES = frozenset({"local"})
_EXCLUDED_NOTE = (
    "Hashes detect corruption only. They do not authenticate this archive "
    "and cannot prove that hand-authored shareable files contain no secrets."
)
EXCLUDED_PATHS: tuple[str, ...] = (
    ".aperio/local/ (local logs, external reports, credentials)",
    "lock files",
    "atomic-write temporary files",
    ".gitignore (generated project scaffolding)",
)


def _lock_targets(aperio_dir: Path) -> dict[str, Path]:
    return {
        "import": aperio_dir / "import.lock",
        "dispatch": dispatch_lock_path(aperio_dir),
        "plan": aperio_dir / PLAN_LOCK_NAME,
        "executions": executions_lock_path(aperio_dir),
        "state": aperio_dir / STATE_LOCK_NAME,
        "handoff": aperio_dir / HANDOFF_LOCK_NAME,
        "decisions": aperio_dir / DECISIONS_LOCK_NAME,
        "reviews": aperio_dir / REVIEW_LOCK_NAME,
        "policy": aperio_dir / POLICY_LOCK_NAME,
    }


@contextmanager
def coordination_locks(aperio_dir: Path, *, include_import: bool):
    """Hold every Aperio writer lock in the global order.

    Cooperative writers (task-status, plan-revise, phase-set, handoff-write,
    decision-add, dispatch, run-report, another import) are serialized for
    the duration, so a read produces one consistent snapshot and a write
    cannot interleave with a conflicting mutation. Bounded wait: if a
    writer holds a lock past the timeout, the operation fails with the
    writer-friendly lock error instead of hanging or forcing the lock.
    """
    targets = _lock_targets(aperio_dir)
    names = LOCK_ORDER if include_import else LOCK_ORDER[1:]
    held: list[tuple[str, str]] = []
    try:
        for name in names:
            try:
                held.append(
                    (
                        name,
                        acquire_lock(
                            targets[name],
                            timeout=IMPORT_LOCK_TIMEOUT,
                            artifact=name,
                        ),
                    )
                )
            except AperioError as exc:
                raise StateError(
                    f"cannot take a consistent project snapshot: another "
                    f"aperio writer holds the {name} lock ({exc}). Nothing "
                    "was modified."
                ) from None
        yield
    finally:
        for name, token in reversed(held):
            release_lock(targets[name], token)


def _destination_snapshot(aperio_dir: Path) -> dict[str, tuple[int, int, int]]:
    """Cheap identity snapshot of the portable destination context."""
    snapshot: dict[str, tuple[int, int, int]] = {}
    for rel in TOP_LEVEL_FILES:
        path = aperio_dir / rel
        if path.is_symlink():
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if path.is_file():
            snapshot[rel] = (info.st_size, info.st_mtime_ns, info.st_ino)
    runs = aperio_dir / "runs"
    if runs.is_dir() and not runs.is_symlink():
        try:
            children = sorted(runs.iterdir())
        except OSError:
            children = []
        for child in children:
            rel = f"runs/{child.name}"
            if not RUN_HANDOFF_PATTERN.match(rel):
                continue
            try:
                info = child.stat()
            except OSError:
                continue
            if child.is_file() and not child.is_symlink():
                snapshot[rel] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return snapshot


def _read_destination_file(aperio_dir: Path, rel: str) -> bytes | None:
    path = aperio_dir / rel
    if path.is_symlink():
        raise PathError(
            f"refusing to read {rel!r} through symlink at {path}; remove the "
            "link and re-run the command"
        )
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError as exc:
        raise StateError(
            f"cannot read existing {path}: {exc.strerror or exc}"
        ) from None


def _destination_handoff_exists(aperio_dir: Path, rel: str) -> bool:
    if rel not in TOP_LEVEL_FILES and not RUN_HANDOFF_PATTERN.match(rel):
        return False
    path = aperio_dir / rel
    if os.path.islink(path):
        return False
    return path.is_file()


def _effective_context(
    aperio_dir: Path, archive_files: dict[str, bytes]
) -> dict[str, bytes]:
    """Imported files plus destination artifacts that will remain.

    Import never deletes: every existing shareable artifact not replaced by
    the archive stays part of the project, so it must be validated together
    with the imported content.
    """
    effective = dict(archive_files)
    for rel in (
        "plan.json",
        "executions.json",
        "policies.json",
        "reviews.json",
    ):
        if rel in effective:
            continue
        current = _read_destination_file(aperio_dir, rel)
        if current is not None:
            effective[rel] = current
    return effective


def _validate_governance_context(effective: dict[str, bytes], plan: dict | None) -> None:
    """Policy/review merge rules (import never drops attribution)."""
    policy = None
    if "policies.json" in effective:
        policy = _decode_json(effective["policies.json"], "effective:policies.json")
        version = policy.get("schema_version")
        if version != POLICY_SCHEMA_VERSION:
            raise StateError(
                "the effective post-import policies.json uses schema version "
                f"{version}, which this aperio does not support; importing "
                "would interpret governance it cannot understand. Nothing "
                "was modified."
            )
        validate_policy_document(policy, Path("effective:policies.json"))

    if "reviews.json" not in effective:
        return
    reviews = _decode_json(effective["reviews.json"], "effective:reviews.json")
    version = reviews.get("schema_version")
    if version != REVIEWS_SCHEMA_VERSION:
        raise StateError(
            "the effective post-import reviews.json uses schema version "
            f"{version}, which this aperio does not support. Nothing was "
            "modified."
        )
    validate_reviews_document(reviews, Path("effective:reviews.json"))
    if reviews["reviews"] and policy is None:
        raise StateError(
            "the effective post-import context would contain review history "
            "without a policies.json; approvals must stay attributable to a "
            "configured actor mapping. Nothing was modified."
        )
    if not reviews["reviews"]:
        return
    if plan is None:
        raise StateError(
            "the effective post-import context contains reviews but no plan; "
            "review targets cannot be validated. Nothing was modified."
        )
    tasks = {task["id"] for task in plan["tasks"]}
    for review in reviews["reviews"]:
        target = review["target"]
        if target["plan_revision"] > plan["plan_version"]:
            raise StateError(
                f"review {review['review_id']} targets plan revision "
                f"{target['plan_revision']}, newer than the effective plan "
                f"revision {plan['plan_version']}. Nothing was modified."
            )
        if target["kind"] == "task" and target["task_id"] not in tasks:
            raise StateError(
                f"review {review['review_id']} targets task "
                f"{target['task_id']}, which the effective post-import plan "
                "does not define. Nothing was modified."
            )


def _validate_merged_context(
    aperio_dir: Path,
    archive_files: dict[str, bytes],
    archive_run_ids: set[str],
) -> None:
    """Validate the effective post-import context before writing anything.

    The merged view is the archive overlaid on the files that remain in the
    destination. It must not pair a plan with execution records or handoffs
    that contradict it, and it must never leave an active run pointing at a
    task the effective plan does not define.
    """
    effective = _effective_context(aperio_dir, archive_files)
    plan = None
    if "plan.json" in effective:
        plan = _decode_json(effective["plan.json"], "effective:plan.json")
        version = plan.get("schema_version")
        if version != PLAN_SCHEMA_VERSION:
            raise StateError(
                "the effective post-import plan.json (archive or retained) "
                f"uses schema version {version}, but this aperio supports "
                f"{PLAN_SCHEMA_VERSION}; importing would combine incompatible "
                "artifacts. Nothing was modified."
            )
        validate_plan_document(plan, Path("effective:plan.json"))

    runs = None
    if "executions.json" in effective:
        runs = _decode_json(effective["executions.json"], "effective:executions.json")
        version = runs.get("schema_version")
        if version != EXECUTIONS_SCHEMA_VERSION:
            raise StateError(
                "the effective post-import executions.json (archive or "
                f"retained) uses schema version {version}, but this aperio "
                f"supports {EXECUTIONS_SCHEMA_VERSION}; importing would "
                "combine incompatible artifacts. Nothing was modified."
            )
        validate_executions_document(runs, Path("effective:executions.json"))

    _validate_governance_context(effective, plan)
    if not runs or not runs["runs"]:
        return
    if plan is None:
        raise StateError(
            "the effective post-import context would contain execution "
            "records but no plan.json (the archive omits it and the "
            "destination has none). Importing would break the task/run "
            "relationship. Nothing was modified."
        )

    tasks = {task["id"] for task in plan["tasks"]}
    plan_replaced = "plan.json" in archive_files
    for run in runs["runs"]:
        run_id = run["run_id"]
        if run["task_id"] not in tasks:
            raise StateError(
                f"run {run_id} references task {run['task_id']}, which the "
                "effective post-import plan does not define. Importing would "
                "leave an execution record pointing at a removed or renamed "
                "task (existing context is never deleted to hide this). "
                "Nothing was modified."
            )
        if run["plan_version"] > plan["plan_version"]:
            raise StateError(
                f"run {run_id} was recorded against plan revision "
                f"{run['plan_version']}, newer than the effective post-import "
                f"plan revision {plan['plan_version']}. Importing would make "
                "the run appear stale. Nothing was modified."
            )
        ref = run.get("handoff_ref")
        if run["status"] == "awaiting-external":
            if not ref:
                raise StateError(
                    f"awaiting-external run {run_id} has no handoff_ref; "
                    "importing would leave it undeliverable. Nothing was "
                    "modified."
                )
            if ref not in archive_files and not _destination_handoff_exists(
                aperio_dir, ref
            ):
                raise StateError(
                    f"awaiting-external run {run_id} references handoff "
                    f"{ref!r}, which neither the archive nor the destination "
                    "provides. Importing would leave the run waiting for a "
                    "missing handoff. Nothing was modified."
                )
        if (
            plan_replaced
            and run_id not in archive_run_ids
            and run["status"] in ("prepared", "running", "awaiting-external")
            and run["plan_version"] != plan["plan_version"]
        ):
            raise StateError(
                f"retained active run {run_id} ({run['status']}) was recorded "
                f"against plan revision {run['plan_version']}, but the import "
                f"replaces the plan at revision {plan['plan_version']}. "
                "Importing would pair a live handoff with a different plan. "
                "Finish or cancel the run, or import a plan at the same "
                "revision. Nothing was modified."
            )


def _validate_export_relationships(files: dict[str, bytes]) -> None:
    """Refuse to package a self-inconsistent exported snapshot."""
    if "executions.json" not in files:
        return
    runs = _decode_json(files["executions.json"], "export:executions.json")
    plan = (
        _decode_json(files["plan.json"], "export:plan.json")
        if "plan.json" in files
        else None
    )
    if plan is None and runs["runs"]:
        raise StateError(
            "cannot export: execution records exist but plan.json is absent; "
            "the archive would contain an unusable task/run combination"
        )
    tasks = {task["id"] for task in (plan["tasks"] if plan else [])}
    for run in runs["runs"]:
        if plan is not None and run["task_id"] not in tasks:
            raise StateError(
                f"cannot export: run {run['run_id']} references task "
                f"{run['task_id']}, which the current plan does not define. "
                "Run 'aperio integrity' and resolve this before exporting."
            )
        if plan is not None and run["plan_version"] > plan["plan_version"]:
            raise StateError(
                f"cannot export: run {run['run_id']} is recorded against plan "
                f"revision {run['plan_version']}, newer than plan.json "
                f"({plan['plan_version']}). Run 'aperio integrity' before "
                "exporting."
            )
        ref = run.get("handoff_ref")
        if run["status"] == "awaiting-external" and ref not in files:
            raise StateError(
                f"cannot export: awaiting-external run {run['run_id']} "
                f"references handoff {ref!r}, which is not part of the "
                "snapshot"
            )


def _is_exportable(rel: str) -> bool:
    """The one whitelist: nothing outside it can be exported or imported."""
    return rel in TOP_LEVEL_FILES or bool(RUN_HANDOFF_PATTERN.match(rel))


def validate_rel_path(rel: object) -> str:
    """Validate and return one portable relative path.

    Canonical shape only: ``name`` or ``runs/RUN-NNNN.handoff.md`` from the
    whitelist, forward slashes, no leading slash, no drive letter, no
    backslash, no NUL, no empty/``.``/``..`` components, no hidden
    components, and no ``local`` directory in any position.
    """
    if not isinstance(rel, str) or not rel:
        raise ValidationError("portable path must be a non-empty string")
    if rel != rel.strip():
        raise ValidationError(
            f"portable path {rel!r} must not have leading/trailing whitespace"
        )
    if "\x00" in rel:
        raise ValidationError(f"portable path {rel!r} contains a NUL byte")
    if "\\" in rel:
        raise ValidationError(
            f"portable path {rel!r} contains a backslash; only forward "
            "slashes are permitted"
        )
    if rel.startswith("/"):
        raise ValidationError(
            f"portable path {rel!r} is absolute; archives must be relative "
            "to .aperio/"
        )
    if re.match(r"^[A-Za-z]:", rel):
        raise ValidationError(
            f"portable path {rel!r} declares a drive letter; archives must "
            "be relative to .aperio/"
        )
    if rel.endswith("/") or "//" in rel:
        raise ValidationError(
            f"portable path {rel!r} is not canonical (empty/trailing "
            "path component)"
        )
    components = rel.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise ValidationError(
                f"portable path {rel!r} contains a traversal or non-canonical "
                f"component ({component!r})"
            )
        if component.startswith("."):
            raise ValidationError(
                f"portable path {rel!r} contains a hidden component "
                f"({component!r}); hidden and temporary files are never "
                "portable context"
            )
        if component.casefold() in _RESERVED_DIR_NAMES:
            raise ValidationError(
                f"portable path {rel!r} refers to {component!r}, which is "
                "machine-local data and must never travel in an archive"
            )
    if not _is_exportable(rel):
        raise ValidationError(
            f"portable path {rel!r} is not an approved shareable context "
            "file; permitted paths are: " + ", ".join(TOP_LEVEL_FILES)
            + ", runs/RUN-NNNN.handoff.md"
        )
    return rel


# --- export ------------------------------------------------------------------


def _read_export_file(aperio_dir: Path, rel: str) -> bytes:
    """Read one whitelisted file, refusing links and oversized content."""
    path = aperio_dir / rel
    if os.path.islink(path):
        raise PathError(
            f"refusing to export symlink at {path}: it could point at "
            "machine-local or private data; remove it and re-run 'aperio "
            "init' to restore the file"
        )
    if not path.is_file():
        raise StateError(f"cannot export {rel}: it is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StateError(
            f"cannot read {rel} for export: {exc.strerror or exc}"
        ) from None
    if size > MAX_ENTRY_BYTES:
        raise StateError(
            f"cannot export {rel}: {size} bytes exceeds the per-file limit "
            f"of {MAX_ENTRY_BYTES} bytes; portable context is text and "
            "should not be this large"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StateError(
            f"cannot read {rel} for export: {exc.strerror or exc}"
        ) from None
    if len(data) > MAX_ENTRY_BYTES:
        raise StateError(
            f"cannot export {rel}: it grew past the per-file limit while "
            "being read"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise StateError(
            f"cannot export {rel}: not valid UTF-8. Portable context is "
            "UTF-8 text only; inspect the file manually."
        ) from None
    return data


def _decode_json(data: bytes, rel: str) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(
            f"cannot export {rel}: invalid JSON ({exc}); repair the file "
            "first ('aperio integrity' reports the details)"
        ) from None
    return parsed


def _portable_execution_projection(document: dict) -> tuple[dict, list[str]]:
    """Strip machine-specific fields from execution records for travel."""
    warnings: list[str] = []
    runs: list[dict] = []
    dropped_pid = dropped_machine = dropped_log = 0
    for run in document["runs"]:
        portable = dict(run)
        if portable.get("pid") is not None:
            dropped_pid += 1
        if portable.get("machine") is not None:
            dropped_machine += 1
        if portable.get("local_log_ref"):
            dropped_log += 1
        portable["pid"] = None
        portable["machine"] = None
        portable["local_log_ref"] = None
        runs.append(portable)
    if dropped_pid or dropped_machine:
        warnings.append(
            "machine-specific run metadata (pid/machine) is not exported"
        )
    if dropped_log:
        warnings.append(
            f"{dropped_log} local log reference(s) dropped: .aperio/local/ "
            "never travels"
        )
    return {"schema_version": document["schema_version"], "runs": runs}, warnings


def _build_manifest(files: dict[str, bytes], schemas: dict) -> dict:
    entries = []
    for rel in sorted(files):
        data = files[rel]
        entries.append(
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "aperio_version": __version__,
        "created_at": utc_now_iso(),
        "schemas": schemas,
        "files": entries,
        "excluded": list(EXCLUDED_PATHS),
        "notes": _EXCLUDED_NOTE,
    }


def _write_archive(output: Path, manifest: dict, files: dict[str, bytes]) -> None:
    """Write the archive non-destructively (the target must not exist)."""
    if os.path.islink(output):
        raise PathError(
            f"refusing to write the export archive through symlink {output}"
        )
    if output.exists():
        raise StateError(
            f"refusing to overwrite existing archive {output}; choose a new "
            "--output path (exports are never destructive)"
        )
    parent = output.parent
    if not parent.is_dir():
        raise PathError(
            f"cannot write the export archive: directory {parent} does not exist"
        )
    tmp = output.with_name(output.name + ".tmp")
    if os.path.islink(tmp):
        raise PathError(f"refusing to follow symlink at {tmp}")
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
    except OSError as exc:
        raise StateError(
            f"cannot create the export archive at {output}: "
            f"{exc.strerror or exc}. The temporary name {tmp} may be "
            "occupied; it is never deleted automatically."
        ) from None
    try:
        with os.fdopen(fd, "wb") as raw:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(raw.fileno(), 0o644)
                except OSError:
                    pass
            _write_zip(raw, manifest, files)
    except OSError as exc:
        _discard(tmp, created_tmp)
        raise StateError(
            f"cannot write the export archive at {output}: {exc.strerror or exc}"
        ) from None
    try:
        os.link(tmp, output)  # atomic create-if-not-exists
    except FileExistsError:
        _discard(tmp, created_tmp)
        raise StateError(
            f"refusing to overwrite an archive that appeared at {output} "
            "while the export was running; it was left untouched"
        ) from None
    except OSError as exc:
        _discard(tmp, created_tmp)
        raise StateError(
            f"cannot publish the export archive at {output}: "
            f"{exc.strerror or exc}; no existing file was modified"
        ) from None
    _discard(tmp, created_tmp)


def _discard(path: Path, created: bool) -> None:
    if not created:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _write_zip(raw, manifest: dict, files: dict[str, bytes]) -> None:
    """Deterministic entry order, fixed timestamps, validated names only."""
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _zip_write(zf, MANIFEST_NAME, manifest_bytes)
        for rel in sorted(files):
            _zip_write(zf, f"{MEMBER_DIR}/{rel}", files[rel])


def _zip_write(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    zf.writestr(info, data)


def export_project(
    project_path: str, *, output: str | Path | None = None, dry_run: bool = False
) -> dict:
    """Export shareable project context to a portable archive.

    Returns a machine-readable report. With ``dry_run`` everything is
    validated and reported but no archive is written.
    """
    target = resolve_target(str(project_path), for_writing=False)
    aperio_dir = aperio_dir_for(target)
    # Hold every cooperative writer lock so plan/executions/handoff
    # cannot be read across a concurrent mutation.
    with coordination_locks(aperio_dir, include_import=False):
        state = require_supported_state(aperio_dir)
        warnings: list[str] = []
        files: dict[str, bytes] = {}

        files["state.json"] = _read_export_file(aperio_dir, "state.json")

        schemas: dict = {"state": state.schema_version}
        for rel in ("brief.md", "decisions.md"):
            if os.path.lexists(aperio_dir / rel):
                files[rel] = _read_export_file(aperio_dir, rel)

        if os.path.lexists(decisions_path(aperio_dir)):
            parsed = parse_decisions_text(files["decisions.md"].decode("utf-8"))
            if parsed.malformed_entries:
                warnings.append(
                    f"{len(parsed.malformed_entries)} malformed decision entr(y/ies) "
                    "exported verbatim (aperio never rewrites history)"
                )
            if parsed.duplicate_ids:
                warnings.append(
                    "duplicate decision ids exported verbatim: "
                    + ", ".join(parsed.duplicate_ids)
                )

        if os.path.lexists(handoff_path(aperio_dir)):
            files["handoff.md"] = _read_export_file(aperio_dir, "handoff.md")
            handoff = parse_handoff_text(files["handoff.md"].decode("utf-8"))
            if handoff.structured is None:
                warnings.append(
                    f"handoff.md uses newer format v{handoff.version}; exported "
                    "verbatim (this aperio cannot fully validate it)"
                )
            schemas["handoff"] = (
                "legacy" if handoff.structured is False
                else f"v{handoff.version}"
            )

        if os.path.lexists(plan_path(aperio_dir)):
            data = _read_export_file(aperio_dir, "plan.json")
            parsed = _decode_json(data, "plan.json")
            validate_plan_document(parsed, Path("archive:plan.json"))
            files["plan.json"] = data
            schemas["plan"] = parsed["schema_version"]

        document = read_executions(aperio_dir)
        if document is not None:
            # prepared/running runs have a live local process (or a crash
            # residue) that cannot travel; awaiting-external is exactly the
            # hand-off case this archive format exists for.
            active = [
                run["run_id"]
                for run in document["runs"]
                if run["status"] in ("prepared", "running")
            ]
            if active:
                warnings.append(
                    "execution metadata and run handoffs were not exported: "
                    f"active run(s) {', '.join(active)} cannot be handed off"
                )
            else:
                projection, projection_warnings = _portable_execution_projection(
                    document
                )
                warnings.extend(projection_warnings)
                files["executions.json"] = (
                    json.dumps(projection, indent=2, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                schemas["executions"] = projection["schema_version"]
                exported_handoffs = 0
                for run in document["runs"]:
                    ref = run.get("handoff_ref")
                    if not ref:
                        continue
                    if run["status"] != "awaiting-external":
                        warnings.append(
                            f"historical run handoff for {run['run_id']} not "
                            "exported (only awaiting-external handoffs travel)"
                        )
                        continue
                    text = read_run_handoff(aperio_dir, run["run_id"])
                    files[ref] = text.encode("utf-8")
                    exported_handoffs += 1
                if exported_handoffs:
                    warnings.append(
                        f"{exported_handoffs} awaiting-external run handoff(s) "
                        "included"
                    )

        if ".gitignore" not in files:
            warnings.append(
                ".gitignore is generated scaffolding and is not exported "
                "(import repairs it)"
            )

        for rel, label in (
            ("policies.json", "policy"),
            ("reviews.json", "reviews"),
            ("policy-audit.json", "audit"),
        ):
            if os.path.lexists(aperio_dir / rel):
                files[rel] = _read_export_file(aperio_dir, rel)
                parsed = _decode_json(files[rel], rel)
                version = parsed.get("schema_version")
                if isinstance(version, bool) or not isinstance(version, int):
                    raise StateError(
                        f"cannot export: {rel} has no integer schema_version; "
                        "run 'aperio integrity' and repair it first"
                    )
                schemas[label] = version
                supported = {
                    "policy": POLICY_SCHEMA_VERSION,
                    "reviews": REVIEWS_SCHEMA_VERSION,
                    "audit": POLICY_SCHEMA_VERSION,
                }[label]
                if version > supported:
                    warnings.append(
                        f"{rel} uses schema version {version} (newer than "
                        "this aperio); exported verbatim without interpretation"
                    )
                elif label == "policy":
                    validate_policy_document(parsed, Path("archive:" + rel))
                elif label == "reviews":
                    validate_reviews_document(parsed, Path("archive:" + rel))
                elif not isinstance(parsed.get("events"), list):
                    raise StateError(
                        f"cannot export: {rel} is corrupt ('events' must be "
                        "a list); run 'aperio integrity' first"
                    )

        total = sum(len(data) for data in files.values())
        if len(files) > MAX_ENTRIES:
            raise StateError(
                f"cannot export: {len(files)} files exceeds the limit of "
                f"{MAX_ENTRIES}"
            )
        if total > MAX_TOTAL_BYTES:
            raise StateError(
                f"cannot export: {total} bytes exceeds the total portable-context "
                f"limit of {MAX_TOTAL_BYTES} bytes"
            )


        _validate_export_relationships(files)

    manifest = _build_manifest(files, schemas)
    if output is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive_path = Path.cwd() / f"aperio-{state.name}-{stamp}.zip"
    else:
        archive_path = Path(os.path.abspath(os.path.expanduser(str(output))))
    report = {
        "command": "export",
        "dry_run": dry_run,
        "archive": None if dry_run else str(archive_path),
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "project": {"name": state.name, "schema_version": state.schema_version},
        "schemas": schemas,
        "files": manifest["files"],
        "total_bytes": total,
        "excluded": list(EXCLUDED_PATHS),
        "warnings": warnings,
    }
    if dry_run:
        return report

    _write_archive(archive_path, manifest, files)
    return report


# --- reading and validating an archive ---------------------------------------


@dataclass
class PortableArchive:
    """A fully validated archive held in memory (no filesystem effects)."""

    manifest: dict
    files: dict[str, bytes]
    schemas: dict
    warnings: list[str]


def _manifest_error(message: str) -> StateError:
    return StateError(f"invalid portable archive: {message}")


def _read_manifest(raw: bytes) -> dict:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _manifest_error(f"manifest is not valid UTF-8 JSON ({exc})") from None
    if not isinstance(data, dict):
        raise _manifest_error("manifest must be a JSON object")
    if data.get("format") != EXPORT_FORMAT:
        raise _manifest_error(
            f"unsupported format {data.get('format')!r}; expected "
            f"{EXPORT_FORMAT!r}"
        )
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _manifest_error("'format_version' must be an integer")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise _manifest_error(
            f"format version {version} is not supported by this aperio "
            f"(supports: {', '.join(map(str, SUPPORTED_FORMAT_VERSIONS))}); "
            "upgrade aperio to import it"
        )
    if not isinstance(data.get("aperio_version"), str) or not data["aperio_version"]:
        raise _manifest_error("'aperio_version' must be a non-empty string")
    schemas = data.get("schemas")
    if not isinstance(schemas, dict):
        raise _manifest_error("'schemas' must be an object")
    for key in ("state",):
        if not isinstance(schemas.get(key), int) or isinstance(
            schemas.get(key), bool
        ):
            raise _manifest_error(f"'schemas.{key}' must be an integer")
    entries = data.get("files")
    if not isinstance(entries, list) or not entries:
        raise _manifest_error("'files' must be a non-empty list")
    if len(entries) > MAX_ENTRIES:
        raise _manifest_error(
            f"manifest lists {len(entries)} files; the limit is {MAX_ENTRIES}"
        )
    seen: set[str] = set()
    seen_folded: set[str] = set()
    total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _manifest_error(f"files[{index}] must be an object")
        path = validate_rel_path(entry.get("path"))
        size = entry.get("size")
        digest = entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _manifest_error(f"files[{index}].size must be a non-negative integer")
        if size > MAX_ENTRY_BYTES:
            raise _manifest_error(
                f"{path!r} is {size} bytes, over the per-file limit of "
                f"{MAX_ENTRY_BYTES}"
            )
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise _manifest_error(
                f"files[{index}].sha256 must be 64 lowercase hex characters"
            )
        folded = path.casefold()
        if path in seen or folded in seen_folded:
            raise _manifest_error(f"duplicate path {path!r} in the manifest")
        seen.add(path)
        seen_folded.add(folded)
        total += size
    if total > MAX_TOTAL_BYTES:
        raise _manifest_error(
            f"archive declares {total} bytes, over the total limit of "
            f"{MAX_TOTAL_BYTES}"
        )
    if "state.json" not in seen:
        raise _manifest_error(
            "the archive does not contain state.json; a portable project "
            "must include it"
        )
    return data


def _zip_member_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def read_portable_archive(archive_path: str | Path) -> PortableArchive:
    """Read and fully validate an archive without touching the destination.

    Validation includes: archive container sanity (regular file, bounded
    size), member names (canonical whitelist only), member types (no
    directories, symlinks, or encrypted entries), manifest schema, declared
    sizes and SHA-256 hashes, UTF-8 decodability, and the content-level
    relationships described in :func:`_validate_payload`.
    """
    path = Path(os.path.abspath(os.path.expanduser(str(archive_path))))
    if os.path.islink(path):
        raise PathError(f"refusing to read an archive through symlink {path}")
    if not path.is_file():
        raise StateError(f"no archive file at {path}")
    try:
        archive_size = path.stat().st_size
    except OSError as exc:
        raise StateError(
            f"cannot read archive {path}: {exc.strerror or exc}"
        ) from None
    if archive_size > MAX_ARCHIVE_FILE_BYTES:
        raise _manifest_error(
            f"archive is {archive_size} bytes, over the limit of "
            f"{MAX_ARCHIVE_FILE_BYTES}"
        )
    try:
        with zipfile.ZipFile(path) as zf:
            return _read_zip(zf)
    except zipfile.BadZipFile:
        raise _manifest_error("not a ZIP archive (corrupt or truncated file)") from None
    except OSError as exc:
        raise StateError(f"cannot read archive {path}: {exc.strerror or exc}") from None


def _read_zip(zf: zipfile.ZipFile) -> PortableArchive:
    infos = zf.infolist()
    if not infos:
        raise _manifest_error("the archive is empty")
    if len(infos) > MAX_ENTRIES + 1:
        raise _manifest_error(
            f"archive has {len(infos)} entries; the limit is {MAX_ENTRIES + 1}"
        )
    names: set[str] = set()
    folded: set[str] = set()
    for info in infos:
        name = info.filename
        if name in names or name.casefold() in folded:
            raise _manifest_error(f"duplicate archive member {name!r}")
        names.add(name)
        folded.add(name.casefold())
        if info.is_dir():
            raise _manifest_error(
                f"archive member {name!r} is a directory entry; portable "
                "archives contain regular files only"
            )
        mode = _zip_member_mode(info)
        if stat.S_ISLNK(mode):
            raise _manifest_error(
                f"archive member {name!r} is a symlink; links are never "
                "imported"
            )
        if info.flag_bits & 0x1:
            raise _manifest_error(
                f"archive member {name!r} is encrypted; encrypted archives "
                "are not supported"
            )
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _manifest_error(
                f"archive member {name!r} uses unsupported compression"
            )
        if info.file_size > MAX_ENTRY_BYTES and name != MANIFEST_NAME:
            raise _manifest_error(
                f"archive member {name!r} is larger than the per-file limit"
            )
    if MANIFEST_NAME not in names:
        raise _manifest_error(f"no {MANIFEST_NAME} at the archive root")

    manifest_raw = _read_member(
        zf, zf.getinfo(MANIFEST_NAME), MAX_MANIFEST_BYTES
    )
    manifest = _read_manifest(manifest_raw)
    declared = {entry["path"]: entry for entry in manifest["files"]}

    payload_names = {
        name for name in names if name != MANIFEST_NAME
    }
    expected_names = {f"{MEMBER_DIR}/{rel}" for rel in declared}
    unexpected = payload_names - expected_names
    if unexpected:
        raise _manifest_error(
            "archive contains entries not listed in the manifest: "
            + ", ".join(sorted(unexpected)[:5])
        )
    missing = expected_names - payload_names
    if missing:
        raise _manifest_error(
            "manifest lists files absent from the archive: "
            + ", ".join(sorted(missing)[:5])
        )

    files: dict[str, bytes] = {}
    total = 0
    for rel in sorted(declared):
        entry = declared[rel]
        data = _read_member(
            zf, zf.getinfo(f"{MEMBER_DIR}/{rel}"), MAX_ENTRY_BYTES
        )
        if len(data) != entry["size"]:
            raise _manifest_error(
                f"{rel!r} is {len(data)} bytes but the manifest declares "
                f"{entry['size']}; the archive is corrupt or was tampered with"
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            raise _manifest_error(
                f"{rel!r} does not match its manifest SHA-256; the archive "
                "is corrupt or was tampered with"
            )
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise _manifest_error(f"{rel!r} is not valid UTF-8 text") from None
        files[rel] = data
        total += len(data)
    if total > MAX_TOTAL_BYTES:
        raise _manifest_error("archive content exceeds the total size limit")

    archive = PortableArchive(
        manifest=manifest, files=files, schemas={}, warnings=[]
    )
    _validate_payload(archive)
    return archive


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Decompress one member with a hard bound (zip-bomb protection)."""
    try:
        with zf.open(info) as fh:
            data = fh.read(limit + 1)
    except zipfile.BadZipFile:
        raise _manifest_error(
            f"archive member {info.filename!r} is corrupt (CRC/deflate error)"
        ) from None
    if len(data) > limit:
        raise _manifest_error(
            f"archive member {info.filename!r} expands beyond the allowed "
            f"{limit} bytes"
        )
    if info.file_size > limit:
        raise _manifest_error(
            f"archive member {info.filename!r} declares more than the "
            f"allowed {limit} bytes"
        )
    return data


def _validate_payload(archive: PortableArchive) -> None:
    """Content-level validation; no filesystem effects."""
    files = archive.files
    manifest = archive.manifest
    schemas = manifest["schemas"]
    warnings: list[str] = []

    state = parse_state_bytes(files["state.json"], Path("archive:state.json"))
    if state.schema_version != schemas.get("state"):
        raise _manifest_error(
            "manifest schemas.state does not match state.json "
            f"({schemas.get('state')} vs {state.schema_version}); the "
            "archive was tampered with"
        )
    archive.schemas = {"state": state.schema_version}

    plan = None
    if "plan.json" in files:
        plan = _decode_json(files["plan.json"], "archive:plan.json")
        plan_version = plan.get("schema_version")
        if isinstance(plan_version, bool) or not isinstance(plan_version, int):
            raise _manifest_error("plan.json schema_version must be an integer")
        if plan_version > PLAN_SCHEMA_VERSION:
            raise StateError(
                f"plan.json uses schema version {plan_version}, newer than "
                f"supported version {PLAN_SCHEMA_VERSION}; this aperio will "
                "not convert it. Upgrade aperio before importing."
            )
        if plan_version != PLAN_SCHEMA_VERSION:
            raise StateError(
                f"plan.json uses unsupported schema version {plan_version}; "
                "no migration is implemented for it"
            )
        if schemas.get("plan") != plan_version:
            raise _manifest_error("manifest schemas.plan does not match plan.json")
        validate_plan_document(plan, Path("archive:plan.json"))
        archive.schemas["plan"] = plan_version
    elif "plan" in schemas:
        raise _manifest_error("manifest declares a plan schema but plan.json is absent")

    runs = None
    if "executions.json" in files:
        runs = _decode_json(files["executions.json"], "archive:executions.json")
        exec_version = runs.get("schema_version")
        if isinstance(exec_version, bool) or not isinstance(exec_version, int):
            raise _manifest_error("executions.json schema_version must be an integer")
        if exec_version > EXECUTIONS_SCHEMA_VERSION:
            raise StateError(
                f"executions.json uses schema version {exec_version}, newer "
                f"than supported version {EXECUTIONS_SCHEMA_VERSION}; this "
                "aperio will not convert it. Upgrade aperio before importing."
            )
        if exec_version != EXECUTIONS_SCHEMA_VERSION:
            raise StateError(
                f"executions.json uses unsupported schema version "
                f"{exec_version}; no migration is implemented for it"
            )
        if schemas.get("executions") != exec_version:
            raise _manifest_error(
                "manifest schemas.executions does not match executions.json"
            )
        validate_executions_document(runs, Path("archive:executions.json"))
        archive.schemas["executions"] = exec_version
    elif "executions" in schemas:
        raise _manifest_error(
            "manifest declares an executions schema but executions.json is absent"
        )

    if "decisions.md" in files:
        parsed = parse_decisions_text(files["decisions.md"].decode("utf-8"))
        if parsed.malformed_entries:
            warnings.append(
                f"{len(parsed.malformed_entries)} malformed decision "
                "entr(y/ies) preserved verbatim"
            )
        if parsed.duplicate_ids:
            warnings.append(
                "duplicate decision ids present: "
                + ", ".join(parsed.duplicate_ids)
            )

    if "handoff.md" in files:
        handoff = parse_handoff_text(files["handoff.md"].decode("utf-8"))
        if handoff.structured is None:
            warnings.append(
                f"handoff.md uses newer format v{handoff.version}; preserved "
                "verbatim (not converted)"
            )
        archive.schemas["handoff"] = (
            "legacy" if handoff.structured is False else f"v{handoff.version}"
        )
        if schemas.get("handoff") not in (None, archive.schemas["handoff"]):
            raise _manifest_error("manifest schemas.handoff does not match handoff.md")

    if runs is not None:
        tasks = {
            task["id"]: task for task in (plan["tasks"] if plan else [])
        }
        if plan is None and runs["runs"]:
            raise StateError(
                "the archive contains execution records but no plan.json; "
                "importing it would break the task/run relationship. Export "
                "the project with its plan, or remove executions.json."
            )
        seen_runs: set[str] = set()
        for run in runs["runs"]:
            run_id = run["run_id"]
            if run_id in seen_runs:
                raise _manifest_error(f"duplicate run id {run_id}")
            seen_runs.add(run_id)
            if plan is not None and run["task_id"] not in tasks:
                raise _manifest_error(
                    f"run {run_id} references unknown task {run['task_id']}"
                )
            if run["status"] in ("prepared", "running"):
                raise StateError(
                    f"run {run_id} is {run['status']!r}: an active local "
                    "execution record belongs to the exporting machine and "
                    "cannot be imported. Finish, cancel, or reconcile the run "
                    "before exporting. Nothing was modified."
                )
            if run.get("pid") is not None or run.get("machine") is not None:
                raise StateError(
                    f"run {run_id} carries machine-local process identity "
                    "(pid/machine). Portable archives must not contain it: "
                    "trusting an imported pid could signal an unrelated "
                    "process on this machine. Re-export with a current aperio "
                    "(which nulls those fields). Nothing was modified."
                )
            local_ref = run.get("local_log_ref")
            if local_ref is not None:
                raise StateError(
                    f"run {run_id} references a machine-local log "
                    f"({local_ref!r}). Local logs never travel, so an imported "
                    "record must not claim one. Re-export with a current "
                    "aperio. Nothing was modified."
                )
            ref = run.get("handoff_ref")
            if run["status"] == "awaiting-external":
                if not ref:
                    raise _manifest_error(
                        f"awaiting-external run {run_id} has no handoff_ref"
                    )
                if ref not in files:
                    raise _manifest_error(
                        f"run {run_id} references handoff {ref!r}, which is "
                        "not in the archive"
                    )
                if ref != f"runs/{run_id}.handoff.md":
                    raise _manifest_error(
                        f"run {run_id} references handoff {ref!r}; the "
                        "reference does not match the run"
                    )
            elif ref is not None and ref not in files:
                warnings.append(
                    f"run {run_id} references handoff {ref!r}; the file is "
                    "not included (historical handoff, not imported)"
                )
        if plan is not None:
            for run in runs["runs"]:
                if run["plan_version"] > plan["plan_version"]:
                    raise _manifest_error(
                        f"run {run['run_id']} was recorded against plan "
                        f"revision {run['plan_version']}, newer than the "
                        f"included plan revision {plan['plan_version']}"
                    )

    if "policies.json" in files:
        policy = _decode_json(files["policies.json"], "archive:policies.json")
        policy_version = policy.get("schema_version")
        if isinstance(policy_version, bool) or not isinstance(policy_version, int):
            raise _manifest_error("policies.json schema_version must be an integer")
        if policy_version > POLICY_SCHEMA_VERSION:
            raise StateError(
                f"policies.json uses policy schema version {policy_version}, "
                f"newer than supported version {POLICY_SCHEMA_VERSION}; this "
                "aperio will not interpret it. Upgrade aperio before importing."
            )
        if policy_version != POLICY_SCHEMA_VERSION:
            raise StateError(
                f"policies.json uses unsupported policy schema version "
                f"{policy_version}; no migration is implemented for it"
            )
        if schemas.get("policy") != policy_version:
            raise _manifest_error("manifest schemas.policy does not match policies.json")
        validate_policy_document(policy, Path("archive:policies.json"))
        archive.schemas["policy"] = policy_version
    elif "policy" in schemas:
        raise _manifest_error(
            "manifest declares a policy schema but policies.json is absent"
        )

    if "reviews.json" in files:
        reviews = _decode_json(files["reviews.json"], "archive:reviews.json")
        review_version = reviews.get("schema_version")
        if isinstance(review_version, bool) or not isinstance(review_version, int):
            raise _manifest_error("reviews.json schema_version must be an integer")
        if review_version > REVIEWS_SCHEMA_VERSION:
            raise StateError(
                f"reviews.json uses review schema version {review_version}, "
                f"newer than supported version {REVIEWS_SCHEMA_VERSION}; this "
                "aperio will not interpret it. Upgrade aperio before importing."
            )
        if review_version != REVIEWS_SCHEMA_VERSION:
            raise StateError(
                f"reviews.json uses unsupported review schema version "
                f"{review_version}; no migration is implemented for it"
            )
        if schemas.get("reviews") != review_version:
            raise _manifest_error("manifest schemas.reviews does not match reviews.json")
        validate_reviews_document(reviews, Path("archive:reviews.json"))
        archive.schemas["reviews"] = review_version
        if reviews["reviews"] and "policies.json" not in files:
            raise StateError(
                "the archive contains review history but no policies.json; "
                "governance records must travel with their policy. Importing "
                "would leave unattributed approval history. Nothing was "
                "modified."
            )
    elif "reviews" in schemas:
        raise _manifest_error(
            "manifest declares a reviews schema but reviews.json is absent"
        )

    if "policy-audit.json" in files:
        audit = _decode_json(files["policy-audit.json"], "archive:policy-audit.json")
        audit_version = audit.get("schema_version")
        if audit_version != POLICY_SCHEMA_VERSION:
            raise StateError(
                f"policy-audit.json uses unsupported schema version "
                f"{audit_version!r}; this aperio will not interpret it"
            )
        if not isinstance(audit.get("events"), list):
            raise _manifest_error("policy-audit.json 'events' must be a list")
        archive.schemas["audit"] = audit_version
    elif "audit" in schemas:
        raise _manifest_error(
            "manifest declares an audit schema but policy-audit.json is absent"
        )

    archive.warnings.extend(warnings)


# --- import ------------------------------------------------------------------


def _target_path(aperio_dir: Path, rel: str) -> Path:
    """Destination path for one validated relative path.

    The parent chain is validated for ``runs/`` (the only nested portable
    directory) so a symlinked or non-directory occupant can never redirect
    an import outside the project.
    """
    if "/" in rel:
        parent = check_artifact_dir(aperio_dir, "runs")
        name = rel.split("/", 1)[1]
        return parent / name
    return aperio_dir / rel


def _reject_destination_leaf(path: Path, rel: str) -> None:
    if os.path.islink(path):
        raise PathError(
            f"refusing to import {rel!r} through symlink at {path}; remove "
            "the link and retry"
        )
    if path.exists() and not path.is_file():
        raise PathError(
            f"cannot import {rel!r}: a non-file occupant exists at {path}"
        )
    if path.exists():
        try:
            links = path.stat().st_nlink
        except OSError:
            links = 1
        if links > 1:
            raise PathError(
                f"refusing to import over {path}: it has {links} hard links, "
                "so replacing it could modify a file outside .aperio/. Copy "
                "the content elsewhere and remove the link first."
            )


def _create_new_bytes(path: Path, data: bytes, *, rel: str) -> None:
    """Create one new imported file with exact bytes (no newline translation)."""
    handle = open_exclusive_binary(path, what=f"imported {rel}")
    try:
        handle.write(data)
    except OSError as exc:
        discard_created_file(path, handle)
        raise StateError(
            f"cannot write imported {rel} at {path}: {exc.strerror or exc}; "
            "the partial file created by this command was removed"
        ) from None
    handle.close()


def _atomic_write_bytes(path: Path, data: bytes, *, what: str) -> None:
    """Replace (or create) one file atomically through an exclusive temp."""
    tmp = path.with_name(path.name + ".import.tmp")
    if os.path.islink(tmp):
        raise PathError(f"refusing to follow symlink at {tmp}")
    created_tmp = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created_tmp = True
    except OSError as exc:
        raise StateError(
            f"cannot prepare the import of {what}: {exc.strerror or exc}. "
            f"The temporary name {tmp} may be occupied; it is never deleted "
            "automatically."
        ) from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        _discard(tmp, created_tmp)
        raise StateError(
            f"cannot write {what} at {path}: {exc.strerror or exc}; "
            "existing content was left untouched"
        ) from None


def _inspect_existing(
    aperio_dir: Path, files: dict[str, bytes], *, replace: bool
) -> tuple[list[str], list[str], list[str]]:
    """Classify destination paths: (create, replace, unchanged) + conflict.

    Returns the three action lists; raises when a differing file exists and
    ``replace`` was not requested (the caller catches and reports).
    """
    create: list[str] = []
    replace_list: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for rel in sorted(files):
        path = _planned_path(aperio_dir, rel)
        _reject_destination_leaf(path, rel)
        if not path.exists():
            create.append(rel)
            continue
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise StateError(
                f"cannot read existing {path} before importing: "
                f"{exc.strerror or exc}"
            ) from None
        if current == files[rel]:
            unchanged.append(rel)
        elif replace:
            replace_list.append(rel)
        else:
            conflicts.append(rel)
    if conflicts:
        raise StateError(
            "import would overwrite existing project context: "
            + ", ".join(conflicts)
            + ". Nothing was modified. Re-run with --replace after reviewing "
            "the archive, or import into an empty project directory."
        )
    return create, replace_list, unchanged


def _write_import(
    aperio_dir: Path,
    files: dict[str, bytes],
    create: list[str],
    replace: list[str],
) -> None:
    """Write imported files with in-process rollback on failure.

    ``state.json`` is written last: a crash mid-import never leaves a
    project that claims to be imported while its context is partial.
    """
    created: list[Path] = []
    replaced: list[tuple[Path, bytes]] = []
    ordered_create = sorted(create, key=lambda rel: (rel == "state.json", rel))
    ordered_replace = sorted(replace, key=lambda rel: (rel == "state.json", rel))
    try:
        for rel in ordered_create:
            path = _target_path(aperio_dir, rel)
            _reject_destination_leaf(path, rel)
            _create_new_bytes(path, files[rel], rel=rel)
            created.append(path)
        for rel in ordered_replace:
            path = _target_path(aperio_dir, rel)
            _reject_destination_leaf(path, rel)
            try:
                old = path.read_bytes()
            except OSError as exc:
                raise StateError(
                    f"cannot read existing {path} before replacing it: "
                    f"{exc.strerror or exc}"
                ) from None
            _atomic_write_bytes(
                path, files[rel], what=f"imported {rel}"
            )
            replaced.append((path, old))
    except BaseException:
        _rollback(created, replaced)
        raise


def _rollback(created: list[Path], replaced: list[tuple[Path, bytes]]) -> None:
    """Best-effort undo of this call's writes (never touches other files)."""
    failures: list[str] = []
    for path, old in reversed(replaced):
        try:
            _atomic_write_bytes(path, old, what="restored " + path.name)
        except (AperioError, OSError) as exc:  # pragma: no cover - rare
            failures.append(f"{path}: {exc}")
    for path in reversed(created):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - rare
            failures.append(f"{path}: {exc}")
    if failures:  # pragma: no cover - surfaced loudly if it ever happens
        raise StateError(
            "import failed and the automatic rollback was incomplete; "
            "inspect these files before retrying: " + "; ".join(failures)
        )


def _planned_path(aperio_dir: Path, rel: str) -> Path:
    """Destination path without creating anything (used by previews)."""
    if "/" not in rel:
        return aperio_dir / rel
    parent = aperio_dir / "runs"
    if os.path.islink(parent):
        raise PathError(
            f"refusing to follow symlink at {parent}: run handoffs must stay "
            "inside the project; remove it and retry"
        )
    if parent.exists() and not parent.is_dir():
        raise PathError(f"cannot import run handoffs: {parent} is not a directory")
    return parent / rel.split("/", 1)[1]


def _classify_destination(aperio_dir: Path, files: dict[str, bytes]) -> str:
    """Return ``fresh``, ``initialized``, or ``partial`` (recoverable).

    ``partial`` means a previous import was interrupted before writing
    state.json: every portable file present matches the archive exactly,
    so finishing the import is safe. Anything else is refused — aperio
    never guesses about content it did not write.
    """
    if not aperio_dir.exists():
        return "fresh"
    if os.path.islink(aperio_dir):
        raise PathError(
            f"refusing to follow symlink at {aperio_dir}; remove it and retry"
        )
    if not aperio_dir.is_dir():
        raise PathError(f"cannot import into {aperio_dir}: it is not a directory")
    if os.path.lexists(aperio_dir / "state.json"):
        return "initialized"
    for entry in sorted(aperio_dir.iterdir()):
        name = entry.name
        if name == "local":
            if not entry.is_dir() or os.path.islink(entry):
                raise _manifest_error(
                    f"{entry} is not a safe local directory; remove it and retry"
                )
            continue
        if name == "runs":
            if os.path.islink(entry) or not entry.is_dir():
                raise _manifest_error(f"{entry} is not a safe runs directory")
            for child in sorted(entry.iterdir()):
                rel = f"runs/{child.name}"
                if rel not in files or os.path.islink(child) or not child.is_file():
                    raise _manifest_error(
                        f"incomplete import: unexpected entry {child}; "
                        "inspect or remove the partial .aperio/ directory"
                    )
                if child.read_bytes() != files[rel]:
                    raise _manifest_error(
                        f"incomplete import: {child} does not match the "
                        "archive; inspect or remove the partial .aperio/ "
                        "directory"
                    )
            continue
        if name not in TOP_LEVEL_FILES or name not in files:
            raise _manifest_error(
                f"incomplete import: unexpected entry {entry}; inspect or "
                "remove the partial .aperio/ directory"
            )
        if os.path.islink(entry) or not entry.is_file():
            raise _manifest_error(f"incomplete import: {entry} is not a file")
        if entry.read_bytes() != files[name]:
            raise _manifest_error(
                f"incomplete import: {entry} does not match the archive; "
                "inspect or remove the partial .aperio/ directory"
            )
    return "partial"


def import_archive(
    project_path: str,
    archive_path: str | Path,
    *,
    replace: bool = False,
    dry_run: bool = False,
) -> dict:
    """Validate and import a portable archive into a destination project.

    The whole archive is validated before any filesystem effect, including
    the merged result of the archive plus the destination files that remain.
    Execution records with machine-local identity (pid/machine/local log) or
    an active status are refused. Existing files are never overwritten
    unless ``replace`` is requested; identical bytes are accepted as
    unchanged. The apply step holds every writer lock in the fixed global
    order, re-validates under lock, aborts if the destination changed while
    preparing, and rolls back its own writes on error.
    """
    archive = read_portable_archive(archive_path)
    target = resolve_target(str(project_path), for_writing=True)
    aperio_dir = aperio_dir_for(target)
    destination = _classify_destination(aperio_dir, archive.files)
    if destination == "initialized":
        require_supported_state(aperio_dir)

    create, replace_list, unchanged = _inspect_existing(
        aperio_dir, archive.files, replace=replace
    )
    archive_run_ids: set[str] = set()
    if "executions.json" in archive.files:
        archive_run_ids = {
            run["run_id"]
            for run in _decode_json(
                archive.files["executions.json"], "archive:executions.json"
            )["runs"]
        }
    _validate_merged_context(aperio_dir, archive.files, archive_run_ids)
    if os.path.lexists(aperio_dir / "state.json") and "state.json" in unchanged:
        warnings = list(archive.warnings) + [
            "state.json already matches the archive; the destination keeps "
            "its current identity"
        ]
    else:
        warnings = list(archive.warnings)

    total = sum(len(data) for data in archive.files.values())
    report = {
        "command": "import",
        "format": archive.manifest["format"],
        "format_version": archive.manifest["format_version"],
        "dry_run": dry_run,
        "archive": str(Path(os.path.abspath(os.path.expanduser(str(archive_path))))),
        "destination": str(target),
        "destination_state": destination,
        "replace": replace,
        "created": sorted(create),
        "replaced": sorted(replace_list),
        "unchanged": sorted(unchanged),
        "schemas": archive.schemas,
        "total_bytes": total,
        "warnings": warnings,
        "notes": [
            "Validation completed before any write.",
            "state.json is written last; an interrupted import never leaves "
            "a project that claims to be imported.",
            "The apply step holds every Aperio writer lock in a fixed "
            "global order and aborts if the destination changed while the "
            "import was preparing.",
            "Multi-file imports are not a filesystem transaction; a crash "
            "can leave a partial context (diagnose with 'aperio integrity', "
            "finish by re-running the import).",
        ],
    }
    if dry_run:
        report["notes"].append(
            "Dry-run: the destination was not locked and may still change. "
            "The apply step re-validates under lock."
        )
        return report

    pre_snapshot = _destination_snapshot(aperio_dir)
    created_project_dir = False
    if not target.exists():
        try:
            target.mkdir(parents=True)
            created_project_dir = True
        except OSError as exc:
            raise StateError(
                f"cannot create destination directory {target}: "
                f"{exc.strerror or exc}"
            ) from None
    if os.path.islink(target):
        raise PathError(f"refusing to follow symlink at {target}")

    created_aperio_dir = False
    if not aperio_dir.exists():
        try:
            aperio_dir.mkdir()  # exclusive: never adopts an existing entry
            created_aperio_dir = True
        except FileExistsError:
            pass  # a concurrent import created it; the re-check decides
        except OSError as exc:
            raise StateError(
                f"cannot create {aperio_dir}: {exc.strerror or exc}"
            ) from None
        if os.path.islink(aperio_dir) or not aperio_dir.is_dir():
            raise PathError(
                f"{aperio_dir} is not a safe directory after creation; nothing "
                "was imported"
            )

    try:
        with coordination_locks(aperio_dir, include_import=True):
            # Re-check the destination now that every cooperative writer is
            # excluded: if it changed while this import was preparing, the
            # preview/validation the operator saw is stale.
            post_snapshot = _destination_snapshot(aperio_dir)
            changed = sorted(
                rel
                for rel in set(pre_snapshot) | set(post_snapshot)
                if pre_snapshot.get(rel) != post_snapshot.get(rel)
            )
            if changed:
                raise StateError(
                    "the destination changed while the import was preparing "
                    f"({', '.join(changed)}); nothing was modified. Re-run "
                    "the import to review the current state."
                )
            create, replace_list, unchanged = _inspect_existing(
                aperio_dir, archive.files, replace=replace
            )
            _validate_merged_context(
                aperio_dir, archive.files, archive_run_ids
            )
            _write_import(aperio_dir, archive.files, create, replace_list)
    except BaseException:
        if created_aperio_dir:
            try:
                aperio_dir.rmdir()  # only succeeds when truly empty
            except OSError:
                pass
        if created_project_dir:
            try:
                target.rmdir()
            except OSError:
                pass
        raise

    try:
        scaffolding = ensure_local_scaffolding(aperio_dir)
    except AperioError as exc:
        raise StateError(
            "the archive was imported, but the machine-local scaffolding "
            f"(local/, .gitignore) could not be repaired: {exc}. Fix the "
            "reported path, then run 'aperio init' on the destination to "
            "finish setup without overwriting imported context."
        ) from None

    report["created"] = sorted(create)
    report["replaced"] = sorted(replace_list)
    report["unchanged"] = sorted(unchanged)
    report["local_scaffolding"] = scaffolding
    return report
