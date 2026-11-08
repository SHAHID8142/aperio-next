"""Aperio command-line interface.

Commands:
    aperio --version                  print the version and exit
    aperio init <dir>                 initialize (or safely repair) a project
    aperio status <dir>               report project state (read-only)
    aperio memory <dir> [--json]      read portable project memory (Feature 02)
    aperio context-pack [dir] ...     build a read-only universal context pack
    aperio profiles [--json]          list built-in task profiles
    aperio goal-check <goal.json>     validate a goal and report completeness
    aperio goal-pack <goal.json> ...  build a context pack from a goal
    aperio decision-add <dir> ...     append a decision (append-only)
    aperio handoff-write <dir> --file <handoff.json>
                                      replace the handoff with a validated one
    aperio phase-set <dir> <phase>    update the project phase (compare-and-swap)
    aperio export <dir> [--output F]  export shareable context to a portable archive
    aperio import <dir> <archive>     import a portable archive (preview by default)
    aperio integrity <dir> [--archive F]
                                      validate portable context consistency
    aperio upgrade <dir> [--apply]    preview or apply explicit schema upgrades
    aperio agent-list [dir]           list built-in agent adapters
    aperio agent-doctor <dir> --agent ID [--config F] [--json]
                                      passively diagnose a provider CLI
    aperio roles [--json]             list built-in workflow roles and actions
    aperio policy-init <dir> ...      enable governance and assign declared actors
    aperio policy-show <dir>          inspect roles, actors, and rules
    aperio policy-assign <dir> ACTOR --role ROLE
                                      assign/change a declared actor role
    aperio policy-permissions <dir> --actor ID
                                      show one actor's permitted actions
    aperio review-submit <dir> --task TASK-001 ...
                                      submit an implementation for review
    aperio review-decide <dir> REV-0001 --outcome ...
                                      record an independent review decision
    aperio review-list <dir> [--task TASK-001]
    aperio review-status <dir>        pending reviews and completion blockers

Exit codes:
    0  success
    1  operational error (unsafe path, failed init, unreadable state, ...)
    2  usage error (handled by argparse)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from aperio import __version__
from aperio.context_pack import (
    DEFAULT_BUDGET_BYTES as CONTEXT_DEFAULT_BUDGET_BYTES,
    DEFAULT_PROFILE as CONTEXT_DEFAULT_PROFILE,
    MAX_BUDGET_BYTES as CONTEXT_MAX_BUDGET_BYTES,
    MAX_CONTEXT_ITEM_BYTES,
    MAX_OBJECTIVE_BYTES,
    PROFILE_VERSIONS,
    attached_context_item,
    build_context_pack,
    read_utf8_text_file,
    render_context_pack_markdown,
)
from aperio.artifacts import publish_text_atomic
from aperio.decisions import append_decision
from aperio.goals import (
    GOAL_SCHEMA_VERSION,
    build_goal_context_pack,
    describe_goal,
    load_goal_file,
)
from aperio.errors import AperioError, StateError, ValidationError
from aperio.handoff import (
    HANDOFF_FIELDS,
    HandoffContent,
    write_handoff,
)
from aperio.inputs import read_json_fields
from aperio.profiles import (
    DEFAULT_PROFILE as GOAL_DEFAULT_PROFILE,
    get_profile,
    list_profiles,
    profile_suggestions,
)
from aperio.integrity import check_integrity
from aperio.memory import load_context
from aperio.paths import resolve_target
from aperio.runtime import (
    cancel_run,
    dispatch_task,
    execution_preview,
    read_executions_or_fail,
    read_run_handoff,
    reconcile_runs,
    record_external_result,
)
from aperio.planmodel import ready_task_ids
from aperio.adapters import builtin_adapters, provider_doctor
from aperio.planstore import (
    adopt_plan_author,
    approve_plan,
    create_plan,
    plan_exists,
    plan_summary,
    read_plan,
    revise_plan,
    sync_plan_authors,
    update_task,
)
from aperio.portable import export_project, import_archive
from aperio.policy import (
    ALL_ACTIONS,
    ROLE_ACTIONS,
    ROLES,
    assign_actor,
    initialize_policy,
    policy_status,
    read_policy,
)
from aperio.reviews import (
    decide_review,
    list_reviews,
    review_status,
    submit_review,
)
from aperio.progress import update_phase
from aperio.upgrade import apply_upgrade, plan_upgrade
from aperio.project import (
    APERIO_DIR_NAME,
    InitResult,
    StatusReport,
    aperio_dir_for,
    formatted_schema_version,
    init_project,
    status_project,
)

_PROG = "aperio"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Aperio: universal, agent-agnostic context, memory, and "
            "reliability tooling. Prepare portable context for any "
            "knowledge-work task; the software-development workflow remains "
            "an optional domain profile."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{_PROG} {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="initialize an Aperio project (creates .aperio/); safe to re-run",
    )
    init_parser.add_argument(
        "project_directory", help="directory in which to create the project"
    )

    status_parser = subparsers.add_parser(
        "status",
        help="report project state (read-only)",
    )
    status_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )

    memory_parser = subparsers.add_parser(
        "memory",
        help="read portable project memory: state, brief, decisions, handoff",
    )
    memory_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    memory_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full context as machine-readable JSON",
    )

    context_parser = subparsers.add_parser(
        "context-pack",
        help="build a read-only universal context pack (Markdown or JSON)",
        description=(
            "Assemble portable context from an objective and optional "
            "operator fields or an initialized Aperio workspace. This "
            "command is read-only and never starts an agent."
        ),
    )
    context_parser.add_argument(
        "workspace", nargs="?",
        help="optional initialized Aperio project directory (read-only)",
    )
    objective_group = context_parser.add_mutually_exclusive_group()
    objective_group.add_argument(
        "--objective", help="task objective as UTF-8 text"
    )
    objective_group.add_argument(
        "--objective-file", metavar="FILE",
        help=f"read the objective from a UTF-8 file (max {MAX_OBJECTIVE_BYTES} bytes)",
    )
    brief_group = context_parser.add_mutually_exclusive_group()
    brief_group.add_argument(
        "--brief", help="optional operator brief as UTF-8 text",
    )
    brief_group.add_argument(
        "--brief-file", metavar="FILE",
        help=f"read the operator brief from a UTF-8 file "
             f"(max {MAX_CONTEXT_ITEM_BYTES} bytes)",
    )
    context_parser.add_argument(
        "--profile", choices=tuple(PROFILE_VERSIONS),
        default=CONTEXT_DEFAULT_PROFILE,
        help="context profile (default: general)",
    )
    context_parser.add_argument(
        "--budget", type=int, default=CONTEXT_DEFAULT_BUDGET_BYTES,
        metavar="BYTES",
        help="selected context-content budget in UTF-8 bytes "
             f"(0..{CONTEXT_MAX_BUDGET_BYTES}; default {CONTEXT_DEFAULT_BUDGET_BYTES})",
    )
    context_parser.add_argument(
        "--constraint", action="append", default=[], metavar="TEXT",
        help="explicit requirement or constraint; repeatable",
    )
    context_parser.add_argument(
        "--assumption", action="append", default=[], metavar="TEXT",
        help="explicit assumption; repeatable",
    )
    context_parser.add_argument(
        "--risk", action="append", default=[], metavar="TEXT",
        help="explicit risk; repeatable",
    )
    context_parser.add_argument(
        "--quality-criterion", action="append", default=[], metavar="TEXT",
        help="explicit quality criterion; repeatable",
    )
    context_parser.add_argument(
        "--expected-output", help="expected deliverable or result",
    )
    context_parser.add_argument(
        "--output-format", help="expected output format or structure",
    )
    context_parser.add_argument(
        "--audience", help="intended audience",
    )
    context_parser.add_argument(
        "--missing-information", action="append", default=[], metavar="TEXT",
        help="known missing information; repeatable",
    )
    context_parser.add_argument(
        "--imported-context", action="append", default=[], metavar="FILE",
        help="operator-selected external context, labeled imported/untrusted; "
             "repeatable",
    )
    context_parser.add_argument(
        "--proposed-context", action="append", default=[], metavar="FILE",
        help="operator-selected agent claim, labeled proposed; repeatable",
    )
    context_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the validated JSON context pack instead of Markdown",
    )

    profiles_parser = subparsers.add_parser(
        "profiles",
        help="list built-in task profiles (data only, no provider calls)",
        description=(
            "Show the built-in task profiles: the input fields each domain "
            "usually uses and the criteria it suggests. Suggestions never "
            "gate approval or completion."
        ),
    )
    profiles_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the profile registry as machine-readable JSON",
    )

    goal_check_parser = subparsers.add_parser(
        "goal-check",
        help="validate a goal document and report what is missing",
        description=(
            "Validate a goal document against goal-schema v"
            f"{GOAL_SCHEMA_VERSION} and report content identity, profile, "
            "trust, and which optional fields are missing. Read-only."
        ),
    )
    goal_check_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    goal_check_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the validation report as JSON",
    )

    goal_pack_parser = subparsers.add_parser(
        "goal-pack",
        help="build a portable context pack from a goal document",
        description=(
            "Convert a validated goal into a provider-neutral context pack "
            "that can be pasted into any agent. Read-only: it never starts "
            "an agent, approves work, or writes into a project."
        ),
    )
    goal_pack_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    goal_pack_parser.add_argument(
        "workspace", nargs="?",
        help="optional initialized Aperio project directory (read-only)",
    )
    goal_pack_parser.add_argument(
        "--budget", type=int, default=CONTEXT_DEFAULT_BUDGET_BYTES,
        metavar="BYTES",
        help="selected context-content budget in UTF-8 bytes "
             f"(0..{CONTEXT_MAX_BUDGET_BYTES}; default {CONTEXT_DEFAULT_BUDGET_BYTES})",
    )
    goal_pack_parser.add_argument(
        "--imported-context", action="append", default=[], metavar="FILE",
        help="operator-selected external context, labeled imported/untrusted; "
             "repeatable",
    )
    goal_pack_parser.add_argument(
        "--proposed-context", action="append", default=[], metavar="FILE",
        help="operator-selected agent claim, labeled proposed; repeatable",
    )
    goal_pack_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the validated JSON context pack instead of Markdown",
    )
    goal_pack_parser.add_argument(
        "--output", metavar="FILE",
        help="write the pack to a file instead of stdout",
    )
    goal_pack_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    decision_parser = subparsers.add_parser(
        "decision-add",
        help="append one decision to .aperio/decisions.md (append-only)",
    )
    decision_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    decision_parser.add_argument(
        "--file",
        help="JSON file with title/context/decision/consequences "
        "(alternative to the individual flags)",
    )
    decision_parser.add_argument("--title", help="single-line decision title")
    decision_parser.add_argument("--context", help="why this decision came up")
    decision_parser.add_argument("--decision", help="what was decided")
    decision_parser.add_argument(
        "--consequences", help="what this commits the project to"
    )

    handoff_parser = subparsers.add_parser(
        "handoff-write",
        help="replace .aperio/handoff.md with a validated structured handoff",
    )
    handoff_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    handoff_parser.add_argument(
        "--file",
        required=True,
        help=f"JSON file with the fields: {', '.join(HANDOFF_FIELDS)}; "
        'optionally "revises": <current revision> to guard against '
        "competing writers",
    )
    handoff_parser.add_argument(
        "--force",
        action="store_true",
        help="deliberately replace the current handoff without a revision "
        "declaration (discards its content, including unreadable newer "
        "formats)",
    )

    phase_parser = subparsers.add_parser(
        "phase-set",
        help="update the project phase (compare-and-swap, lost-update safe)",
    )
    phase_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    phase_parser.add_argument("phase", help="new project phase (single line)")

    plan_create_parser = subparsers.add_parser(
        "plan-create",
        help="create the project plan from a structured JSON file (draft, revision 1)",
    )
    plan_create_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_create_parser.add_argument(
        "--file",
        required=True,
        help="JSON file with the plan content (objective, expected_outcome, "
        "scope, exclusions, assumptions, risks, strategy, "
        "acceptance_criteria, verification_strategy, tasks)",
    )
    plan_create_parser.add_argument(
        "--actor", help="declared actor id (required when governance is enabled)"
    )

    plan_show_parser = subparsers.add_parser(
        "plan-show",
        help="show the current plan, its approval state, and task readiness",
    )
    plan_show_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_show_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full plan document as machine-readable JSON",
    )

    plan_adopt_parser = subparsers.add_parser(
        "plan-adopt",
        help="explicitly adopt authorship of a pre-governance plan revision",
    )
    plan_adopt_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_adopt_parser.add_argument(
        "--revision", type=int,
        help="revision to adopt (default: the current plan revision)",
    )
    plan_adopt_parser.add_argument(
        "--author", required=True,
        help="declared actor id that authored the revision",
    )
    plan_adopt_parser.add_argument(
        "--reason", required=True,
        help="why the attribution is being adopted (recorded in history)",
    )
    plan_adopt_parser.add_argument(
        "--actor", help="declared operator adopting it (planner role)",
    )

    plan_sync_parser = subparsers.add_parser(
        "plan-sync-authors",
        help="append missing plan attribution audit records (never rewrites)",
    )
    plan_sync_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_sync_parser.add_argument(
        "--actor", help="declared operator performing the repair (planner role)",
    )

    plan_approve_parser = subparsers.add_parser(
        "plan-approve",
        help="record plan approval (operational marker after genuine review)",
    )
    plan_approve_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_approve_parser.add_argument(
        "--revision",
        type=int,
        help="approve this specific plan_version (default: the current one)",
    )
    plan_approve_parser.add_argument(
        "--by",
        help="label recording who approved (not cryptographic proof)",
    )
    plan_approve_parser.add_argument(
        "--actor", help="declared actor id (required when governance is enabled)"
    )

    plan_revise_parser = subparsers.add_parser(
        "plan-revise",
        help="replace the plan content with a new draft revision",
    )
    plan_revise_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_revise_parser.add_argument(
        "--file",
        required=True,
        help="JSON file with the new plan content (lifecycle state of "
        "surviving tasks is carried over automatically)",
    )
    plan_revise_parser.add_argument(
        "--revises",
        type=int,
        required=True,
        help="the plan_version being revised (stale revisions are refused)",
    )
    plan_revise_parser.add_argument(
        "--actor", help="declared actor id (required when governance is enabled)"
    )

    task_list_parser = subparsers.add_parser(
        "task-list",
        help="list plan tasks with their status and derived readiness",
    )
    task_list_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    task_list_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit tasks with derived readiness as machine-readable JSON",
    )

    task_status_parser = subparsers.add_parser(
        "task-status",
        help="perform a validated task-status transition (execution tracking)",
    )
    task_status_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    task_status_parser.add_argument("task_id", help="task id, e.g. TASK-001")
    task_status_parser.add_argument(
        "--status",
        help="new status: pending (unblock), in-progress (start/reopen), "
        "blocked, or completed (required unless --file provides it)",
    )
    task_status_parser.add_argument(
        "--file",
        help="JSON file with the optional payload fields (owner, reason, "
        "evidence, limitations, result) - alternative to the flags",
    )
    task_status_parser.add_argument("--owner", help="agent claiming the task (required to start)")
    task_status_parser.add_argument("--reason", help="reason (required to block)")
    task_status_parser.add_argument("--evidence", help="verification evidence (required to complete)")
    task_status_parser.add_argument(
        "--limitations", help="verification limitations (required to complete)"
    )
    task_status_parser.add_argument(
        "--result", help="verification result: passed or failed (completion requires passed)"
    )
    task_status_parser.add_argument(
        "--actor", help="declared actor id (required when governance is enabled)"
    )

    agent_list_parser = subparsers.add_parser(
        "agent-list",
        help="list the built-in agent adapters and their availability",
    )
    agent_list_parser.add_argument(
        "project_directory", nargs="?", help="unused; accepted for symmetry"
    )

    doctor_parser = subparsers.add_parser(
        "agent-doctor",
        help="passively diagnose a provider CLI (no login or model request)",
    )
    doctor_parser.add_argument("project_directory")
    doctor_parser.add_argument(
        "--agent", required=True,
        help="provider adapter id: codex-cli or claude-cli",
    )
    doctor_parser.add_argument("--config", help="adapter config JSON file")
    doctor_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the diagnostic report as machine-readable JSON",
    )

    run_preview_parser = subparsers.add_parser(
        "run-preview",
        help="show a deterministic, side-effect-free execution preview",
    )
    run_preview_parser.add_argument("project_directory")
    run_preview_parser.add_argument("task_id", help="task id, e.g. TASK-001")
    run_preview_parser.add_argument(
        "--agent", required=True,
        help="adapter id: manual, local-process, codex-cli, or claude-cli",
    )
    run_preview_parser.add_argument("--config", help="adapter config JSON file")
    run_preview_parser.add_argument(
        "--authorize-write",
        "--authorize-workspace-write",
        dest="authorize_workspace_write",
        action="store_true",
        help="show effective permissions as if workspace-write were authorized "
        "(preview only; this does not launch anything)",
    )

    run_start_parser = subparsers.add_parser(
        "run-start",
        help="dispatch a task run (subprocess adapters require "
        "--authorize-exec to actually execute; otherwise preparation only)",
    )
    run_start_parser.add_argument("project_directory")
    run_start_parser.add_argument("task_id")
    run_start_parser.add_argument(
        "--agent", required=True,
        help="adapter id: manual, local-process, codex-cli, or claude-cli",
    )
    run_start_parser.add_argument("--config", help="adapter config JSON file")
    run_start_parser.add_argument(
        "--authorize-exec",
        dest="authorize_exec",
        action="store_true",
        help="EXPLICIT human authorization for launching a real subprocess; "
        "without it the run is preparation only",
    )
    run_start_parser.add_argument(
        "--authorize-write",
        "--authorize-workspace-write",
        dest="authorize_workspace_write",
        action="store_true",
        help="SEPARATE explicit authorization for provider workspace-write "
        "or shell-capable modes; read-only launches do not need it",
    )
    run_start_parser.add_argument(
        "--previous-run", help="link a retry to the run it replaces"
    )
    run_start_parser.add_argument(
        "--actor", help="declared actor id (required when governance is enabled)"
    )
    run_start_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the run record as machine-readable JSON",
    )

    run_status_parser = subparsers.add_parser(
        "run-status", help="show one execution run (JSON with --json)"
    )
    run_status_parser.add_argument("project_directory")
    run_status_parser.add_argument("run_id")
    run_status_parser.add_argument(
        "--json", dest="as_json", action="store_true"
    )

    runs_list_parser = subparsers.add_parser(
        "runs-list", help="list all execution runs, newest first"
    )
    runs_list_parser.add_argument("project_directory")
    runs_list_parser.add_argument(
        "--json", dest="as_json", action="store_true"
    )

    run_cancel_parser = subparsers.add_parser(
        "run-cancel", help="cancel an active run (never steals locks; "
        "never resurrects finalized runs)"
    )
    run_cancel_parser.add_argument("project_directory")
    run_cancel_parser.add_argument("run_id")
    run_cancel_parser.add_argument("--reason", help="why the run is cancelled")

    runs_reconcile_parser = subparsers.add_parser(
        "runs-reconcile",
        help="detect interrupted/stale runs without inventing success",
    )
    runs_reconcile_parser.add_argument("project_directory")
    runs_reconcile_parser.add_argument(
        "--json", dest="as_json", action="store_true"
    )

    run_report_parser = subparsers.add_parser(
        "run-report",
        help="record an external agent's result on an awaiting-external run",
    )
    run_report_parser.add_argument("project_directory")
    run_report_parser.add_argument("run_id")
    run_report_parser.add_argument(
        "--file", required=True,
        help="JSON file: outcome_reported (string), success_reported "
        "(boolean), optional diagnostics (string)",
    )

    run_handoff_parser = subparsers.add_parser(
        "run-handoff",
        help="re-emit the prepared run-handoff document for a manual run",
    )
    run_handoff_parser.add_argument("project_directory")
    run_handoff_parser.add_argument("run_id")

    export_parser = subparsers.add_parser(
        "export",
        help="export shareable project context to a portable ZIP archive",
    )
    export_parser.add_argument("project_directory")
    export_parser.add_argument(
        "--output",
        help="archive path (must not exist; default: ./aperio-<name>-<utc>.zip)",
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and preview exactly what would be exported; write nothing",
    )
    export_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit a machine-readable report",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="import a portable archive into a project (validate + preview first)",
    )
    import_parser.add_argument("project_directory")
    import_parser.add_argument("archive", help="portable archive to import")
    import_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace existing portable context (explicit operator approval; "
             "review the archive first)",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the complete archive and preview actions; write nothing",
    )
    import_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit a machine-readable report",
    )

    integrity_parser = subparsers.add_parser(
        "integrity",
        help="validate the portable context of a project (read-only)",
    )
    integrity_parser.add_argument("project_directory")
    integrity_parser.add_argument(
        "--archive",
        help="also validate this portable archive (manifest hashes included)",
    )
    integrity_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit a machine-readable report",
    )

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="preview (default) or apply explicit, versioned schema upgrades",
    )
    upgrade_parser.add_argument("project_directory")
    upgrade_parser.add_argument(
        "--apply",
        action="store_true",
        help="apply implemented migrations after writing a backup",
    )
    upgrade_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit a machine-readable report",
    )
    task_status_parser.add_argument(
        "--reopen",
        action="store_true",
        help="deliberately reopen a completed task (clears its verification "
        "block - record durable history with 'aperio decision-add' first)",
    )

    roles_parser = subparsers.add_parser(
        "roles", help="list built-in workflow roles and permitted actions",
    )
    roles_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the role matrix as machine-readable JSON",
    )

    policy_init_parser = subparsers.add_parser(
        "policy-init",
        help="create and enable workflow governance for a project (never replaces)",
    )
    policy_init_parser.add_argument("project_directory")
    policy_init_parser.add_argument(
        "--assign", action="append", default=[], metavar="ACTOR:ROLE",
        help="assign a declared actor to planner, implementer, or reviewer "
             "(repeatable)",
    )
    policy_init_parser.add_argument(
        "--by", help="declared operator label recording who set this up",
    )
    policy_init_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the policy document as machine-readable JSON",
    )

    policy_show_parser = subparsers.add_parser(
        "policy-show", help="inspect the project policy and actor assignments",
    )
    policy_show_parser.add_argument("project_directory")
    policy_show_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the policy document as machine-readable JSON",
    )

    policy_assign_parser = subparsers.add_parser(
        "policy-assign",
        help="assign an actor to a role (history is preserved)",
    )
    policy_assign_parser.add_argument("project_directory")
    policy_assign_parser.add_argument("actor", help="declared actor id")
    policy_assign_parser.add_argument(
        "--role", required=True, choices=ROLES,
        help="planner, implementer, or reviewer",
    )
    policy_assign_parser.add_argument("--by", help="declared operator label")
    policy_assign_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the updated policy as machine-readable JSON",
    )

    policy_permissions_parser = subparsers.add_parser(
        "policy-permissions",
        help="show the declared permissions and duties of one actor",
    )
    policy_permissions_parser.add_argument("project_directory")
    policy_permissions_parser.add_argument("--actor", required=True)
    policy_permissions_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit permissions as machine-readable JSON",
    )

    review_submit_parser = subparsers.add_parser(
        "review-submit",
        help="submit an in-progress implementation for independent review",
    )
    review_submit_parser.add_argument("project_directory")
    review_submit_parser.add_argument(
        "--task", required=True, help="task id being submitted",
    )
    review_submit_parser.add_argument("--run", help="optional run id for evidence")
    review_submit_parser.add_argument("--actor", help="declared implementer id")
    review_submit_parser.add_argument(
        "--scope", action="append", default=[],
        help="reviewed scope item (repeatable; at least one required)",
    )
    review_submit_parser.add_argument(
        "--evidence", action="append", default=[],
        help="evidence reference (repeatable; at least one required)",
    )
    review_submit_parser.add_argument(
        "--limitations", help="known limitations (recorded verbatim, single line)",
    )
    review_submit_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the review record as machine-readable JSON",
    )

    review_decide_parser = subparsers.add_parser(
        "review-decide",
        help="record an immutable approval, changes request, or rejection",
    )
    review_decide_parser.add_argument("project_directory")
    review_decide_parser.add_argument("review_id", help="review id, e.g. REV-0001")
    review_decide_parser.add_argument("--actor", help="declared reviewer id")
    review_decide_parser.add_argument(
        "--outcome", required=True,
        choices=("approved", "changes-requested", "rejected"),
    )
    review_decide_parser.add_argument(
        "--findings", help="required for changes-requested and rejected",
    )
    review_decide_parser.add_argument("--limitations")
    review_decide_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the decided review as machine-readable JSON",
    )

    review_list_parser = subparsers.add_parser(
        "review-list", help="list review history (append-only)",
    )
    review_list_parser.add_argument("project_directory")
    review_list_parser.add_argument("--task", help="only this task's reviews")
    review_list_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the reviews as machine-readable JSON",
    )

    review_status_parser = subparsers.add_parser(
        "review-status",
        help="show pending reviews and what still blocks verified completion",
    )
    review_status_parser.add_argument("project_directory")
    review_status_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the status as machine-readable JSON",
    )

    return parser


def _print_init(result: InitResult) -> None:
    if result.already_initialized:
        assert result.state is not None
        print(
            f"Aperio project already initialized at {result.target} "
            f"(schema v{result.state.schema_version})"
        )
    else:
        print(f"Aperio project initialized at {result.target}")

    for rel in result.created:
        print(f"  created  {rel}")
    for rel in result.present:
        print(f"  present  {rel}")
    if result.already_initialized and not result.created:
        print("  nothing to do: all context files present, nothing overwritten")


def _print_status(report: StatusReport) -> None:
    state = report.state
    print(f"Project:     {state.name}")
    print(f"Location:    {report.target}")
    print(f"Schema:      {formatted_schema_version(state)}")
    print(f"Phase:       {state.phase}")
    print(f"Created:     {state.created_at}")
    print(f"Updated:     {state.updated_at}")
    print(f"aperio:      {state.aperio_version}")
    print()
    print("Context files:")
    for rel, exists in report.files.items():
        marker = "present" if exists else "MISSING"
        print(f"  {marker:<8} {APERIO_DIR_NAME}/{rel}")
    missing = [rel for rel, exists in report.files.items() if not exists]
    if missing:
        print()
        print(
            "Some context files are missing; "
            f"'aperio init {report.target}' can repair them without overwriting."
        )


def _print_memory(context, as_json: bool) -> None:
    if as_json:
        print(json.dumps(context.to_dict(), indent=2, ensure_ascii=False))
        return
    state = context.state
    decision_count = len(context.decisions) if context.decisions is not None else 0
    print(f"Project:     {state.name}")
    print(f"Location:    {context.target}")
    print(f"Schema:      {formatted_schema_version(state)}")
    print(f"Phase:       {state.phase}")
    print(f"Updated:     {state.updated_at}")
    print(f"Decisions:   {decision_count}")
    if context.handoff is None:
        handoff_state = "MISSING"
    elif context.handoff.structured:
        handoff_state = "structured v1"
    elif context.handoff.structured is None:
        handoff_state = f"newer format v{context.handoff.version} (raw text preserved)"
    else:
        handoff_state = "legacy free-form"
    print(f"Handoff:     {handoff_state}")
    if context.missing:
        print(f"Missing:     {', '.join(context.missing)} (run 'aperio init' to repair)")
    if context.malformed_entries:
        print(
            f"Damaged:     {len(context.malformed_entries)} malformed decision "
            "entries (reported, never fatal - see docs/architecture.md)"
        )
    print()
    print("Full machine-readable context: aperio memory <dir> --json")


def _cmd_context_pack(args) -> None:
    objective = args.objective
    if args.objective_file:
        objective = read_utf8_text_file(
            args.objective_file,
            label="objective file",
            max_bytes=MAX_OBJECTIVE_BYTES,
        )
    brief = args.brief
    if args.brief_file:
        brief = read_utf8_text_file(
            args.brief_file,
            label="brief file",
            max_bytes=MAX_CONTEXT_ITEM_BYTES,
        )
    imported_items = []
    for index, path in enumerate(args.imported_context, start=1):
        imported_items.append(
            attached_context_item(
                read_utf8_text_file(path, label="imported context file"),
                trust="imported",
                ordinal=index,
            )
        )
    proposed_items = []
    for index, path in enumerate(args.proposed_context, start=1):
        proposed_items.append(
            attached_context_item(
                read_utf8_text_file(path, label="proposed context file"),
                trust="proposed",
                ordinal=index,
            )
        )
    document = build_context_pack(
        objective,
        workspace=args.workspace,
        profile=args.profile,
        budget_bytes=args.budget,
        brief=brief,
        constraints=args.constraint,
        assumptions=args.assumption,
        risks=args.risk,
        quality_criteria=args.quality_criterion,
        expected_output=args.expected_output,
        output_format=args.output_format,
        audience=args.audience,
        missing_information=args.missing_information,
        imported_items=imported_items,
        proposed_items=proposed_items,
    )
    if args.as_json:
        sys.stdout.write(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
    else:
        sys.stdout.write(render_context_pack_markdown(document))


def _cmd_profiles(args) -> None:
    if args.as_json:
        sys.stdout.write(
            json.dumps({"profiles": list_profiles()}, ensure_ascii=False, indent=2)
            + "\n"
        )
        return
    default = get_profile(GOAL_DEFAULT_PROFILE)
    print("Built-in task profiles (data only; suggestions never gate work)")
    print()
    for profile in list_profiles():
        marker = " (default)" if profile["id"] == default["id"] else ""
        print(f"{profile['id']} v{profile['version']}{marker}")
        print(f"  {profile['description']}")
        print(f"  Usually specified: {', '.join(profile['focus_fields'])}")
        print()
    print("Inspect one profile: aperio profiles --json")


def _cmd_goal_check(args) -> None:
    goal = load_goal_file(args.goal_file)
    report = describe_goal(goal)
    suggestions = profile_suggestions(goal["profile"])
    payload = {"report": report, "suggestions": suggestions}
    if args.as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    print("Goal valid" + f" (goal-schema v{report['goal_schema_version']})")
    print(f"Identity:     {report['goal_identity']}")
    print(f"Content hash: {report['goal_sha256']}")
    print(
        f"Profile:      {report['profile']} v{report['profile_version']}"
    )
    print(f"Trust:        {report['trust']} (declared source: "
          f"{report['provenance']['source']})")
    if report["trust"] != "authoritative":
        print(
            "Instructions: attributed to "
            f"`{report['provenance']['source']}` - not operator instructions; "
            "a context pack renders them as untrusted proposals"
        )
    work = report["work"]
    if work["task_count"]:
        print(
            f"Work:         {work['task_count']} task(s); dependency levels: "
            + " -> ".join(str(level) for level in work["levels"])
        )
    else:
        print("Work:         none (a single task needs no decomposition)")
    missing = report["completeness"]["missing"]
    if missing:
        print()
        print("Not supplied by the operator (Aperio does not invent values):")
        for entry in missing:
            print(f"  - {entry['field']}: {entry['detail']}")
    print()
    print(
        "Profile suggestions are advisory and never gate approval or "
        "completion."
    )
    print("Build a pack: aperio goal-pack " + str(args.goal_file))


def _check_output_destination(raw: str, *, goal_path: str) -> Path:
    """Validate a goal-pack output path before anything is written.

    Refuses: a missing/non-directory parent, any path that resolves into
    Aperio-managed project state (``.aperio/``, including via a symlinked
    directory), and the goal document the pack was derived from. Symlink and
    non-regular leaves are rejected later by the publishing primitive.
    """
    target = Path(raw)
    parent = target.parent
    if not parent.exists():
        raise ValidationError("output directory does not exist")
    if not parent.is_dir():
        raise ValidationError("output path parent is not a directory")
    resolved_parent = Path(os.path.realpath(parent))
    resolved = resolved_parent / target.name
    if APERIO_DIR_NAME in resolved.parts:
        raise ValidationError(
            "refusing to write into Aperio project state: the output path "
            f"resolves inside '{APERIO_DIR_NAME}/', which holds managed "
            "project memory; choose a path outside it"
        )
    if os.path.realpath(target) == os.path.realpath(goal_path):
        raise ValidationError(
            "refusing to overwrite the goal file with its own context pack; "
            "choose a different --output path"
        )
    return target


def _write_pack_output(
    path: str, text: str, *, force: bool, goal_path: str
) -> None:
    target = _check_output_destination(path, goal_path=goal_path)
    publish_text_atomic(target, text, force=force, what="context pack")


def _cmd_goal_pack(args) -> None:
    goal = load_goal_file(args.goal_file)
    imported_items = []
    for index, path in enumerate(args.imported_context, start=1):
        imported_items.append(
            attached_context_item(
                read_utf8_text_file(path, label="imported context file"),
                trust="imported",
                ordinal=index,
            )
        )
    proposed_items = []
    for index, path in enumerate(args.proposed_context, start=1):
        proposed_items.append(
            attached_context_item(
                read_utf8_text_file(path, label="proposed context file"),
                trust="proposed",
                ordinal=index,
            )
        )
    document = build_goal_context_pack(
        goal,
        workspace=args.workspace,
        budget_bytes=args.budget,
        imported_items=imported_items,
        proposed_items=proposed_items,
    )
    text = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if args.as_json
        else render_context_pack_markdown(document)
    )
    if args.output:
        _write_pack_output(
            args.output, text, force=args.force, goal_path=args.goal_file
        )
        print(f"Wrote context pack to {args.output}")
        return
    sys.stdout.write(text)


def _cmd_decision_add(args) -> None:
    flags_used = [
        name
        for name in ("title", "context", "decision", "consequences")
        if getattr(args, name) is not None
    ]
    if args.file is not None and flags_used:
        raise ValidationError(
            "decision-add: --file and the individual flags are mutually "
            f"exclusive (flags given: {', '.join('--' + f for f in flags_used)})"
        )
    if args.file is not None:
        fields = read_json_fields(
            args.file,
            ("title", "context", "decision", "consequences"),
        )
    else:
        missing = [
            name
            for name in ("title", "context", "decision", "consequences")
            if getattr(args, name) is None
        ]
        if missing:
            raise ValidationError(
                "decision-add: provide --file or all four flags "
                f"(missing: {', '.join('--' + name for name in missing)})"
            )
        fields = {
            "title": args.title,
            "context": args.context,
            "decision": args.decision,
            "consequences": args.consequences,
        }
    target = resolve_target(args.project_directory, for_writing=True)
    entry = append_decision(
        aperio_dir_for(target),
        title=fields["title"],
        context=fields["context"],
        decision=fields["decision"],
        consequences=fields["consequences"],
    )
    print(f"appended {entry.id} to {APERIO_DIR_NAME}/decisions.md")


def _cmd_handoff_write(args) -> None:
    fields = read_json_fields(
        args.file, HANDOFF_FIELDS, optional_int_fields=("revises",)
    )
    revises = fields.pop("revises", None)
    target = resolve_target(args.project_directory, for_writing=True)
    revision = write_handoff(
        aperio_dir_for(target),
        HandoffContent(**fields),
        expected_revision=revises,
        force=args.force,
    )
    print(
        f"handoff written to {APERIO_DIR_NAME}/handoff.md "
        f"(7 sections validated, revision {revision})"
        + (" [forced]" if args.force else "")
    )


def _cmd_phase_set(args) -> None:
    result = update_phase(args.project_directory, args.phase)
    if result.updated:
        print(f"phase: {result.previous_phase} -> {result.new_phase}")
    else:
        print(f"phase unchanged: {result.new_phase}")


def _cmd_plan_create(args) -> None:
    content = _read_plan_input(args.file)
    target = resolve_target(args.project_directory, for_writing=True)
    document = create_plan(
        aperio_dir_for(target), content, actor=args.actor
    )
    print(
        f"plan created at {APERIO_DIR_NAME}/plan.json "
        f"(revision {document['plan_version']}, draft; "
        f"{len(document['tasks'])} task(s)). Execution tracking requires "
        "approval: aperio plan-approve"
    )


def _cmd_plan_show(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    if args.as_json:
        print(json.dumps(read_plan(aperio_dir), indent=2, ensure_ascii=False))
        return
    summary = plan_summary(aperio_dir)
    if summary is None:
        print("No plan in this project; create one with 'aperio plan-create'.")
        return
    if summary.get("unsupported_schema"):
        print(
            f"Plan present but written in unsupported plan-schema version "
            f"{summary.get('schema_version')}; aperio cannot display or "
            "modify it. Inspect .aperio/plan.json manually."
        )
        return
    print(f"Plan:        revision {summary['plan_version']} ({summary['approval_status']})")
    if summary["approval_status"] == "approved":
        print(f"Approved:    revision {summary['approved_revision']} (provenance stamp - see docs)")
    else:
        print("Approval:    required before execution tracking")
    print(f"Objective:   {summary['objective']}")
    print(f"Tasks:       {summary['task_counts']}")
    print(f"Ready:       {', '.join(summary['ready_task_ids']) or '(none)'}")
    if summary["blocked_task_ids"]:
        print(f"Blocked:     {', '.join(summary['blocked_task_ids'])}")
    print()
    print("Full document: aperio plan-show <dir> --json")


def _cmd_plan_adopt(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    aperio_dir = aperio_dir_for(target)
    revision = args.revision
    if revision is None:
        revision = read_plan(aperio_dir)["plan_version"]
    document = adopt_plan_author(
        aperio_dir, revision, args.author,
        reason=args.reason, actor=args.actor,
    )
    print(
        f"plan revision {revision} authorship adopted as {args.author!r} "
        f"(recorded in the plan and attribution audit; approval still "
        "requires an independent decision)"
    )


def _cmd_plan_sync_authors(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    repaired = sync_plan_authors(
        aperio_dir_for(target), actor=args.actor
    )
    if repaired:
        print(
            "attribution audit repaired for revision(s): "
            + ", ".join(str(item) for item in repaired)
        )
    else:
        print("attribution audit already complete; nothing was modified")


def _cmd_plan_approve(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    document = approve_plan(
        aperio_dir_for(target), revision=args.revision, by=args.by,
        actor=args.actor,
    )
    approval = document["approval"]
    print(
        f"plan approval recorded: revision {approval['approved_revision']} "
        f"approved at {approval['approved_at']}"
        + (f" by {approval['approved_by']}" if approval["approved_by"] else "")
        + " (operational marker; genuine human review is the operator's "
        "responsibility)"
    )


def _cmd_plan_revise(args) -> None:
    content = _read_plan_input(args.file)
    target = resolve_target(args.project_directory, for_writing=True)
    document = revise_plan(
        aperio_dir_for(target), content, revises=args.revises,
        actor=args.actor,
    )
    print(
        f"plan revised: now revision {document['plan_version']} "
        f"({len(document['tasks'])} task(s); approval reset to draft - "
        "renewed approval required before execution tracking)"
    )


def _cmd_task_list(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    if not plan_exists(aperio_dir):
        print("No plan in this project; create one with 'aperio plan-create'.")
        return
    plan = read_plan(aperio_dir)
    approved = plan["approval"]["status"] == "approved"
    ready = set(ready_task_ids(plan))  # dependency-ready
    executable = ready if approved else set()
    if args.as_json:
        listing = [
            dict(
                task,
                is_ready=task["id"] in ready,
                is_executable=task["id"] in executable,
            )
            for task in plan["tasks"]
        ]
        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return
    if not approved:
        print(
            "Plan approval: DRAFT - dependency-ready tasks are NOT "
            "executable until the plan is approved (aperio plan-approve)."
        )
    for task in sorted(plan["tasks"], key=lambda t: t["id"]):
        if task["id"] in executable:
            marker = "ready"
        elif task["id"] in ready:
            marker = "dep-ready"
        else:
            marker = f"{task['status']:>11}"
        owner = f" (owner: {task['owner']})" if task["owner"] else ""
        print(f"  [{marker}] {task['id']}: {task['title']}{owner}")


def _read_plan_input(path: str) -> dict:
    """Read a plan create/revise input file: strict content validation
    happens in the model layer; here we only parse the JSON file."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read plan input file {path}: {exc.strerror or exc}"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            f"plan input file {path} is not valid UTF-8; re-save it as UTF-8"
        ) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"plan input file {path} is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None


