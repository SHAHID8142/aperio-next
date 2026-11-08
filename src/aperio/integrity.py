"""Portable-context integrity checks (Feature 05).

``aperio integrity`` answers one question: *is this project's shareable
context internally consistent and safe to travel?* It is strictly
diagnostic: it never repairs, rewrites, deletes, or "fixes" anything, and
it never reads ``.aperio/local/`` content beyond noting that the directory
exists.

Checks performed:

- ``state.json``: exists, supported schema, valid fields.
- ``plan.json``: supported schema, full plan model/graph/lifecycle
  validation (duplicate ids, unknown dependencies, contradictory status,
  approval invariants).
- ``decisions.md``: parseable entries; malformed entries and duplicate ids
  are reported (aperio never rewrites history, so damage is visible, not
  fatal).
- ``handoff.md``: known format; a damaged structured v1 file is an error,
  a newer format is reported and preserved.
- ``executions.json``: supported schema, valid records, task references
  that exist in the plan, run handoff references that exist and match the
  run, and local-log references confined to ``.aperio/local/``.
- Missing expected shareable files and unexpected entries in the
  ``.aperio/`` root / ``runs/`` directory.
- Optionally, an archive passed with ``--archive`` is fully validated
  (manifest hashes, path whitelist, schemas, payload relationships).

Severity policy: corruption and unsupported schemas are errors (exit 1);
missing optional files, malformed history, and unexpected leftovers are
warnings. Every finding names a path and an actionable next step.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aperio.adapters import runs_dir
from aperio.decisions import decisions_path, read_decisions
from aperio.errors import AperioError
from aperio.handoff import handoff_path, read_handoff
from aperio.paths import resolve_target
from aperio.planmodel import PLAN_SCHEMA_VERSION, validate_plan_document
from aperio.policy import (
    PLAN_ATTRIBUTION_ACTIONS,
    POLICY_SCHEMA_VERSION,
    policy_path,
    validate_policy_document,
)
from aperio.reviews import (
    REVIEWS_SCHEMA_VERSION,
    reviews_path,
    validate_reviews_document,
)
from aperio.planstore import plan_path
from aperio.portable import (
    RUN_HANDOFF_PATTERN,
    TOP_LEVEL_FILES,
    read_portable_archive,
)
from aperio.project import APERIO_DIR_NAME, aperio_dir_for
from aperio.runtime import (
    EXECUTIONS_SCHEMA_VERSION,
    execution_summary,
    read_executions,
)
from aperio.schema import SCHEMA_VERSION, state_path

EXPECTED_FILES: tuple[str, ...] = (
    "state.json",
    "brief.md",
    "decisions.md",
    "handoff.md",
    ".gitignore",
)

_RESERVED_TEMP_NAMES = frozenset(
    {
        "state.json.tmp",
        "plan.json.tmp",
        "executions.json.tmp",
        "handoff.md.tmp",
        "policies.json.tmp",
        "reviews.json.tmp",
        "policy-audit.json.tmp",
    }
)
_RESERVED_LOCK_NAMES = frozenset(
    {
        "state.lock",
        "plan.lock",
        "executions.lock",
        "handoff.lock",
        "decisions.lock",
        "dispatch.lock",
        "import.lock",
        "reviews.lock",
        "policy.lock",
    }
)


def _finding(code: str, message: str, path: str | None = None) -> dict:
    finding = {"code": code, "message": message}
    if path is not None:
        finding["path"] = path
    return finding


def _schema_of(path: Path) -> int | None:
    """Best-effort schema version of a JSON file (None when unreadable)."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def check_integrity(
    project_path: str, *, archive_path: str | Path | None = None
) -> dict:
    """Run all portable-context checks and return a machine-readable report."""
    target = resolve_target(str(project_path), for_writing=False)
    aperio_dir = aperio_dir_for(target)
    errors: list[dict] = []
    warnings: list[dict] = []
    schemas: dict = {
        "state": None,
        "plan": None,
        "executions": None,
        "handoff": None,
        "policy": None,
        "reviews": None,
        "audit": None,
    }
    missing: list[str] = []
    unexpected: list[str] = []

    if os.path.islink(aperio_dir):
        errors.append(
            _finding(
                "symlink-context",
                f"{aperio_dir} is a symlink; portable context must live "
                "inside the project. Remove it and run 'aperio init'.",
                APERIO_DIR_NAME,
            )
        )
        return _finish(errors, warnings, schemas, missing, unexpected)

    if state_path(aperio_dir).is_symlink():
        errors.append(
            _finding(
                "symlink-file",
                "state.json is a symlink; remove it and run 'aperio init' to "
                "restore a regular file",
                "state.json",
            )
        )
        return _finish(errors, warnings, schemas, missing, unexpected)
    if not state_path(aperio_dir).exists():
        errors.append(
            _finding(
                "not-initialized",
                f"no {APERIO_DIR_NAME}/state.json; run 'aperio init' first",
                APERIO_DIR_NAME,
            )
        )
        return _finish(errors, warnings, schemas, missing, unexpected)

    # --- state ---------------------------------------------------------
    try:
        from aperio.project import require_supported_state

        state = require_supported_state(aperio_dir)
        schemas["state"] = state.schema_version
    except AperioError as exc:
        errors.append(_finding("state-invalid", str(exc), "state.json"))
        return _finish(errors, warnings, schemas, missing, unexpected)

    # --- top-level presence / unexpected entries -----------------------
    for rel in EXPECTED_FILES:
        path = aperio_dir / rel
        if not path.exists():
            missing.append(rel)
            warnings.append(
                _finding(
                    "missing-file",
                    f"{rel} is missing; run 'aperio init' to restore the "
                    "template without overwriting anything",
                    rel,
                )
            )
        elif path.is_symlink():
            errors.append(
                _finding(
                    "symlink-file",
                    f"{rel} is a symlink; remove it and run 'aperio init' to "
                    "restore a regular file",
                    rel,
                )
            )

    try:
        entries = sorted(aperio_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        errors.append(
            _finding("unreadable-context", f"cannot list {aperio_dir}: {exc}", APERIO_DIR_NAME)
        )
        entries = []
    known_dirs = {"local", "runs"}
    for entry in entries:
        name = entry.name
        if name in EXPECTED_FILES or name in TOP_LEVEL_FILES:
            continue
        if name in known_dirs:
            continue
        if name in _RESERVED_TEMP_NAMES:
            warnings.append(
                _finding(
                    "leftover-temp",
                    f"reserved temporary file {name} is present; it is a "
                    "leftover from an interrupted write and is never removed "
                    "automatically. Inspect it and remove it yourself.",
                    name,
                )
            )
            continue
        if name in _RESERVED_LOCK_NAMES:
            warnings.append(
                _finding(
                    "leftover-lock",
                    f"lock file {name} is present; if no aperio process is "
                    "running, remove it manually (locks are never stolen).",
                    name,
                )
            )
            continue
        unexpected.append(name)
        warnings.append(
            _finding(
                "unexpected-file",
                f"{name} is not part of Aperio's portable context; it is "
                "ignored by export and left untouched",
                name,
            )
        )

    local_dir = aperio_dir / "local"
    if local_dir.exists() and (os.path.islink(local_dir) or not local_dir.is_dir()):
        errors.append(
            _finding(
                "invalid-local-dir",
                ".aperio/local must be a real directory (it is never exported)",
                "local",
            )
        )

    # --- plan ----------------------------------------------------------
    plan = None
    plan_file = plan_path(aperio_dir)
    if plan_file.exists():
        if plan_file.is_symlink():
            errors.append(
                _finding("symlink-file", "plan.json is a symlink", "plan.json")
            )
        else:
            version = _schema_of(plan_file)
            schemas["plan"] = version
            try:
                data = json.loads(plan_file.read_bytes().decode("utf-8"))
                validate_plan_document(data, plan_file)
                plan = data
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    _finding("plan-unreadable", f"cannot read plan.json: {exc}", "plan.json")
                )
            except json.JSONDecodeError as exc:
                errors.append(
                    _finding(
                        "plan-corrupt",
                        f"plan.json is not valid JSON ({exc}); aperio will "
                        "not rewrite it. Restore it from version control.",
                        "plan.json",
                    )
                )
            except AperioError as exc:
                errors.append(_finding("plan-invalid", str(exc), "plan.json"))
            if version is not None and version > PLAN_SCHEMA_VERSION:
                warnings.append(
                    _finding(
                        "future-plan-schema",
                        f"plan.json uses schema v{version}, newer than supported "
                        f"v{PLAN_SCHEMA_VERSION}; it is preserved, not converted",
                        "plan.json",
                    )
                )
    else:
        missing.append("plan.json")

    # --- decisions -----------------------------------------------------
    decisions_file = decisions_path(aperio_dir)
    if decisions_file.exists() and not decisions_file.is_symlink():
        try:
            parsed = read_decisions(aperio_dir)
            if parsed.malformed_entries:
                warnings.append(
                    _finding(
                        "malformed-decisions",
                        f"{len(parsed.malformed_entries)} malformed decision "
                        "entr(y/ies); aperio reports but never rewrites history. "
                        "Move damaged entries into prose or fix them by hand.",
                        "decisions.md",
                    )
                )
            if parsed.duplicate_ids:
                warnings.append(
                    _finding(
                        "duplicate-decision-ids",
                        "duplicate decision ids: " + ", ".join(parsed.duplicate_ids),
                        "decisions.md",
                    )
                )
        except AperioError as exc:
            errors.append(_finding("decisions-invalid", str(exc), "decisions.md"))

    # --- handoff -------------------------------------------------------
    handoff_file = handoff_path(aperio_dir)
    if handoff_file.exists() and not handoff_file.is_symlink():
        try:
            handoff = read_handoff(aperio_dir)
            if handoff.structured is None:
                schemas["handoff"] = f"v{handoff.version}"
                warnings.append(
                    _finding(
                        "future-handoff-format",
                        f"handoff.md uses format v{handoff.version}, newer than "
                        "this aperio understands; preserved verbatim",
                        "handoff.md",
                    )
                )
            elif handoff.structured:
                schemas["handoff"] = f"v{handoff.version}"
            else:
                schemas["handoff"] = "legacy"
        except AperioError as exc:
            errors.append(_finding("handoff-invalid", str(exc), "handoff.md"))

    # --- execution records --------------------------------------------
    referenced_handoffs: set[str] = set()
    try:
        document = read_executions(aperio_dir)
        if document is not None:
            schemas["executions"] = document["schema_version"]
            tasks = {
                task["id"]: task for task in (plan["tasks"] if plan else [])
            }
            for run in document["runs"]:
                run_id = run["run_id"]
                if plan is not None and run["task_id"] not in tasks:
                    errors.append(
                        _finding(
                            "orphan-run-task",
                            f"run {run_id} references task {run['task_id']}, "
                            "which is not in the plan",
                            f"executions.json#{run_id}",
                        )
                    )
                if (
                    run["status"] == "awaiting-external"
                    and not run.get("handoff_ref")
                ):
                    errors.append(
                        _finding(
                            "missing-handoff-ref",
                            f"awaiting-external run {run_id} has no handoff_ref",
                            f"executions.json#{run_id}",
                        )
                    )
                ref = run.get("handoff_ref")
                if isinstance(ref, str) and ref:
                    referenced_handoffs.add(ref)
                    if not RUN_HANDOFF_PATTERN.match(ref):
                        errors.append(
                            _finding(
                                "invalid-handoff-ref",
                                f"run {run_id} has an unsafe handoff_ref {ref!r}",
                                f"executions.json#{run_id}",
                            )
                        )
                    elif not (aperio_dir / ref).is_file():
                        errors.append(
                            _finding(
                                "dangling-handoff-ref",
                                f"run {run_id} references {ref}, which is "
                                "missing; re-emit it with 'aperio run-handoff' "
                                "or record the loss with 'aperio decision-add'",
                                ref,
                            )
                        )
                local_ref = run.get("local_log_ref")
                if local_ref is not None and (
                    not isinstance(local_ref, str)
                    or not local_ref.startswith("local/")
                ):
                    errors.append(
                        _finding(
                            "invalid-local-ref",
                            f"run {run_id} has a local_log_ref outside "
                            ".aperio/local/ (portable records must never "
                            "reference other paths)",
                            f"executions.json#{run_id}",
                        )
                    )
    except AperioError as exc:
        errors.append(
            _finding("executions-invalid", str(exc), "executions.json")
        )

    # --- runs directory -------------------------------------------------
    runs = runs_dir(aperio_dir)
    if runs.exists():
        if os.path.islink(runs) or not runs.is_dir():
            errors.append(
                _finding(
                    "invalid-runs-dir",
                    ".aperio/runs exists but is not a real directory",
                    "runs",
                )
            )
        else:
            for entry in sorted(runs.iterdir(), key=lambda p: p.name):
                rel = f"runs/{entry.name}"
                if entry.is_symlink() or not entry.is_file():
                    errors.append(
                        _finding(
                            "invalid-run-artifact",
                            f"{rel} is not a regular file; run artifacts must "
                            "be plain files. Remove it and re-emit as needed.",
                            rel,
                        )
                    )
                    continue
                if not RUN_HANDOFF_PATTERN.match(rel):
                    warnings.append(
                        _finding(
                            "unexpected-run-artifact",
                            f"{rel} is not a recognized run handoff name; it is "
                            "excluded from export",
                            rel,
                        )
                    )
                elif rel not in referenced_handoffs:
                    warnings.append(
                        _finding(
                            "unreferenced-run-handoff",
                            f"{rel} is not referenced by any execution record; "
                            "it is historical and not exported",
                            rel,
                        )
                    )


    # --- workflow governance (Feature 06) -------------------------------
    policy = None
    policy_file = policy_path(aperio_dir)
    if policy_file.exists():
        if policy_file.is_symlink():
            errors.append(
                _finding("symlink-file", "policies.json is a symlink", "policies.json")
            )
        else:
            version = _schema_of(policy_file)
            schemas["policy"] = version
            if isinstance(version, int) and version > POLICY_SCHEMA_VERSION:
                errors.append(
                    _finding(
                        "policy-unsupported-schema",
                        f"policies.json uses schema version {version}, newer "
                        f"than supported {POLICY_SCHEMA_VERSION}; it is not "
                        "interpreted (upgrade aperio)",
                        "policies.json",
                    )
                )
            else:
                try:
                    policy = json.loads(policy_file.read_bytes().decode("utf-8"))
                    validate_policy_document(policy, policy_file)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(
                        _finding(
                            "policy-invalid",
                            f"policies.json is unreadable/invalid: {exc}",
                            "policies.json",
                        )
                    )
                except AperioError as exc:
                    errors.append(
                        _finding("policy-invalid", str(exc), "policies.json")
                    )

    reviews_file = reviews_path(aperio_dir)
    if reviews_file.exists():
        if reviews_file.is_symlink():
            errors.append(
                _finding("symlink-file", "reviews.json is a symlink", "reviews.json")
            )
        else:
            version = _schema_of(reviews_file)
            schemas["reviews"] = version
            if isinstance(version, int) and version > REVIEWS_SCHEMA_VERSION:
                errors.append(
                    _finding(
                        "reviews-unsupported-schema",
                        f"reviews.json uses schema version {version}, newer "
                        f"than supported {REVIEWS_SCHEMA_VERSION}; it is not "
                        "interpreted (upgrade aperio)",
                        "reviews.json",
                    )
                )
            else:
                try:
                    document = json.loads(
                        reviews_file.read_bytes().decode("utf-8")
                    )
                    validate_reviews_document(document, reviews_file)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(
                        _finding(
                            "reviews-invalid",
                            f"reviews.json is unreadable/invalid: {exc}",
                            "reviews.json",
                        )
                    )
                    document = None
                except AperioError as exc:
                    errors.append(
                        _finding("reviews-invalid", str(exc), "reviews.json")
                    )
                    document = None
                if document is not None:
                    if document["reviews"] and policy is None:
                        warnings.append(
                            _finding(
                                "reviews-without-policy",
                                "reviews.json exists without a valid "
                                "policies.json; approval history has no "
                                "configured actor mapping",
                                "reviews.json",
                            )
                        )
                    task_map = (
                        {task["id"]: task for task in plan["tasks"]}
                        if plan is not None
                        else {}
                    )
                    for review in document["reviews"]:
                        target = review["target"]
                        if target["kind"] != "task":
                            continue
                        if plan is not None and target["task_id"] not in task_map:
                            errors.append(
                                _finding(
                                    "review-orphan-task",
                                    f"review {review['review_id']} targets task "
                                    f"{target['task_id']}, which the plan does "
                                    "not define",
                                    "reviews.json",
                                )
                            )
                            continue
                        task = task_map.get(target["task_id"])
                        if task is None:
                            continue
                        for key, task_key in (
                            ("attempt", "attempt"),
                            ("task_revision", "revision"),
                        ):
                            if key in target and target[key] > int(
                                task.get(task_key, 0)
                            ):
                                errors.append(
                                    _finding(
                                        "review-future-work-ref",
                                        f"review {review['review_id']} targets "
                                        f"{key} {target[key]} of "
                                        f"{target['task_id']}, newer than the "
                                        f"task's {int(task.get(task_key, 0))}",
                                        "reviews.json",
                                    )
                                )

    audit_file = aperio_dir / "policy-audit.json"
    if (
        not audit_file.exists()
        and plan is not None
        and isinstance(plan.get("authors"), dict)
        and plan["authors"]
    ):
        errors.append(
            _finding(
                "attribution-audit-missing",
                "plan.json has durable author attribution but "
                "policy-audit.json is missing; approval is blocked until "
                "'aperio plan-sync-authors' recreates the attribution history",
                "policy-audit.json",
            )
        )
    if audit_file.exists():
        if audit_file.is_symlink():
            errors.append(
                _finding(
                    "symlink-file", "policy-audit.json is a symlink",
                    "policy-audit.json",
                )
            )
        else:
            schemas["audit"] = _schema_of(audit_file)
            try:
                audit = json.loads(audit_file.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(
                    _finding(
                        "audit-invalid",
                        f"policy-audit.json is unreadable/invalid: {exc}",
                        "policy-audit.json",
                    )
                )
            else:
                if (
                    not isinstance(audit, dict)
                    or audit.get("schema_version") != POLICY_SCHEMA_VERSION
                    or not isinstance(audit.get("events"), list)
                ):
                    errors.append(
                        _finding(
                            "audit-invalid",
                            "policy-audit.json must be schema "
                            f"{POLICY_SCHEMA_VERSION} with an 'events' list",
                            "policy-audit.json",
                        )
                    )
                elif plan is not None and isinstance(plan.get("authors"), dict):
                    events = audit["events"]
                    for revision_key, record in plan["authors"].items():
                        matches = {
                            event.get("actor")
                            for event in events
                            if isinstance(event, dict)
                            and event.get("action") in PLAN_ATTRIBUTION_ACTIONS
                            and event.get("revision") == int(revision_key)
                        }
                        if len(matches) > 1:
                            errors.append(
                                _finding(
                                    "attribution-audit-conflict",
                                    f"plan revision {revision_key} has "
                                    "conflicting authors in the attribution "
                                    "audit log ("
                                    + ", ".join(sorted(str(a) for a in matches))
                                    + "); approval is blocked until the "
                                    "history is reconciled",
                                    "policy-audit.json",
                                )
                            )
                        elif record.get("actor") not in matches:
                            errors.append(
                                _finding(
                                    "attribution-audit-missing",
                                    f"plan revision {revision_key} attributes "
                                    f"authorship to {record.get('actor')!r}, "
                                    "but the attribution audit record is "
                                    "missing; run 'aperio plan-sync-authors' "
                                    "to repair the append-only history",
                                    "policy-audit.json",
                                )
                            )

    # --- optional archive ----------------------------------------------
    archive_report = None
    if archive_path is not None:
        try:
            checked = read_portable_archive(archive_path)
            archive_report = {
                "archive": str(Path(os.path.abspath(os.path.expanduser(str(archive_path))))),
                "format": checked.manifest["format"],
                "format_version": checked.manifest["format_version"],
                "files": len(checked.files),
                "schemas": dict(checked.schemas),
                "warnings": list(checked.warnings),
            }
        except AperioError as exc:
            errors.append(
                _finding("archive-invalid", str(exc), "archive")
            )

    report = _finish(errors, warnings, schemas, missing, unexpected)
    report["execution"] = (
        None if not state_path(aperio_dir).exists() else _safe_execution(aperio_dir)
    )
    report["archive"] = archive_report
    return report


def _safe_execution(aperio_dir: Path) -> dict | None:
    try:
        return execution_summary(aperio_dir)
    except AperioError:
        return None


def _finish(
    errors: list[dict],
    warnings: list[dict],
    schemas: dict,
    missing: list[str],
    unexpected: list[str],
) -> dict:
    return {
        "command": "integrity",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "schemas": schemas,
        "supported": {
            "state": SCHEMA_VERSION,
            "plan": PLAN_SCHEMA_VERSION,
            "executions": EXECUTIONS_SCHEMA_VERSION,
        },
        "missing": missing,
        "unexpected": unexpected,
    }


__all__ = ["check_integrity", "EXPECTED_FILES"]