def _cmd_agent_list(args) -> None:
    for adapter in builtin_adapters():
        available, reason = adapter.available()
        status = "available" if available else f"unavailable: {reason}"
        print(f"  {adapter.id:<14} {adapter.display_name} [{status}]")
    print()
    print(
        "local-process, codex-cli, and claude-cli require --authorize-exec "
        "for real subprocess execution; provider write-capable modes also "
        "require the separate --authorize-write. Providers default to "
        "read-only. manual prepares a handoff and never executes."
    )


def _cmd_agent_doctor(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    report = provider_doctor(
        args.agent,
        config=_read_adapter_config(args.config),
        aperio_dir=aperio_dir_for(target),
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(f"adapter:          {report['adapter_id']}")
    print(f"installed:        {report['installed']}")
    if report.get("executable"):
        print(f"executable:       {report['executable']}")
    print(f"version:          {report.get('version') or 'unknown'}")
    if report.get("version_error"):
        print(f"version error:    {report['version_error']}")
    if report.get("installation_error"):
        print(f"installation:     {report['installation_error']}")
    print(f"config valid:     {report['configuration']['valid']}")
    if report["configuration"].get("error"):
        print(f"config error:     {report['configuration']['error']}")
    print("required flags:   " + ", ".join(report.get("required_flags") or []))
    if report.get("missing_controls"):
        print("missing controls: " + ", ".join(report["missing_controls"]))
    print(f"read-only:        "
          f"{report['read_only_execution']['supported']}")
    print(f"workspace-write:  "
          f"{report['workspace_write_execution']['supported']} "
          f"({report['workspace_write_execution']['requires']})")
    print("authentication:   not checked (passive diagnostics)")
    print("live integration: unverified (no model request made)")
    if report.get("capability_error"):
        print(f"capability:       {report['capability_error']}")


def _read_adapter_config(path: str | None) -> dict | None:
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read adapter config {path}: {exc.strerror or exc}"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            f"adapter config {path} is not valid UTF-8"
        ) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"adapter config {path} is not valid JSON "
            f"(line {exc.lineno}: {exc.msg})"
        ) from None


def _cmd_run_preview(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    preview = execution_preview(
        aperio_dir_for(target), args.task_id,
        adapter_id=args.agent, config=_read_adapter_config(args.config),
        authorize_workspace_write=args.authorize_workspace_write,
    )
    print(json.dumps(preview, indent=2, ensure_ascii=False))


def _cmd_run_start(args) -> None:
    config = _read_adapter_config(args.config)
    target = resolve_target(args.project_directory, for_writing=True)
    record = dispatch_task(
        aperio_dir_for(target),
        args.task_id,
        adapter_id=args.agent,
        config=config,
        authorize_exec=args.authorize_exec,
        authorize_workspace_write=args.authorize_workspace_write,
        previous_run_id=args.previous_run,
        actor=args.actor,
    )
    if args.as_json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return
    if record.get("status") == "preview":
        print("read-only preparation (no --authorize-exec):")
        command = record.get("command", {})
        print(f"  task:      {record['task_id']}")
        print(f"  adapter:   {record['adapter_id']}")
        print(f"  executable: {command.get('executable')}")
        for arg in command.get("args", []):
            print(f"    arg: {arg}")
        print(f"  working_dir: {command.get('working_dir')}")
        print(f"  timeout: {command.get('timeout_seconds')}s; "
              f"env: {command.get('env_policy')}")
        permissions = command.get("effective_permissions")
        if permissions:
            print("  effective permissions: "
                  + json.dumps(permissions, sort_keys=True))
        if command.get("requested_workspace_write"):
            if args.authorize_workspace_write:
                print("  workspace-write is authorized for this preview")
            else:
                print("  requested workspace-write is NOT authorized; this "
                      "launch is read-only")
        for note in command.get("risk_notes") or []:
            print(f"  risk: {note}")
        print("  re-run with --authorize-exec to execute "
              "(add --authorize-write only for a write-capable provider mode)")
        return
    print(
        f"run {record['run_id']}: {record['status']} "
        f"(task {record['task_id']}, adapter {record['adapter_id']})"
    )
    if record.get("outcome"):
        print(f"  outcome: {record['outcome']}")
    if record.get("handoff_ref"):
        print(f"  handoff: {APERIO_DIR_NAME}/{record['handoff_ref']}")
    if record.get("local_log_ref"):
        print(f"  local log: {APERIO_DIR_NAME}/{record['local_log_ref']}")


def _cmd_run_status(args) -> None:
    aperio = aperio_dir_for(
        resolve_target(args.project_directory, for_writing=False)
    )
    document = read_executions_or_fail(aperio)
    for run in document["runs"]:
        if run["run_id"] == args.run_id:
            if args.as_json:
                print(json.dumps(run, indent=2, ensure_ascii=False))
            else:
                _print_run(run)
            return
    raise StateError(f"no run {args.run_id} in this project")


def _print_run(run: dict) -> None:
    print(f"Run:         {run['run_id']}")
    print(f"Status:      {run['status']}")
    print(f"Task:        {run['task_id']}")
    print(f"Adapter:     {run['adapter_id']}")
    print(f"Plan rev:    {run['plan_version']}")
    print(f"Started:     {run['started_at']}")
    print(f"Finished:    {run.get('finished_at') or '(not finished)'}")
    if run.get("outcome"):
        print(f"Outcome:     {run['outcome']}")
    if run.get("exit_code") is not None:
        print(f"Exit code:   {run['exit_code']}")
    if run.get("diagnostics_note"):
        print(f"Diagnostics: {run['diagnostics_note']}")
    if run.get("handoff_ref"):
        print(f"Handoff:     {APERIO_DIR_NAME}/{run['handoff_ref']}")
    if run.get("local_log_ref"):
        print(f"Local log:   {APERIO_DIR_NAME}/{run['local_log_ref']}")
    if run.get("previous_run_id"):
        print(f"Retry of:    {run['previous_run_id']}")


def _cmd_runs_list(args) -> None:
    aperio = aperio_dir_for(
        resolve_target(args.project_directory, for_writing=False)
    )
    document = read_executions_or_fail(aperio)
    runs = list(reversed(document["runs"]))  # newest first
    if args.as_json:
        print(json.dumps(runs, indent=2, ensure_ascii=False))
        return
    if not runs:
        print("No execution runs recorded.")
        return
    for run in runs:
        print(
            f"  {run['run_id']}  {run['status']:<17} {run['task_id']}  "
            f"{run['adapter_id']}"
        )


def _cmd_run_cancel(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    run = cancel_run(
        aperio_dir_for(target), args.run_id, reason=args.reason
    )
    print(f"run {run['run_id']} cancelled: {run['outcome']}")


def _cmd_runs_reconcile(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    report = reconcile_runs(aperio_dir_for(target))
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    for key in ("interrupted", "cancelled_stale", "awaiting_external_stale",
                "orphaned_tasks", "left_running", "foreign_machine",
                "plan_version_drift"):
        if report[key]:
            print(f"{key}: {report[key]}")
    if report["recovery_required"]:
        print("Recovery required: resolve interrupted runs/orphaned claims "
              "via task-status or run-report; no success was invented.")
    if not any(report[k] for k in report if k != "no_records"):
        print("No incomplete runs found.")


def _cmd_run_report(args) -> None:
    try:
        raw = Path(args.file).read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read run-report file {args.file}: {exc.strerror or exc}"
        ) from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValidationError(
            f"run-report file {args.file} is not valid UTF-8"
        ) from None
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"run-report file {args.file} is not valid JSON "
            f"(line {exc.lineno}: {exc.msg})"
        ) from None
    target = resolve_target(args.project_directory, for_writing=True)
    run = record_external_result(aperio_dir_for(target), args.run_id, result)
    print(
        f"run {run['run_id']}: external result recorded "
        f"({run['status']}). Task verification is still a separate, "
        "manual Feature-03 step."
    )


def _cmd_run_handoff(args) -> None:
    aperio = aperio_dir_for(
        resolve_target(args.project_directory, for_writing=False)
    )
    print(read_run_handoff(aperio, args.run_id), end="")


def _cmd_task_status(args) -> None:
    if args.file is not None:
        payload = read_json_fields(
            args.file,
            ("status",),
            optional_str_fields=(
                "owner",
                "reason",
                "evidence",
                "limitations",
                "result",
            ),
        )
        flag_values = [args.owner, args.reason, args.evidence, args.limitations, args.result]
        if any(value is not None for value in flag_values):
            raise ValidationError(
                "task-status: --file and the individual payload flags are "
                "mutually exclusive"
            )
        status = payload.pop("status")
        if args.status is not None and args.status != status:
            raise ValidationError(
                "task-status: --status and the payload file's 'status' disagree"
            )
        values = payload
    else:
        if args.status is None:
            raise ValidationError(
                "task-status: --status is required (or provide --file with "
                "a 'status' field)"
            )
        status = args.status
        values = {
            "owner": args.owner,
            "reason": args.reason,
            "evidence": args.evidence,
            "limitations": args.limitations,
            "result": args.result,
        }
    target = resolve_target(args.project_directory, for_writing=True)
    if args.reopen and status != "in-progress":
        raise ValidationError(
            "task-status: --reopen is only valid with --status in-progress "
            "(it deliberately reopens a completed task)"
        )
    if args.reopen:
        try:
            previous = read_plan(aperio_dir_for(target))
        except StateError:
            previous = None
        if previous is not None:
            prior = next(
                (t for t in previous["tasks"] if t["id"] == args.task_id), None
            )
            if prior is not None and prior["status"] == "completed":
                verification = prior["verification"]
                print(
                    "REOPEN: superseded verification of "
                    f"{args.task_id} - evidence: "
                    f"{verification.get('evidence')!r}; limitations: "
                    f"{verification.get('limitations')!r}; result: "
                    f"{verification.get('result')!r}; verified_at: "
                    f"{verification.get('verified_at')!r}. Record durable "
                    "history with 'aperio decision-add' if needed."
                )
    task = update_task(
        aperio_dir_for(target),
        args.task_id,
        new_status=status,
        owner=values.get("owner"),
        reason=values.get("reason"),
        evidence=values.get("evidence"),
        limitations=values.get("limitations"),
        result=values.get("result"),
        reopen=args.reopen,
        actor=args.actor,
    )
    verification = task["verification"]
    extra = ""
    if task["status"] == "completed":
        extra = f" (verified {verification['verified_at']})"
    elif verification["result"] == "failed":
        extra = " (failed verification recorded)"
    owner = f", owner: {task['owner']}" if task["owner"] else ""
    print(f"task {task['id']}: {task['status']}{owner}{extra}")


def _cmd_export(args) -> None:
    report = export_project(
        args.project_directory, output=args.output, dry_run=args.dry_run
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    if report["dry_run"]:
        print("export preview (dry run): validation complete, nothing written")
    else:
        print(
            f"exported {len(report['files'])} file(s) to {report['archive']}"
        )
    print(
        f"  format: {report['format']} v{report['format_version']}; "
        f"{report['total_bytes']} bytes"
    )
    for entry in report["files"]:
        print(f"  {entry['size']:>8}  {entry['sha256'][:16]}...  {entry['path']}")
    for warning in report["warnings"]:
        print(f"  warning: {warning}")
    print(
        "  note: hashes detect corruption only; they do not authenticate the "
        "archive and cannot prove that hand-written files contain no secrets. "
        "Review shareable text before sharing."
    )


def _cmd_import(args) -> None:
    report = import_archive(
        args.project_directory,
        args.archive,
        replace=args.replace,
        dry_run=args.dry_run,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    if report["dry_run"]:
        print(
            "import preview (dry run): the archive is fully validated; "
            "nothing written"
        )
    else:
        print(f"imported into {report['destination']}")
    print(f"  format: {report['format']} v{report['format_version']}")
    for marker, key in (
        ("create", "created"), ("replace", "replaced"),
        ("unchanged", "unchanged"),
    ):
        for rel in report[key]:
            print(f"  {marker:<9} {APERIO_DIR_NAME}/{rel}")
    for warning in report["warnings"]:
        print(f"  warning: {warning}")
    if report["dry_run"]:
        print("  re-run without --dry-run to apply")


def _cmd_integrity(args) -> int:
    report = check_integrity(
        args.project_directory, archive_path=args.archive
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if report["ok"]:
        print("integrity: OK")
    else:
        print(f"integrity: {len(report['errors'])} error(s)")
    for finding in report["errors"]:
        location = finding.get("path")
        where = f" [{location}]" if location else ""
        print(f"  error: {finding['message']}{where}")
    for finding in report["warnings"]:
        location = finding.get("path")
        where = f" [{location}]" if location else ""
        print(f"  warning: {finding['message']}{where}")
    schemas = report["schemas"]
    print(
        "  schemas: "
        + ", ".join(f"{name}={value}" for name, value in schemas.items())
    )
    if report.get("archive"):
        archive = report["archive"]
        print(
            f"  archive: OK ({archive['files']} file(s), "
            f"{archive['format']} v{archive['format_version']})"
        )
    return 0 if report["ok"] else 1


def _cmd_upgrade(args) -> int:
    report = (
        apply_upgrade(args.project_directory)
        if args.apply
        else plan_upgrade(args.project_directory)
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if report["applied"]:
        print(f"schema upgrade applied; backup at {report['backup']}")
    elif args.apply:
        print("schema upgrade: nothing to migrate; project unchanged")
    else:
        print("schema upgrade preview (dry run): nothing written")
    for entry in report["artifacts"]:
        version = entry["current_version"]
        supported = entry["supported_version"]
        print(
            f"  {entry['artifact']:<11} current={version!s:<6} "
            f"supported={supported!s:<6} {entry['action']:<8} "
            f"{entry['description']}"
        )
    for warning in report["warnings"]:
        print(f"  warning: {warning}")
    if not report["ok"]:
        print("  blocked: resolve the entries above; no automatic conversion exists")
        return 1
    if not args.apply:
        print("  re-run with --apply to migrate supported versions")
    return 0


def _cmd_roles(args) -> None:
    matrix = {
        role: {"actions": list(ROLE_ACTIONS[role])} for role in ROLES
    }
    if args.as_json:
        print(json.dumps(matrix, indent=2, ensure_ascii=False))
        return
    print("Built-in workflow roles (declared responsibilities, not providers):")
    for role in ROLES:
        print(f"  {role:12s} " + ", ".join(ROLE_ACTIONS[role]))
    print()
    print(
        "Role permissions are enforced from the configured actor mapping in "
        "policies.json; a caller-supplied role label is never trusted."
    )


def _parse_assignments(pairs: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for item in pairs:
        if ":" not in item:
            raise ValidationError(
                "--assign must look like ACTOR:ROLE "
                f"(got {item!r})"
            )
        actor, role = item.split(":", 1)
        if role not in ROLES:
            raise ValidationError(
                f"--assign role must be one of {', '.join(ROLES)}"
            )
        parsed.append((actor, role))
    return parsed


def _cmd_policy_init(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    policy = initialize_policy(
        aperio_dir_for(target),
        assignments=_parse_assignments(args.assign),
        by=args.by,
    )
    if args.as_json:
        print(json.dumps(policy, indent=2, ensure_ascii=False))
        return
    print(
        f"workflow governance enabled at {APERIO_DIR_NAME}/"
        f"{'policies.json'} (policy revision {policy['policy_version']})"
    )
    if not policy["actors"]:
        print("  (no actors assigned yet: use 'aperio policy-assign')")
    for actor, assignment in policy["actors"].items():
        print(f"  {actor} -> {assignment['role']}")
    print(
        "Declared actor ids are workflow declarations, not authenticated "
        "identity; enforce real identity at your process boundary."
    )


def _cmd_policy_show(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    policy = read_policy(aperio_dir)
    if policy is None:
        if args.as_json:
            print(json.dumps({"present": False, "enabled": False}, indent=2))
            return
        print(
            "No policy configured; this project is ungoverned (legacy "
            "behavior). Enable with 'aperio policy-init'."
        )
        return
    if args.as_json:
        print(json.dumps(policy, indent=2, ensure_ascii=False))
        return
    print(
        f"Policy:      revision {policy['policy_version']} "
        f"(schema {policy['schema_version']}, "
        f"{'enabled' if policy['enabled'] else 'disabled'})"
    )
    print(f"Updated:     {policy.get('updated_at')}")
    print("Actors:")
    if not policy["actors"]:
        print("  (none assigned)")
    for actor, assignment in sorted(policy["actors"].items()):
        print(f"  {actor} -> {assignment['role']}")
    print("Rules:")
    for key, value in sorted(policy.get("separation", {}).items()):
        print(f"  separation.{key}: {value}")
    for key, value in sorted(policy.get("review", {}).items()):
        print(f"  review.{key}: {value}")


def _cmd_policy_assign(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    policy = assign_actor(
        aperio_dir_for(target), args.actor, args.role, by=args.by
    )
    if args.as_json:
        print(json.dumps(policy, indent=2, ensure_ascii=False))
        return
    print(
        f"actor {args.actor} assigned to {args.role} "
        f"(policy revision {policy['policy_version']}); historical "
        "attribution is preserved"
    )


def _cmd_policy_permissions(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    policy = read_policy(aperio_dir)
    if policy is None:
        raise StateError(
            "no project policy is configured; run 'aperio policy-init' first"
        )
    actor = args.actor
    assignment = policy["actors"].get(actor)
    if assignment is None:
        raise StateError(
            f"actor {actor!r} is not assigned in the project policy; unknown "
            "actors are never given a guessed role"
        )
    role = assignment["role"]
    report = {
        "actor": actor,
        "role": role,
        "actions": list(policy["roles"][role]["actions"]),
        "separation": dict(policy.get("separation", {})),
        "review": dict(policy.get("review", {})),
        "declared_not_authenticated": True,
    }
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(f"Actor:       {actor} (declared, not authenticated)")
    print(f"Role:        {role}")
    print("Actions:     " + (", ".join(report["actions"]) or "(none)"))
    for key, value in sorted(report["separation"].items()):
        print(f"Rule:        separation.{key} = {value}")
    for key, value in sorted(report["review"].items()):
        print(f"Rule:        review.{key} = {value}")


def _cmd_review_submit(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    record = submit_review(
        aperio_dir_for(target),
        actor=args.actor,
        task_id=args.task,
        scope=args.scope,
        evidence=args.evidence,
        limitations=args.limitations,
        run_id=args.run,
    )
    if args.as_json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return
    print(
        f"review {record['review_id']} submitted for {record['target']['task_id']} "
        f"at plan revision {record['target']['plan_revision']} "
        f"(status: pending; independent reviewer required)"
    )


def _cmd_review_decide(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    record = decide_review(
        aperio_dir_for(target),
        args.review_id,
        actor=args.actor,
        outcome=args.outcome,
        findings=args.findings,
        limitations=args.limitations,
    )
    if args.as_json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return
    print(
        f"review {record['review_id']}: {record['outcome']} "
        f"by {record['reviewer']} at {record['decided_at']}"
    )
    if record.get("findings"):
        print(f"findings: {record['findings']}")


def _cmd_review_list(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    reviews = list_reviews(aperio_dir_for(target), task_id=args.task)
    if args.as_json:
        print(json.dumps(reviews, indent=2, ensure_ascii=False))
        return
    if not reviews:
        print("No reviews recorded.")
        return
    for review in reviews:
        target_label = review["target"].get("task_id") or "plan"
        print(
            f"{review['review_id']}  {target_label:10s} "
            f"rev {review['target']['plan_revision']:<3} "
            f"{review['outcome']:17s} by {review['submitted_by']}"
            + (f" -> {review['reviewer']}" if review["reviewer"] else "")
        )


def _cmd_review_status(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    status = review_status(aperio_dir)
    policy = policy_status(aperio_dir)
    from aperio.reviews import blocking_requirements

    report = {
        "governance_enabled": bool(policy.get("enabled")),
        "reviews": status,
        "blocking_requirements": blocking_requirements(aperio_dir),
    }
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(
        "Governance:  "
        + ("enabled" if report["governance_enabled"] else "not enabled")
    )
    if not status.get("present"):
        print("Reviews:     none recorded")
        return
    if status.get("unsupported"):
        print("Reviews:     unsupported schema (not interpreted)")
        return
    pending = status.get("pending", [])
    print(f"Pending:     {len(pending)}")
    for item in pending:
        task = item["target"].get("task_id") or "plan"
        print(f"  {item['review_id']}  {task}  (submitted by {item['submitted_by']})")
    blocking = report["blocking_requirements"]
    print("Blocking verified completion: " + (str(len(blocking)) if blocking else "none"))
    for item in blocking:
        print(
            f"  {item['task_id']} needs an approved independent review at plan "
            f"revision {item['plan_revision']}"
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            _print_init(init_project(args.project_directory))
        elif args.command == "status":
            _print_status(status_project(args.project_directory))
        elif args.command == "memory":
            _print_memory(load_context(args.project_directory), args.as_json)
        elif args.command == "context-pack":
            _cmd_context_pack(args)
        elif args.command == "profiles":
            _cmd_profiles(args)
        elif args.command == "goal-check":
            _cmd_goal_check(args)
        elif args.command == "goal-pack":
            _cmd_goal_pack(args)
        elif args.command == "decision-add":
            _cmd_decision_add(args)
        elif args.command == "handoff-write":
            _cmd_handoff_write(args)
        elif args.command == "phase-set":
            _cmd_phase_set(args)
        elif args.command == "plan-create":
            _cmd_plan_create(args)
        elif args.command == "plan-show":
            _cmd_plan_show(args)
        elif args.command == "plan-adopt":
            _cmd_plan_adopt(args)
        elif args.command == "plan-sync-authors":
            _cmd_plan_sync_authors(args)
        elif args.command == "plan-approve":
            _cmd_plan_approve(args)
        elif args.command == "plan-revise":
            _cmd_plan_revise(args)
        elif args.command == "task-list":
            _cmd_task_list(args)
        elif args.command == "task-status":
            _cmd_task_status(args)
        elif args.command == "agent-list":
            _cmd_agent_list(args)
        elif args.command == "agent-doctor":
            _cmd_agent_doctor(args)
        elif args.command == "run-preview":
            _cmd_run_preview(args)
        elif args.command == "run-start":
            _cmd_run_start(args)
        elif args.command == "run-status":
            _cmd_run_status(args)
        elif args.command == "runs-list":
            _cmd_runs_list(args)
        elif args.command == "run-cancel":
            _cmd_run_cancel(args)
        elif args.command == "runs-reconcile":
            _cmd_runs_reconcile(args)
        elif args.command == "run-report":
            _cmd_run_report(args)
        elif args.command == "run-handoff":
            _cmd_run_handoff(args)
        elif args.command == "export":
            _cmd_export(args)
        elif args.command == "import":
            _cmd_import(args)
        elif args.command == "integrity":
            return _cmd_integrity(args)
        elif args.command == "upgrade":
            return _cmd_upgrade(args)
        elif args.command == "roles":
            _cmd_roles(args)
        elif args.command == "policy-init":
            _cmd_policy_init(args)
        elif args.command == "policy-show":
            _cmd_policy_show(args)
        elif args.command == "policy-assign":
            _cmd_policy_assign(args)
        elif args.command == "policy-permissions":
            _cmd_policy_permissions(args)
        elif args.command == "review-submit":
            _cmd_review_submit(args)
        elif args.command == "review-decide":
            _cmd_review_decide(args)
        elif args.command == "review-list":
            _cmd_review_list(args)
        elif args.command == "review-status":
            _cmd_review_status(args)
        else:  # pragma: no cover - argparse enforces required subcommand
            parser.error(f"unknown command: {args.command!r}")
    except AperioError as exc:
        print(f"{_PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print(
            f"{_PROG}: error: interrupted; owned child processes were "
            "terminated and the run can be reconciled",
            file=sys.stderr,
        )
        return 130

    return 0
