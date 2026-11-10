"""Aperio command-line interface.

Commands:
    aperio --version                  print the version and exit
    aperio init <dir>                 initialize (or safely repair) a project
    aperio status <dir>               report project state (read-only)
    aperio memory <dir> [--json]      read portable project memory (Feature 02)
    aperio context-pack [dir] ...     build a read-only universal context pack
    aperio profiles [--json]          list built-in task profiles
    aperio goal-check <goal.json>     validate a goal and report completeness
    aperio mem-propose --store F ...  propose a universal memory record
    aperio mem-review STORE MEMORY_ID accept, verify, retract, supersede
    aperio mem-list STORE [--json]    list stored memories and review states
    aperio mem-show STORE MEMORY_ID   inspect one memory and its history
    aperio mem-recall STORE ...        retrieve memories for an objective
    aperio mem-export STORE [--output F]
                                      export selected memories portably
    aperio mem-import STORE FILE      import memories as proposals
    aperio goal-pack <goal.json> ...  build a context pack from a goal
    aperio preflight <goal.json> ...  mistake-prevention checks before work
    aperio quality-check <goal.json> --deliverable FILE ...
                                      evaluate a deliverable against the goal
    aperio feedback-check <feedback.json>
                                      validate a feedback proposal (M5)
    aperio feedback-review <feedback.json> --decision D --actor A
                                      record an explicit review decision
    aperio feedback-lesson <feedback.json> --store F
                                      store an accepted lesson as proposed memory
    aperio feedback-compare --before F --after F
                                      compare two quality reports (same goal)
    aperio improvement-apply --file P --target-file F --output NEW
                                      apply an improvement proposal to a copy
    aperio exchange-export [--goal F] [--context-pack F] ...
                                      build a universal portable exchange (M6)
    aperio exchange-import FILE --dest-dir DIR [--apply]
                                      preview a universal exchange import
                                      (add --apply to write)
    aperio agent-brief <goal.json> ...  assemble an agent-ready instruction
                                      package for pasting into any agent
    aperio benchmark-cases [--json]     list the built-in benchmark cases
    aperio benchmark-run [--cases DIR] [--only ID]
                                      run the cross-domain benchmark (M8)
    aperio capture DIR --message TEXT   Brain Dump Guard: capture a mid-task
                                      message (nothing executes)
    aperio inbox DIR [--json]           show pending captures and items
    aperio inbox-resolve DIR CAPTURE-ID ITEM --action A
                                      acknowledge or dismiss one item
    aperio checkpoint-write DIR --reason R
                                      derive and publish a durable checkpoint
    aperio checkpoint-show DIR [--json]
                                      show the current checkpoint
    aperio recover DIR [--json]         recovery inspection after a crash
    aperio plan-propose-from-goal GOAL DIR [--create]
                                      bridge a goal's work decomposition
                                      into a plan proposal (M-bridge)
    aperio orchestrate-assess DIR GOAL [--capability T:C ...]
                                      explainable single-vs-specialist view
    aperio orchestrate-plan DIR GOAL [--capability T:C ...]
                                      build the sequential delegation plan
    aperio orchestrate-brief DIR TASK-ID GOAL
                                      render one specialist's scoped brief
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
from aperio.memstore import (
    DEFAULT_SENSITIVITY as MEMORY_DEFAULT_SENSITIVITY,
    MAX_CONTENT_BYTES as MEMORY_MAX_CONTENT_BYTES,
    MEMORY_STATES,
    MEMORY_TYPES,
    SENSITIVITY_LEVELS as MEMORY_SENSITIVITY_LEVELS,
    SENSITIVITY_RANK as MEMORY_SENSITIVITY_RANK,
    SOURCE_KINDS as MEMORY_SOURCE_KINDS,
    MemoryStore,
)
from aperio.memrecall import (
    RECALL_LIMITATION,
    memory_context_items,
    recall_memories,
)
from aperio.quality import (
    QUALITY_REPORT_FORMAT,
    QUALITY_REPORT_VERSION,
    load_quality_request_file,
    render_quality_report_markdown,
    run_preflight,
    run_quality_check,
    validate_quality_request,
)
from aperio.feedback import (
    COMPARISON_FORMAT,
    apply_proposal_to_goal,
    apply_proposal_to_request,
    compare_quality_reports,
    feedback_identity,
    feedback_issues,
    feedback_revision,
    feedback_trust,
    lesson_memory_record,
    load_feedback_file,
    load_proposal_file,
    record_review,
    render_comparison_markdown,
    validate_feedback,
    validate_improvement_proposal,
)
from aperio.exchange import (
    EXCHANGE_FORMAT,
    EXCHANGE_FORMAT_VERSION,
    apply_import,
    build_exchange,
    context_pack_section_from_file,
    feedback_section_from_file,
    goal_section_from_file,
    load_exchange_file as load_universal_exchange_file,
    memory_section,
    notes_section,
    plan_import,
    quality_report_section_from_file,
    render_import_report_markdown,
)
from aperio.guidance import (
    build_agent_brief,
    render_agent_brief_markdown,
)
from aperio.intake import (
    confirmation_line,
    capture as capture_message,
    resolve as resolve_intake_item,
    IntakeLog,
)
from aperio.checkpoint import (
    read_checkpoint,
    recover_report,
    refresh_checkpoint,
    render_recovery_markdown,
)
from aperio.planbridge import (
    PROPOSAL_FORMAT,
    load_proposal_file as load_plan_proposal_file,
    plan_proposal_from_goal,
    render_proposal_markdown,
)
from aperio.orchestrate import (
    OrchestrationStore,
    assess_orchestration,
    build_orchestration_plan,
    render_specialist_brief_markdown,
    specialist_brief,
)
from aperio.planmodel import (
    PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_READ_VERSIONS,
)
from aperio.benchmark import (
    builtin_cases,
    load_case_file,
    render_benchmark_markdown,
    run_benchmark,
    validate_case,
)
from aperio.memportable import (
    export_memories,
    import_memories,
    load_exchange_file,
    render_export,
    summarize_import,
)
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
from aperio.planmodel import priority_rank
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
from aperio.onboarding_discovery import discover_workspace as _ob_discover_workspace
from aperio.onboarding import (
    INTENT_CHOICES as _OB_INTENT_CHOICES,
    INIT_ACTION as _OB_INIT_ACTION,
    answer as _ob_answer,
    cancel as _ob_cancel,
    confirm_initialization as _ob_confirm_initialization,
    decline_initialization as _ob_decline_initialization,
    pause as _ob_pause,
    record_intent as _ob_record_intent,
    revise as _ob_revise,
    start as _ob_start,
    store_reconstruction as _ob_store_reconstruction,
)
from aperio.onboarding_interview import interview_progress as _ob_interview_progress
from aperio.onboarding_readiness import (
    build_report as _ob_build_report,
    evaluate_readiness as _ob_evaluate_readiness,
    render_report_markdown as _ob_render_report_markdown,
)
from aperio.onboarding_reconstruct import reconstruct_workspace as _ob_reconstruct_workspace
from aperio.onboarding_session import (
    read_session as _ob_read_session,
    record_approval as _ob_record_approval,
    session_exists as _ob_session_exists,
)
from aperio.onboarding_standardize import (
    APPLY_ACTION as _OB_APPLY_ACTION,
    apply_standardization as _ob_apply_standardization,
    load_proposal as _ob_load_proposal,
    propose_standardization as _ob_propose_standardization,
    render_proposal_markdown as _ob_render_proposal_markdown,
    save_proposal as _ob_save_proposal,
    verify_standardization as _ob_verify_standardization,
)
from aperio.onboarding_tools import (
    INSTALL_ACTION as _OB_INSTALL_ACTION,
    build_install_plan as _ob_build_install_plan,
    discover_tools as _ob_discover_tools,
    execute_install as _ob_execute_install,
    probe_tool as _ob_probe_tool,
    recommend_tools as _ob_recommend_tools,
)
from aperio.project import require_supported_state as _ob_require_supported_state
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


def _add_memory_store_options(parser) -> None:
    parser.add_argument(
        "--memory-store", metavar="FILE",
        help="optional universal memory store to retrieve relevant memories "
             "from (read-only)",
    )
    parser.add_argument(
        "--memory-tag", action="append", default=[], metavar="TAG",
        help="only retrieve memories carrying this tag; repeatable",
    )
    parser.add_argument(
        "--memory-workspace", metavar="LABEL",
        help="workspace label that workspace-scoped memories must match",
    )
    parser.add_argument(
        "--memory-max-sensitivity",
        choices=MEMORY_SENSITIVITY_LEVELS,
        default=MEMORY_DEFAULT_SENSITIVITY,
        help=(
            "highest memory sensitivity to consider (default: internal). "
            "private memory is never written into a pack, because packs are "
            "shareable artifacts; raise this only to make the exclusion "
            "explicit"
        ),
    )
    parser.add_argument(
        "--memory-as-of", metavar="YYYY-MM-DD",
        help="evaluate memory expiry as of this date",
    )


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
    _add_memory_store_options(context_parser)
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
    _add_memory_store_options(goal_pack_parser)
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

    # --- universal mistake prevention and quality evaluation (M4) --------
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="run mistake-prevention checks on a goal before work begins",
        description=(
            "Inspect a validated goal for what could go wrong before any "
            "work starts: unclear objectives, missing expectations, "
            "conflicting or duplicated instructions, untrusted "
            "instructions, unvalidated assumptions, and — with "
            "--memory-store — expired, unreviewed, or conflicting memory. "
            "Deterministic and read-only: it never executes content, "
            "writes memory, or alters the goal."
        ),
    )
    preflight_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    _add_memory_store_options(preflight_parser)
    preflight_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the quality report as JSON",
    )
    preflight_parser.add_argument(
        "--output", metavar="FILE",
        help="write the report to a file instead of stdout",
    )
    preflight_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    quality_check_parser = subparsers.add_parser(
        "quality-check",
        help="evaluate a candidate deliverable against a goal",
        description=(
            "Check a candidate deliverable against what the goal "
            "explicitly, objectively requires: operator-named sections and "
            "texts, modal constraint phrases ('must include X', 'must not "
            "contain Y'), stated length bounds, output-format "
            "expectations, deliverable coverage, and source attribution "
            "when the goal demands evidence. Findings are labelled "
            "confirmed, warning, unknown, or recommendation — the tool "
            "never claims to judge truth or taste. Read-only."
        ),
    )
    quality_check_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    quality_check_parser.add_argument(
        "--deliverable", required=True, metavar="FILE",
        help="path to the candidate deliverable (UTF-8 text, at most 1 MiB)",
    )
    quality_check_parser.add_argument(
        "--request", metavar="FILE",
        help="optional quality-request JSON file (request-schema v1) with "
             "required_sections/required_texts/forbidden_texts/word bounds",
    )
    quality_check_parser.add_argument(
        "--require-section", action="append", default=[], metavar="NAME",
        help="a section/heading the deliverable must contain; repeatable",
    )
    quality_check_parser.add_argument(
        "--require-text", action="append", default=[], metavar="TEXT",
        help="text the deliverable must contain verbatim; repeatable",
    )
    quality_check_parser.add_argument(
        "--forbid-text", action="append", default=[], metavar="TEXT",
        help="text the deliverable must not contain; repeatable",
    )
    quality_check_parser.add_argument(
        "--min-words", type=int, metavar="N",
        help="minimum word count for the deliverable",
    )
    quality_check_parser.add_argument(
        "--max-words", type=int, metavar="N",
        help="maximum word count for the deliverable",
    )
    quality_check_parser.add_argument(
        "--no-output-contract", action="store_true",
        help="ignore the goal's output_contract when deriving checks",
    )
    quality_check_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the quality report as JSON",
    )
    quality_check_parser.add_argument(
        "--output", metavar="FILE",
        help="write the report to a file instead of stdout",
    )
    quality_check_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    # --- controlled feedback and improvement (M5) ------------------------
    feedback_check_parser = subparsers.add_parser(
        "feedback-check",
        help="validate a feedback document and report its trust and warnings",
        description=(
            "Validate a feedback document (feedback-schema v1) and report "
            "its identity, origin, review state, descriptive trust, and "
            "structural warnings (untrusted origin, self-reported "
            "evaluation, missing review, missing lesson). Read-only."
        ),
    )
    feedback_check_parser.add_argument(
        "feedback_file", metavar="FEEDBACK_FILE",
        help="path to a feedback JSON file",
    )
    feedback_check_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the validation report as JSON",
    )

    feedback_review_parser = subparsers.add_parser(
        "feedback-review",
        help="record an explicit accepted/rejected decision on feedback",
        description=(
            "Record one explicit review decision on a feedback document. "
            "The result is written to --output, or back to the input file "
            "with --replace plus the expected revision (a stale writer "
            "conflicts instead of clobbering). A decided document is "
            "final; a change of mind is a new feedback document."
        ),
    )
    feedback_review_parser.add_argument(
        "feedback_file", metavar="FEEDBACK_FILE",
        help="path to a feedback JSON file (proposed state)",
    )
    feedback_review_parser.add_argument(
        "--decision", required=True, choices=("accepted", "rejected"),
        help="the review decision",
    )
    feedback_review_parser.add_argument(
        "--actor", required=True, metavar="ID",
        help="declared reviewer identifier (attribution, not authentication)",
    )
    feedback_review_parser.add_argument(
        "--note", metavar="TEXT", help="optional review note",
    )
    feedback_review_parser.add_argument(
        "--at", metavar="ISO_UTC",
        help="review timestamp (default: now, UTC)",
    )
    feedback_review_parser.add_argument(
        "--output", metavar="FILE",
        help="write the reviewed document here instead of stdout",
    )
    feedback_review_parser.add_argument(
        "--replace", action="store_true",
        help="replace the input file (requires --expected-revision)",
    )
    feedback_review_parser.add_argument(
        "--expected-revision", type=int, metavar="N",
        help="the revision the caller read (CAS for --replace)",
    )
    feedback_review_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    feedback_lesson_parser = subparsers.add_parser(
        "feedback-lesson",
        help="store an accepted feedback's lesson as a proposed memory",
        description=(
            "Write the lesson of an accepted feedback document into the "
            "operator-named M3 memory store as a NEW PROPOSED memory. The "
            "ordinary memory review contract ('aperio mem-review accept') "
            "remains the only way the lesson gains authority; nothing is "
            "promoted here."
        ),
    )
    feedback_lesson_parser.add_argument(
        "feedback_file", metavar="FEEDBACK_FILE",
        help="path to an accepted feedback JSON file",
    )
    feedback_lesson_parser.add_argument(
        "--store", required=True, metavar="FILE",
        help="operator-named memory store file",
    )
    feedback_lesson_parser.add_argument(
        "--actor", metavar="ID", help="declared proposer identifier",
    )

    feedback_compare_parser = subparsers.add_parser(
        "feedback-compare",
        help="compare two quality reports for the same goal",
        description=(
            "Deterministic before/after comparison of two aperio-quality-"
            "report documents for the SAME goal: resolved findings, new "
            "findings (potential regressions, always reported), and "
            "unchanged ones. The comparison reports what changed; it never "
            "claims what caused it."
        ),
    )
    feedback_compare_parser.add_argument(
        "--before", required=True, metavar="FILE",
        help="quality report before the change",
    )
    feedback_compare_parser.add_argument(
        "--after", required=True, metavar="FILE",
        help="quality report after the change",
    )
    feedback_compare_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the comparison as JSON",
    )
    feedback_compare_parser.add_argument(
        "--output", metavar="FILE", help="write the comparison to a file",
    )
    feedback_compare_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    improvement_parser = subparsers.add_parser(
        "improvement-apply",
        help="apply an improvement proposal to a NEW copy of a goal or request",
        description=(
            "Apply a validated improvement proposal (improvement-proposal "
            "v1) to a COPY of its target file. The original is never "
            "modified: the result is written to --output through the "
            "shared atomic publication path, so the change is reviewable "
            "and reversible. Proposals cannot target the profile registry "
            "(code-owned data)."
        ),
    )
    improvement_parser.add_argument(
        "--file", required=True, metavar="PROPOSAL",
        help="path to the improvement proposal JSON file",
    )
    improvement_parser.add_argument(
        "--target-file", required=True, metavar="FILE",
        help="the current goal or quality-request file",
    )
    improvement_parser.add_argument(
        "--output", required=True, metavar="FILE",
        help="where to write the updated copy (must not exist without --force)",
    )
    improvement_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )
    improvement_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the applied document as JSON instead of a summary",
    )

    # --- universal portable exchange (M6) --------------------------------
    exchange_export_parser = subparsers.add_parser(
        "exchange-export",
        help="build one portable universal exchange document",
        description=(
            "Assemble a selected set of work material — a goal, its "
            "context pack, a quality report, feedback documents, selected "
            "universal memories, and operator notes — into one "
            "provider-neutral JSON document (aperio-universal-exchange "
            "v1) that another agent or machine can import without losing "
            "provenance. Every section is validated against its own "
            "contract; memories keep the M3 exchange rules. Read-only "
            "except for the --output file."
        ),
    )
    exchange_export_parser.add_argument(
        "--goal", metavar="FILE",
        help="include this goal document (goal-schema v1)",
    )
    exchange_export_parser.add_argument(
        "--context-pack", metavar="FILE",
        help="include this context pack JSON (from 'goal-pack --json')",
    )
    exchange_export_parser.add_argument(
        "--quality-report", metavar="FILE",
        help="include this quality report JSON",
    )
    exchange_export_parser.add_argument(
        "--feedback", action="append", default=[], metavar="FILE",
        help="include this feedback document; repeatable",
    )
    exchange_export_parser.add_argument(
        "--memory-store", metavar="FILE",
        help="include memories from this store (selection below)",
    )
    exchange_export_parser.add_argument(
        "--memory-id", action="append", default=[], metavar="MEM-ID",
        help="export this memory id; repeatable",
    )
    exchange_export_parser.add_argument(
        "--memory-tag", action="append", default=[], metavar="TAG",
        help="export memories carrying this tag; repeatable",
    )
    exchange_export_parser.add_argument(
        "--memory-as-of", metavar="YYYY-MM-DD",
        help="evaluate memory validity against this date",
    )
    exchange_export_parser.add_argument(
        "--include-private", action="store_true",
        help="allow private memories into the exchange (deliberate)",
    )
    exchange_export_parser.add_argument(
        "--note", action="append", default=[], metavar="TEXT",
        help="operator note: an outstanding uncertainty or instruction "
             "for the receiving agent; repeatable",
    )
    exchange_export_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the exchange document as JSON on stdout",
    )
    exchange_export_parser.add_argument(
        "--output", metavar="FILE", help="write the exchange document here",
    )
    exchange_export_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    exchange_import_parser = subparsers.add_parser(
        "exchange-import",
        help="preview or apply a universal exchange import",
        description=(
            "Import an aperio-universal-exchange document: imported files "
            "are written as 'imported-*.json' into the destination "
            "directory (existing files are never overwritten), and memory "
            "sections enter the destination store as PROPOSED — nothing "
            "is promoted, nothing executes. Preview is the default; pass "
            "--apply to write."
        ),
    )
    exchange_import_parser.add_argument(
        "exchange_file", metavar="EXCHANGE_FILE",
        help="path to the exchange JSON document",
    )
    exchange_import_parser.add_argument(
        "--dest-dir", required=True, metavar="DIR",
        help="directory that receives imported files (must exist)",
    )
    exchange_import_parser.add_argument(
        "--memory-store", metavar="FILE",
        help="destination memory store for memory sections (required to "
             "import them)",
    )
    exchange_import_parser.add_argument(
        "--max-sensitivity",
        choices=MEMORY_SENSITIVITY_LEVELS, default=MEMORY_DEFAULT_SENSITIVITY,
        help="highest memory sensitivity to import (default: internal)",
    )
    exchange_import_parser.add_argument(
        "--apply", action="store_true",
        help="write the import (default: preview only)",
    )
    exchange_import_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the import report as JSON",
    )

    # --- universal agent guidance (M7) ------------------------------------
    brief_parser = subparsers.add_parser(
        "agent-brief",
        help="assemble an agent-ready instruction package",
        description=(
            "Assemble a concise, paste-ready instruction package for any "
            "text agent: the validated objective, explicit requirements "
            "and constraints, deliverables and acceptance criteria, the "
            "output contract, quality criteria plus advisory profile "
            "self-checks, reviewed memory and accepted lessons (labelled, "
            "never overriding the brief), known uncertainties, and the "
            "trust boundaries. Only an operator-declared goal can become a "
            "brief. Read-only; no provider is contacted — pasting is the "
            "transport."
        ),
    )
    brief_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    brief_parser.add_argument(
        "--task-reference", metavar="TEXT",
        help="optional task label to carry into the brief",
    )
    _add_memory_store_options(brief_parser)
    brief_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the brief as JSON",
    )
    brief_parser.add_argument(
        "--output", metavar="FILE",
        help="write the brief to a file instead of stdout",
    )
    brief_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    # --- cross-domain benchmark (M8) --------------------------------------
    bench_cases_parser = subparsers.add_parser(
        "benchmark-cases",
        help="list the built-in benchmark cases (benchmark-case v1)",
        description=(
            "List the versioned evaluation cases shipped with Aperio: "
            "planted, objectively checkable flaws per domain, with "
            "expected detections, false-positive guards, trust "
            "attempts, transfer expectations, and the metrics that "
            "deliberately require a human evaluator."
        ),
    )
    bench_cases_parser.add_argument(
        "--cases", metavar="DIR",
        help="load cases from this directory instead of the built-ins",
    )
    bench_cases_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the case list as JSON",
    )

    bench_run_parser = subparsers.add_parser(
        "benchmark-run",
        help="run the cross-domain benchmark and report detections",
        description=(
            "Run the versioned benchmark cases offline (no provider, no "
            "network): detection of planted flaws per domain, "
            "trust-boundary enforcement, cross-agent information "
            "preservation, memory correctness, missing-information "
            "handling, and measured overhead. The report states plainly "
            "what was measured and what only a human evaluator can judge; "
            "no aggregate quality score is produced."
        ),
    )
    bench_run_parser.add_argument(
        "--cases", metavar="DIR",
        help="run cases from this directory instead of the built-ins",
    )
    bench_run_parser.add_argument(
        "--only", action="append", default=[], metavar="CASE_ID",
        help="run only this case; repeatable",
    )
    bench_run_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the benchmark report as JSON",
    )
    bench_run_parser.add_argument(
        "--output", metavar="FILE", help="write the report to a file",
    )
    bench_run_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    # --- Brain Dump Guard: durable intake ---------------------------------
    capture_parser = subparsers.add_parser(
        "capture",
        help="capture a mid-task brain-dump message (nothing executes)",
        description=(
            "Brain Dump Guard runtime path: preserve the raw message, "
            "classify its items with a deterministic keyword heuristic "
            "(labeled as such, never claimed as language "
            "understanding), and route them — related items noted for a "
            "checkpoint, unrelated items queued, ideas kept as ideas. "
            "Nothing captured executes and the active task is never "
            "changed. Urgency authority comes ONLY from --urgent passed "
            "by you; urgency words inside the text are detected and "
            "reported without authority."
        ),
    )
    capture_parser.add_argument(
        "project", metavar="DIR",
        help="initialized Aperio project directory",
    )
    capture_parser.add_argument(
        "--message", metavar="TEXT",
        help="the operator's raw message (max 16 KiB)",
    )
    capture_parser.add_argument(
        "--file", metavar="F",
        help="read the message from a UTF-8 file instead",
    )
    capture_parser.add_argument(
        "--urgent", action="store_true",
        help="operator-requested urgency: checkpoint the current task "
             "and mark the capture for immediate attention (the trusted "
             "operator channel — text content can never set this)",
    )
    capture_parser.add_argument(
        "--actor", metavar="ID", help="declared operator identifier",
    )

    inbox_parser = subparsers.add_parser(
        "inbox",
        help="show captured brain-dump items and their routing",
        description="Replayed intake-log state (read-only).",
    )
    inbox_parser.add_argument(
        "project", metavar="DIR", help="initialized Aperio project directory",
    )
    inbox_parser.add_argument(
        "--include-resolved", action="store_true",
        help="also show acknowledged/dismissed items",
    )
    inbox_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the replayed intake state as JSON",
    )

    inbox_resolve_parser = subparsers.add_parser(
        "inbox-resolve",
        help="acknowledge or dismiss one captured item (append-only)",
        description=(
            "Record an explicit acknowledge/dismiss decision on one "
            "captured item. Intake history is append-only: a resolved "
            "item cannot be re-resolved. Promotion into executable work "
            "is a separate reviewed operation, not a resolve action."
        ),
    )
    inbox_resolve_parser.add_argument(
        "project", metavar="DIR", help="initialized Aperio project directory",
    )
    inbox_resolve_parser.add_argument(
        "capture_id", metavar="CAPTURE-ID", help="e.g. INT-0001",
    )
    inbox_resolve_parser.add_argument(
        "item_index", metavar="ITEM", type=int,
        help="zero-based item index within the capture",
    )
    inbox_resolve_parser.add_argument(
        "--action", required=True, choices=("acknowledge", "dismiss"),
        help="the resolve decision",
    )
    inbox_resolve_parser.add_argument(
        "--actor", required=True, metavar="ID",
        help="declared operator identifier",
    )
    inbox_resolve_parser.add_argument(
        "--note", metavar="TEXT", help="optional note",
    )

    # --- durable task continuity: checkpoints -----------------------------
    checkpoint_write_parser = subparsers.add_parser(
        "checkpoint-write",
        help="derive and publish a durable checkpoint from current state",
        description=(
            "Derive a checkpoint from durable state (plan, executions, "
            "intake log) and publish it through a byte compare-and-swap. "
            "Called automatically at meaningful transitions; call it "
            "explicitly before pausing work and after any recovery "
            "review."
        ),
    )
    checkpoint_write_parser.add_argument(
        "project", metavar="DIR", help="initialized Aperio project directory",
    )
    checkpoint_write_parser.add_argument(
        "--reason", required=True, metavar="TEXT",
        help="why this checkpoint was written (e.g. 'before pausing')",
    )

    checkpoint_show_parser = subparsers.add_parser(
        "checkpoint-show",
        help="show the current durable checkpoint (read-only)",
    )
    checkpoint_show_parser.add_argument(
        "project", metavar="DIR", help="initialized Aperio project directory",
    )
    checkpoint_show_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the checkpoint document as JSON",
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help="recovery inspection: checkpoint vs current durable state",
        description=(
            "After an interruption or crash: validate the checkpoint, "
            "compare it against current durable state, report drift, "
            "separate verified completions from unverified agent claims, "
            "and state resume eligibility with reasons. Read-only: "
            "recovery never mutates anything and never invents "
            "completion."
        ),
    )
    recover_parser.add_argument(
        "project", metavar="DIR", help="initialized Aperio project directory",
    )
    recover_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the recovery report as JSON",
    )

    # --- goal-to-plan bridge ----------------------------------------------
    propose_parser = subparsers.add_parser(
        "plan-propose-from-goal",
        help="bridge a goal's work decomposition into a plan proposal",
        description=(
            "Convert a validated operator goal into a plan proposal in "
            "the plan-schema input shape. A proposal is NOT a plan: "
            "creating the plan (with --create) produces a DRAFT that "
            "still requires explicit approval before any execution. "
            "Only operator-declared goals can be bridged; work-task ids "
            "are re-numbered TASK-001… in declared order with the "
            "mapping recorded; missing goal fields are listed as "
            "missing, never invented."
        ),
    )
    propose_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="path to a goal JSON file",
    )
    propose_parser.add_argument(
        "project", metavar="DIR",
        help="initialized Aperio project directory",
    )
    propose_parser.add_argument(
        "--create", action="store_true",
        help="create the DRAFT plan from the proposal via the ordinary "
             "gated plan-create (approval still required)",
    )
    propose_parser.add_argument(
        "--actor", metavar="ID", help="declared operator identifier",
    )
    propose_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the proposal as JSON",
    )
    propose_parser.add_argument(
        "--output", metavar="FILE",
        help="write the proposal document here",
    )
    propose_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    # --- controlled multi-agent orchestration ------------------------------
    def _add_orchestrate_args(parser) -> None:
        parser.add_argument(
            "project", metavar="DIR",
            help="initialized Aperio project directory",
        )
        parser.add_argument(
            "goal_file", metavar="GOAL_FILE",
            help="path to the operator goal JSON file",
        )
        parser.add_argument(
            "--capability", action="append", default=[], metavar="TASK:CAP",
            help="declare a task's required capability "
                 "(e.g. TASK-002:coding); repeatable, operator-declared "
                 "input — never inferred",
        )
        parser.add_argument(
            "--actor", metavar="ID", help="declared operator identifier",
        )

    assess_parser = subparsers.add_parser(
        "orchestrate-assess",
        help="explainable single-agent vs specialist assessment",
        description=(
            "Assess whether the plan's tasks belong to one capable agent "
            "or to sequential specialist delegation, with every criterion "
            "and reason listed. Parallel execution is not offered: "
            "non-overlapping write scopes cannot be proven for the "
            "existing adapters. The assessment is advice, never "
            "authorization."
        ),
    )
    _add_orchestrate_args(assess_parser)
    assess_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the assessment as JSON",
    )

    orch_plan_parser = subparsers.add_parser(
        "orchestrate-plan",
        help="build the sequential specialist delegation plan",
        description=(
            "Map each plan task to a specialist adapter (from the "
            "existing adapter registry) with scoped per-task context. "
            "Dispatch still happens through the ordinary run-start / "
            "scheduler gates; an implementer cannot approve its own "
            "work. Parallel execution is not offered."
        ),
    )
    _add_orchestrate_args(orch_plan_parser)
    orch_plan_parser.add_argument(
        "--adapter", action="append", default=[], metavar="TASK:ADAPTER",
        help="override the specialist adapter for one task; repeatable",
    )
    orch_plan_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the orchestration document as JSON",
    )

    orch_brief_parser = subparsers.add_parser(
        "orchestrate-brief",
        help="render one specialist's scoped task brief",
        description=(
            "Render the brief ONE specialist receives: its own task's "
            "contract plus the operator goal's authoritative sections, "
            "with other tasks present only as dependency context — never "
            "their deliverables. Quoted context under the trust "
            "boundary; it grants no authority."
        ),
    )
    orch_brief_parser.add_argument(
        "project", metavar="DIR",
        help="initialized Aperio project directory",
    )
    orch_brief_parser.add_argument(
        "task_id", metavar="TASK-ID", help="the delegated task, e.g. TASK-002",
    )
    orch_brief_parser.add_argument(
        "goal_file", metavar="GOAL_FILE", help="the operator goal JSON file",
    )
    orch_brief_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit the brief as JSON",
    )

    # --- universal memory (M3) -------------------------------------------
    mem_propose_parser = subparsers.add_parser(
        "mem-propose",
        help="propose a memory record in an explicit memory store",
        description=(
            "Add one memory to an operator-chosen memory store. The record "
            "starts as 'proposed': nothing is trusted, verified, or promoted "
            "by being stored. Works with no Aperio project."
        ),
    )
    mem_propose_parser.add_argument(
        "--store", required=True, metavar="FILE",
        help="explicit memory store file (created if absent)",
    )
    mem_propose_parser.add_argument(
        "--type", required=True, choices=MEMORY_TYPES, dest="memory_type",
        help="memory type",
    )
    mem_propose_parser.add_argument(
        "--title", required=True, help="short single-line title",
    )
    content_group = mem_propose_parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="memory content text")
    content_group.add_argument(
        "--content-file", metavar="FILE",
        help=f"read memory content from a UTF-8 file (max {MEMORY_MAX_CONTENT_BYTES} bytes)",
    )
    mem_propose_parser.add_argument(
        "--scope", choices=("global", "profile", "objective", "workspace"),
        default="global",
        help="scope kind the memory applies to (default: global)",
    )
    mem_propose_parser.add_argument(
        "--scope-value", metavar="TEXT",
        help="objective text or workspace label for objective/workspace scope",
    )
    mem_propose_parser.add_argument(
        "--scope-profile", metavar="PROFILE",
        help="profile id for profile scope",
    )
    mem_propose_parser.add_argument(
        "--tag", action="append", default=[], metavar="TAG",
        help="tag for retrieval; repeatable",
    )
    mem_propose_parser.add_argument(
        "--sensitivity", choices=MEMORY_SENSITIVITY_LEVELS,
        default=MEMORY_DEFAULT_SENSITIVITY,
        help="sensitivity classification (default: internal)",
    )
    mem_propose_parser.add_argument(
        "--evidence", action="append", default=[], metavar="TEXT",
        help="supporting evidence reference; repeatable",
    )
    mem_propose_parser.add_argument(
        "--source", choices=MEMORY_SOURCE_KINDS, default="operator",
        help="declared source kind (default: operator)",
    )
    mem_propose_parser.add_argument(
        "--source-ref", metavar="TEXT", help="declared source reference",
    )
    mem_propose_parser.add_argument(
        "--valid-from", metavar="YYYY-MM-DD",
        help="date from which the memory is current (default: immediately)",
    )
    mem_propose_parser.add_argument(
        "--valid-until", metavar="YYYY-MM-DD",
        help="last date the memory is current (inclusive)",
    )
    mem_propose_parser.add_argument(
        "--supersedes", metavar="MEMORY_ID",
        help="memory this record is meant to replace (reviewed separately)",
    )
    mem_propose_parser.add_argument(
        "--actor", metavar="ID", help="declared actor id (not authentication)",
    )
    mem_propose_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
    )

    mem_review_parser = subparsers.add_parser(
        "mem-review",
        help="accept, verify, retract, supersede, or reinstate a memory",
        description=(
            "Record an explicit review decision. Acceptance makes a memory "
            "authoritative for its scope; it does not prove the claim. "
            "Verification requires at least one evidence reference and a "
            "declared reviewer identifier, and refuses a reviewer identical to "
            "the memory's declared proposer. Identifiers are attribution, not "
            "authentication: Aperio cannot prove who ran the review."
        ),
    )
    mem_review_parser.add_argument("store", metavar="STORE")
    mem_review_parser.add_argument("memory_id", metavar="MEMORY_ID")
    review_action = mem_review_parser.add_mutually_exclusive_group(required=True)
    review_action.add_argument(
        "--accept", dest="action", action="store_const", const="accept",
        help="accept as authoritative for its scope (attested)",
    )
    review_action.add_argument(
        "--verify", dest="action", action="store_const", const="verify",
        help=(
            "record supporting evidence (verified); requires --evidence and "
            "--actor, and the reviewer must differ from the declared proposer"
        ),
    )
    review_action.add_argument(
        "--retract", dest="action", action="store_const", const="retract",
        help="retract a memory that is wrong or no longer applicable",
    )
    review_action.add_argument(
        "--supersede", dest="action", action="store_const", const="supersede",
        help="mark superseded by a newer memory",
    )
    review_action.add_argument(
        "--reinstate", dest="action", action="store_const", const="reinstate",
        help="return a retracted/superseded memory to proposed for re-review",
    )
    mem_review_parser.add_argument(
        "--note", help="review note recorded with the transition",
    )
    mem_review_parser.add_argument(
        "--evidence", action="append", default=[], metavar="TEXT",
        help="evidence supporting this decision; repeatable",
    )
    mem_review_parser.add_argument(
        "--reason", help="reason for retraction or supersession",
    )
    mem_review_parser.add_argument(
        "--actor", metavar="ID", help="declared actor id (not authentication)",
    )
    mem_review_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
    )

    mem_list_parser = subparsers.add_parser(
        "mem-list",
        help="list memories in a store with their review state",
    )
    mem_list_parser.add_argument("store", metavar="STORE")
    mem_list_parser.add_argument(
        "--state", action="append", default=[], choices=MEMORY_STATES,
        help="only show these states; repeatable",
    )
    mem_list_parser.add_argument(
        "--include-inactive", action="store_true",
        help="also list retracted, superseded, and expired memories",
    )
    mem_list_parser.add_argument(
        "--as-of", metavar="YYYY-MM-DD",
        help="evaluate validity as of this date (default: today)",
    )
    mem_list_parser.add_argument(
        "--max-sensitivity", choices=MEMORY_SENSITIVITY_LEVELS,
        default="private",
        help="highest sensitivity to display (default: private)",
    )
    mem_list_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
    )

    mem_show_parser = subparsers.add_parser(
        "mem-show",
        help="show one memory, its provenance, and its review history",
    )
    mem_show_parser.add_argument("store", metavar="STORE")
    mem_show_parser.add_argument("memory_id", metavar="MEMORY_ID")
    mem_show_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
    )

    mem_recall_parser = subparsers.add_parser(
        "mem-recall",
        help="retrieve memories relevant to an objective (read-only)",
    )
    mem_recall_parser.add_argument("store", metavar="STORE")
    recall_group = mem_recall_parser.add_mutually_exclusive_group()
    recall_group.add_argument("--objective", help="current objective text")
    recall_group.add_argument(
        "--goal", metavar="GOAL_FILE",
        help="read the objective and profile from a goal document",
    )
    mem_recall_parser.add_argument(
        "--profile", default=CONTEXT_DEFAULT_PROFILE,
        choices=tuple(PROFILE_VERSIONS),
        help="current profile (default: general)",
    )
    mem_recall_parser.add_argument(
        "--tag", action="append", default=[], metavar="TAG",
        help="require at least one of these tags; repeatable",
    )
    mem_recall_parser.add_argument(
        "--workspace", metavar="LABEL",
        help="workspace label that workspace-scoped memories must match",
    )
    mem_recall_parser.add_argument(
        "--budget", type=int, default=CONTEXT_DEFAULT_BUDGET_BYTES,
        metavar="BYTES",
        help="memory content budget in UTF-8 bytes",
    )
    mem_recall_parser.add_argument(
        "--max-sensitivity", choices=MEMORY_SENSITIVITY_LEVELS,
        default=MEMORY_DEFAULT_SENSITIVITY,
        help="highest sensitivity to retrieve (default: internal)",
    )
    mem_recall_parser.add_argument(
        "--as-of", metavar="YYYY-MM-DD",
        help="evaluate expiry as of this date",
    )
    mem_recall_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
    )

    mem_export_parser = subparsers.add_parser(
        "mem-export",
        help="export selected memories as a portable exchange file",
        description=(
            "Write a versioned, reviewable exchange file for manual transfer "
            "to another agent or machine. Private memories are excluded "
            "unless --include-private is given."
        ),
    )
    mem_export_parser.add_argument("store", metavar="STORE")
    mem_export_parser.add_argument(
        "--id", action="append", default=[], metavar="MEMORY_ID",
        help="export only this memory; repeatable",
    )
    mem_export_parser.add_argument(
        "--tag", action="append", default=[], metavar="TAG",
        help="export memories carrying this tag; repeatable",
    )
    mem_export_parser.add_argument(
        "--max-sensitivity", choices=MEMORY_SENSITIVITY_LEVELS,
        default=MEMORY_DEFAULT_SENSITIVITY,
        help="highest sensitivity to export (default: internal)",
    )
    mem_export_parser.add_argument(
        "--include-private", action="store_true",
        help="include memories classified private (explicit opt-in)",
    )
    mem_export_parser.add_argument(
        "--include-inactive", action="store_true",
        help="include retracted, superseded, and expired memories",
    )
    mem_export_parser.add_argument(
        "--as-of", metavar="YYYY-MM-DD",
        help="evaluate expiry as of this date",
    )
    mem_export_parser.add_argument(
        "--output", metavar="FILE",
        help="write the exchange file here instead of stdout",
    )
    mem_export_parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing --output file",
    )

    mem_import_parser = subparsers.add_parser(
        "mem-import",
        help="import a portable memory exchange file (untrusted by default)",
        description=(
            "Validate and import memories from an exchange file. Imported "
            "records are always stored as proposed: a file can transfer "
            "content and provenance, never trust."
        ),
    )
    mem_import_parser.add_argument("store", metavar="STORE")
    mem_import_parser.add_argument("exchange_file", metavar="FILE")
    mem_import_parser.add_argument(
        "--dry-run", action="store_true",
        help="validate and report without writing anything",
    )
    mem_import_parser.add_argument(
        "--max-sensitivity", choices=MEMORY_SENSITIVITY_LEVELS,
        default=MEMORY_DEFAULT_SENSITIVITY,
        help="highest sensitivity to import (default: internal)",
    )
    mem_import_parser.add_argument(
        "--actor", metavar="ID", help="declared actor id (not authentication)",
    )
    mem_import_parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit machine-readable JSON",
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

    # --- Step 01: universal onboarding ------------------------------------
    _register_onboarding_parsers(subparsers)

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
    memory_items, memory_report = _memory_selection(
        args,
        objective=objective or "",
        profile=args.profile,
        budget_bytes=args.budget,
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
        memory_items=memory_items,
        memory_report=memory_report,
        memory_as_of=args.memory_as_of,
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


def _write_report_output(path: str, text: str, *, force: bool) -> None:
    target = Path(path)
    publish_text_atomic(target, text, force=force, what="quality report")


def _preflight_memory_selection(args, *, objective: str, profile: str):
    """Recall a memory selection for preflight inspection (read-only)."""

    if not getattr(args, "memory_store", None):
        return None
    store = MemoryStore(args.memory_store)
    selection = recall_memories(
        store.snapshots(as_of=getattr(args, "memory_as_of", None))["memories"],
        objective=objective,
        profile=profile,
        tags=getattr(args, "memory_tag", []) or [],
        max_sensitivity=getattr(
            args, "memory_max_sensitivity", MEMORY_DEFAULT_SENSITIVITY
        ),
        as_of=getattr(args, "memory_as_of", None),
        scope_values=(
            {"workspace": args.memory_workspace}
            if getattr(args, "memory_workspace", None)
            else None
        ),
    )
    return selection


def _emit_report(report: dict, *, as_json: bool, output: str | None,
                 force: bool) -> None:
    if as_json:
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_quality_report_markdown(report)
    if output:
        _write_report_output(output, text, force=force)
        print(f"Wrote quality report to {output}")
        return
    sys.stdout.write(text)


def _cmd_preflight(args) -> None:
    goal = load_goal_file(args.goal_file)
    memory = _preflight_memory_selection(
        args, objective=goal["objective"], profile=goal["profile"]
    )
    report = run_preflight(goal, memory=memory)
    _emit_report(report, as_json=args.as_json, output=args.output,
                 force=args.force)


def _cmd_quality_check(args) -> None:
    goal = load_goal_file(args.goal_file)
    deliverable_path = Path(args.deliverable)
    if deliverable_path.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the deliverable file; pass "
            "the real path so the bytes match what you inspected"
        )
    try:
        raw = deliverable_path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read deliverable file: {exc.strerror or exc}"
        ) from None
    try:
        deliverable_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("deliverable file must be valid UTF-8") from None

    if args.request:
        request = load_quality_request_file(args.request)
    else:
        request = {}
    if args.require_section:
        request["required_sections"] = list(args.require_section)
    if args.require_text:
        request["required_texts"] = list(args.require_text)
    if args.forbid_text:
        request["forbidden_texts"] = list(args.forbid_text)
    if args.min_words is not None:
        request["min_words"] = args.min_words
    if args.max_words is not None:
        request["max_words"] = args.max_words
    if args.no_output_contract:
        request["use_output_contract"] = False

    report = run_quality_check(
        goal,
        deliverable_text=deliverable_text,
        request=request or {},
        deliverable_source="operator-supplied file",
    )
    _emit_report(report, as_json=args.as_json, output=args.output,
                 force=args.force)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _cmd_feedback_check(args) -> None:
    feedback = load_feedback_file(args.feedback_file)
    report = {
        "feedback_id": feedback_identity(feedback),
        "state": feedback["state"],
        "origin": feedback["origin"],
        "trust": feedback_trust(feedback),
        "revision": feedback_revision(feedback),
        "severity": feedback["severity"],
        "warnings": feedback_issues(feedback),
        "has_lesson": bool(feedback["lesson"]),
        "memory_type": feedback["memory_type"],
        "scope": feedback["scope"],
    }
    if args.as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return
    print(f"Feedback valid ({report['feedback_id']})")
    print(f"State:     {report['state']} (revision {report['revision']})")
    print(f"Origin:    {report['origin']} — trust {report['trust']}")
    print(f"Severity:  {report['severity']}")
    print(f"Lesson:    {'present' if report['has_lesson'] else 'absent'}"
          f" ({report['memory_type']})")
    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning['code']}: {warning['detail']}")


def _cmd_feedback_review(args) -> None:
    feedback = load_feedback_file(args.feedback_file)
    current_revision = feedback_revision(feedback)
    if args.replace:
        if args.expected_revision is None:
            raise ValidationError(
                "feedback-review: --replace requires --expected-revision N "
                "(the revision you read); re-read the file and retry"
            )
        if args.expected_revision != current_revision:
            raise ValidationError(
                f"feedback-review: the file is at revision "
                f"{current_revision}, not the expected {args.expected_revision}; "
                "someone reviewed it in parallel — re-read and retry"
            )
    reviewed = record_review(
        feedback,
        decision=args.decision,
        reviewer=args.actor,
        at=args.at or _utc_now_iso(),
        note=args.note,
    )
    text = json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n"
    if args.replace:
        target = Path(args.feedback_file)
        publish_text_atomic(target, text, force=True,
                            what="reviewed feedback")
        print(f"Reviewed feedback written to {args.feedback_file} "
              f"(revision {feedback_revision(reviewed)})")
        return
    if args.output:
        publish_text_atomic(Path(args.output), text, force=args.force,
                            what="reviewed feedback")
        print(f"Reviewed feedback written to {args.output}")
        return
    sys.stdout.write(text)


def _cmd_feedback_lesson(args) -> None:
    feedback = load_feedback_file(args.feedback_file)
    for warning in feedback_issues(feedback):
        if warning["code"] in ("feedback_not_reviewed",
                               "evaluation_self_reported"):
            print(f"warning: {warning['detail']}", file=sys.stderr)
    record = lesson_memory_record(feedback, actor=args.actor)
    store = MemoryStore(args.store)
    appended = store.propose(record)["memory"]
    print(f"Lesson stored as PROPOSED memory {appended['memory_id']} "
          f"in {args.store}")
    print("Authority is unchanged: review it explicitly with "
          f"'aperio mem-review {args.store} {appended['memory_id']} --accept'")


def _cmd_feedback_compare(args) -> None:
    comparison = compare_quality_reports(args.before, args.after)
    if args.as_json:
        text = json.dumps(comparison, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_comparison_markdown(comparison)
    if args.output:
        _write_report_output(args.output, text, force=args.force)
        print(f"Wrote comparison to {args.output}")
        return
    sys.stdout.write(text)


def _cmd_improvement_apply(args) -> None:
    proposal = load_proposal_file(args.file)
    target_path = Path(args.target_file)
    if target_path.is_symlink():
        raise ValidationError(
            "refusing to follow a symlink for the target file"
        )
    try:
        raw = target_path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read target file: {exc.strerror or exc}"
        ) from None
    try:
        target_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("target file must be valid UTF-8") from None
    try:
        target_data = json.loads(target_text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"target file is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None

    if proposal["target"] == "goal":
        applied = apply_proposal_to_goal(proposal, target_data)
    else:
        applied = apply_proposal_to_request(proposal, target_data)
    text = json.dumps(applied, ensure_ascii=False, indent=2) + "\n"
    if not args.as_json:
        publish_text_atomic(Path(args.output), text, force=args.force,
                            what="updated document")
        change = proposal["change"]
        print(
            f"Applied {change['action']} {change['field']!r} to a copy at "
            f"{args.output}; the original file is unchanged."
        )
        return
    sys.stdout.write(text)


def _cmd_exchange_export(args) -> None:
    from aperio.memportable import export_memories
    from aperio.memstore import MemoryStore

    sections = []
    if args.goal:
        sections.append(goal_section_from_file(args.goal))
    if args.context_pack:
        sections.append(context_pack_section_from_file(args.context_pack))
    if args.quality_report:
        sections.append(quality_report_section_from_file(args.quality_report))
    for path in args.feedback:
        sections.append(feedback_section_from_file(path))
    if args.memory_store:
        if not args.memory_id and not args.memory_tag:
            raise ValidationError(
                "exchange-export: select memories explicitly with "
                "--memory-id or --memory-tag; an exchange carries a "
                "deliberate selection, not a whole store"
            )
        document = export_memories(
            MemoryStore(args.memory_store),
            memory_ids=list(args.memory_id) or None,
            tags=list(args.memory_tag) or None,
            as_of=args.memory_as_of,
            include_private=args.include_private,
        )
        sections.append(memory_section(document))
    if args.note:
        sections.append(
            notes_section([{"text": note} for note in args.note])
        )
    if not sections:
        raise ValidationError(
            "exchange-export: select at least one section "
            "(--goal/--context-pack/--quality-report/--feedback/"
            "--memory-store/--note)"
        )
    exchange = build_exchange(sections)
    text = json.dumps(exchange, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        publish_text_atomic(Path(args.output), text, force=args.force,
                            what="exchange document")
        print(f"Wrote universal exchange to {args.output} "
              f"({len(exchange['sections'])} section(s): "
              f"{', '.join(exchange['manifest']['includes'])})")
        return
    if not args.as_json:
        raise ValidationError(
            "exchange-export: choose --output FILE (or --json to print the "
            "document); stdout prose output does not exist for exchanges"
        )
    sys.stdout.write(text)


def _cmd_exchange_import(args) -> None:
    exchange = load_universal_exchange_file(args.exchange_file)
    if args.apply:
        report = apply_import(
            exchange,
            args.dest_dir,
            memory_store_path=args.memory_store,
            max_sensitivity=args.max_sensitivity,
        )
    else:
        report = plan_import(
            exchange,
            args.dest_dir,
            memory_store_path=args.memory_store,
            max_sensitivity=args.max_sensitivity,
        )
    if args.as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return
    sys.stdout.write(render_import_report_markdown(report))


def _cmd_agent_brief(args) -> None:
    goal = load_goal_file(args.goal_file)
    if getattr(args, "memory_max_sensitivity", "internal") == "private":
        # A brief is a shareable artifact: private memory never enters,
        # whatever the flag says.
        print(
            "warning: private memory is never written into an agent brief; "
            "the selection is capped at internal",
            file=sys.stderr,
        )
        args.memory_max_sensitivity = "internal"
    memory = _preflight_memory_selection(
        args, objective=goal["objective"], profile=goal["profile"]
    )
    brief = build_agent_brief(
        goal, memory_selection=memory, task_reference=args.task_reference
    )
    if args.as_json:
        text = json.dumps(brief, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_agent_brief_markdown(brief)
    if args.output:
        _write_report_output(args.output, text, force=args.force)
        print(f"Wrote agent brief to {args.output}")
        return
    sys.stdout.write(text)


def _load_benchmark_cases(args) -> list[dict]:
    if args.cases:
        directory = Path(args.cases)
        if not directory.is_dir():
            raise ValidationError(
                f"benchmark cases directory does not exist: {directory}"
            )
        cases = [load_case_file(path) for path in sorted(directory.glob("*.json"))]
        if not cases:
            raise ValidationError(
                f"no benchmark cases found in {directory}"
            )
        return cases
    return builtin_cases()


def _cmd_benchmark_cases(args) -> None:
    cases = _load_benchmark_cases(args)
    if args.as_json:
        payload = [
            {"case_id": c["case_id"], "domain": c["domain"],
             "scenario": c["scenario"], "checks": c["checks"],
             "expected_rules": c["expected_rules"],
             "requires_human": c["requires_human"]}
            for c in cases
        ]
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    for case in cases:
        print(f"{case['case_id']}  [{case['domain']}]  {case['scenario']}")
    print(f"\n{len(cases)} case(s); benchmark-case v1; "
          "human-judged metrics are listed per case and never scored.")


def _cmd_benchmark_run(args) -> None:
    cases = _load_benchmark_cases(args)
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            raise ValidationError(
                "no case matched --only: " + ", ".join(sorted(wanted))
            )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aperio-benchmark-") as td:
        report = run_benchmark(cases, Path(td))
    if args.as_json:
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_benchmark_markdown(report)
    if args.output:
        _write_report_output(args.output, text, force=args.force)
        print(f"Wrote benchmark report to {args.output}")
        return
    sys.stdout.write(text)


def _read_message_arg(args) -> str:
    if args.message and args.file:
        raise ValidationError(
            "capture: use --message or --file, not both"
        )
    if args.file:
        from aperio.context_pack import read_utf8_text_file

        return read_utf8_text_file(args.file, max_bytes=16 * 1024)
    if not args.message:
        raise ValidationError(
            "capture: provide --message TEXT or --file F"
        )
    return args.message


def _cmd_capture(args) -> None:
    message = _read_message_arg(args)
    aperio_dir = Path(args.project) / ".aperio"
    event = capture_message(
        aperio_dir,
        message,
        operator_urgent=args.urgent,
        actor=args.actor,
    )
    print(confirmation_line(event))
    print(
        f"Capture {event['capture_id']} stored in .aperio/inbox.jsonl "
        "(append-only); review with: aperio inbox "
        + str(args.project)
    )

    _refresh_after_transition(
        Path(getattr(args, 'project')) / ".aperio", 'brain-dump captured'
    )

def _cmd_inbox(args) -> None:
    state = IntakeLog(Path(args.project) / ".aperio").read()
    resolved_keys = {
        (r["capture_id"], r["item_index"]) for r in state["resolves"]
    }
    if args.as_json:
        payload = {
            "counts": state["counts"],
            "damaged": state["damaged"],
            "pending": [
                {"capture_id": c["capture_id"], "at": c["at"],
                 "actor": c["actor"], "operator_urgent":
                 c["operator_urgent"], "active_task": c["active_task"],
                 "items": c["items"]}
                for c in state["captures"]
                if any((c["capture_id"], i) not in resolved_keys
                       for i in range(len(c["items"])))
            ],
        }
        if args.include_resolved:
            payload["resolved"] = [
                {"capture_id": r["capture_id"],
                 "item_index": r["item_index"], "action": r["action"],
                 "at": r["at"], "actor": r["actor"], "note": r["note"]}
                for r in state["resolves"]
            ]
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    pending = [
        c for c in state["captures"]
        if any((c["capture_id"], i) not in resolved_keys
               for i in range(len(c["items"])))
    ]
    print(
        f"{state['counts']['pending_items']} pending item(s) across "
        f"{len(pending)} capture(s)"
        + (f"; {len(state['damaged'])} damaged line(s)" if state["damaged"]
           else "")
    )
    print("classification is deterministic keyword triage, not language "
          "understanding; review items before acting on them")
    for capture_event in pending:
        print(f"\n{capture_event['capture_id']}  {capture_event['at']}"
              + ("  [URGENT: operator-requested]"
                 if capture_event["operator_urgent"] else "")
              + (f"  (during {capture_event['active_task']['task_id']})"
                 if capture_event["active_task"] else ""))
        for index, item in enumerate(capture_event["items"]):
            if (capture_event["capture_id"], index) in resolved_keys:
                continue
            flag = " [urgency language, no authority]" \
                if item["urgency_language_detected"] else ""
            print(f"  [{index}] {item['category']} -> {item['routing']}"
                  f" (basis: {item['routing_basis']}){flag}")
            print(f"      {item['text'][:120]}")
    if args.include_resolved and state["resolves"]:
        print("\nResolved:")
        for r in state["resolves"]:
            print(f"  {r['capture_id']}[{r['item_index']}] {r['action']}"
                  f" by {r['actor']} at {r['at']}")


def _cmd_inbox_resolve(args) -> None:
    event = resolve_intake_item(
        Path(args.project) / ".aperio", args.capture_id, args.item_index,
        action=args.action, actor=args.actor, note=args.note,
    )
    print(f"Recorded {event['action']} on {event['capture_id']}"
          f"[{event['item_index']}] (append-only; intake history unchanged)")

    _refresh_after_transition(
        Path(getattr(args, 'project')) / ".aperio", 'intake item resolved'
    )

def _aperio_dir_of(args) -> Path:
    return Path(args.project) / ".aperio"


def _refresh_after_transition(aperio_dir: Path, reason: str) -> None:
    """Write a checkpoint after a meaningful state transition.

    Best-effort by design: a checkpoint failure must never corrupt the
    transition that just happened, so failures are reported on stderr
    and leave the transition intact.
    """

    try:
        refresh_checkpoint(aperio_dir, reason=reason)
    except Exception as exc:  # noqa: BLE001 - deliberate: continuity aid
        print(
            f"warning: checkpoint not updated ({str(exc)[:160]})",
            file=sys.stderr,
        )


def _cmd_checkpoint_write(args) -> None:
    document = refresh_checkpoint(_aperio_dir_of(args), reason=args.reason)
    print(
        f"Checkpoint revision {document['revision']} written "
        f"({document['written_at']}): {document['next_recommended_action']}"
    )


def _cmd_checkpoint_show(args) -> None:
    document = read_checkpoint(_aperio_dir_of(args))
    if args.as_json:
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2)
                         + "\n")
        return
    print(f"Checkpoint revision {document['revision']} "
          f"({document['written_at']}, reason: {document['reason']})")
    tasks = document["tasks_summary"]
    print(f"Verified completed: {len(tasks['verified_completed'])}"
          f"; completed without evidence: "
          f"{len(tasks['completed_without_evidence'])}"
          f"; in progress: {len(tasks['in_progress'])}"
          f"; pending: {len(tasks['pending'])}"
          f"; blocked: {len(tasks['blocked'])}")
    if document["active_task"]:
        print(f"Active task: {document['active_task']['task_id']}"
              f" ({document['active_task'].get('title')})")
    print(f"Inbox: {document['inbox']['pending_items']} pending, "
          f"{document['inbox']['urgent_pending']} urgent")
    if document["unverified_signals"]:
        print("Unverified signals (not evidence):")
        for signal in document["unverified_signals"]:
            print(f"  - {signal}")
    print(f"Next: {document['next_recommended_action']}")
    print(f"Resume eligibility: "
          f"{'eligible' if document['resume_eligibility']['eligible'] else 'gated'}")
    for reason in document["resume_eligibility"]["reasons"]:
        print(f"  - {reason}")


def _cmd_recover(args) -> None:
    report = recover_report(_aperio_dir_of(args))
    if args.as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2)
                         + "\n")
        return
    sys.stdout.write(render_recovery_markdown(report))


def _cmd_plan_propose_from_goal(args) -> None:
    goal = load_goal_file(args.goal_file)
    proposal = plan_proposal_from_goal(goal, actor=args.actor)
    # (feedback's load_proposal_file is a different contract; this handler
    # only builds proposals, it does not load them from disk)
    if args.create:
        from aperio.planstore import create_plan

        record = create_plan(
            Path(args.project) / ".aperio", proposal["plan_input"],
        )
        print(
            f"Created DRAFT plan revision {record['plan_version']} from "
            f"goal {proposal['goal_identity']}; approval is still "
            "required before any execution ('aperio plan-approve')."
        )
        if args.as_json:
            sys.stdout.write(json.dumps(
                {"proposal": proposal,
                 "created_plan_version": record["plan_version"]},
                ensure_ascii=False, indent=2) + "\n")
            return
        return
    text = json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json if False else args.as_json:
        pass
    if args.output:
        publish_text_atomic(Path(args.output), text + "\n",
                            force=args.force, what="plan proposal")
        print(f"Wrote plan proposal to {args.output}")
        return
    if args.as_json:
        sys.stdout.write(text + "\n")
        return
    sys.stdout.write(render_proposal_markdown(proposal))


def _orchestration_inputs(args) -> tuple[dict, list[dict], dict[str, str]]:
    goal = load_goal_file(args.goal_file)
    plan = read_plan(Path(args.project) / ".aperio")
    capabilities: dict[str, str] = {}
    for entry in getattr(args, "capability", None) or []:
        if ":" not in entry:
            raise ValidationError(
                f"orchestration: --capability must be TASK:CAPABILITY, "
                f"got {entry!r}"
            )
        task_id, capability = entry.split(":", 1)
        capabilities[task_id] = _check_single_line(
            capability, "orchestration --capability value"
        )
    return goal, plan.get("tasks", []), capabilities


def _check_single_line(value: str, where: str) -> str:
    if not value.strip() or "\n" in value or "\x00" in value:
        raise ValidationError(f"{where} must be single-line non-empty text")
    return value


def _cmd_orchestrate_assess(args) -> None:
    goal, plan_tasks, capabilities = _orchestration_inputs(args)
    from aperio.adapters import builtin_adapters

    available = [a.id for a in builtin_adapters()
                 if a.available()[0] or a.id == "manual"]
    assessment = assess_orchestration(
        goal, plan_tasks=plan_tasks, available_adapters=available,
        required_capabilities=capabilities,
    )
    if args.as_json:
        sys.stdout.write(json.dumps(assessment, ensure_ascii=False, indent=2)
                         + "\n")
        return
    print(f"Recommendation: **{assessment['recommendation']}**")
    print("Criteria: "
          + json.dumps(assessment["criteria"], ensure_ascii=False))
    print("Reasons:")
    for reason in assessment["reasons"]:
        print(f"  - {reason}")
    if assessment["capability_gaps"]:
        print("Capability gaps (manual fallback required):")
        for gap in assessment["capability_gaps"]:
            print(f"  - {gap['task_id']}: {gap['capability']} — "
                  f"{gap['fallback']}")


def _cmd_orchestrate_plan(args) -> None:
    goal, plan_tasks, capabilities = _orchestration_inputs(args)
    overrides = {}
    for entry in getattr(args, "adapter", None) or []:
        if ":" not in entry:
            raise ValidationError(
                f"orchestration: --adapter must be TASK:ADAPTER, got "
                f"{entry!r}"
            )
        task_id, adapter = entry.split(":", 1)
        overrides[task_id] = _check_single_line(
            adapter, "orchestration --adapter value"
        )
    from aperio.adapters import builtin_adapters

    available = [a.id for a in builtin_adapters()
                 if a.available()[0] or a.id == "manual"]
    document = build_orchestration_plan(
        goal, plan_tasks,
        required_capabilities=capabilities,
        adapter_override=overrides,
        available_adapters=available,
        actor=args.actor,
    )
    store = OrchestrationStore(Path(args.project) / ".aperio")
    store.write(document)
    if args.as_json:
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2)
                         + "\n")
        return
    print(f"Orchestration plan written to {store.path} "
          f"({len(document['delegations'])} delegation(s), recommendation: "
          f"{document['recommendation']}; sequential-only).")
    for delegation in document["delegations"]:
        print(f"  {delegation['task_id']}: {delegation['specialist_adapter']}"
              + (f" ({delegation['required_capability']})"
                 if delegation["required_capability"] else ""))
    print("Dispatch stays through 'aperio run-start' with its "
          "authorization gates; briefs: aperio orchestrate-brief.")


def _cmd_orchestrate_brief(args) -> None:
    aperio_dir = Path(args.project) / ".aperio"
    orchestration = OrchestrationStore(aperio_dir).read()
    plan = read_plan(aperio_dir)
    goal = load_goal_file(args.goal_file)
    brief = specialist_brief(orchestration, plan, args.task_id, goal)
    if args.as_json:
        sys.stdout.write(json.dumps(brief, ensure_ascii=False, indent=2)
                         + "\n")
        return
    sys.stdout.write(render_specialist_brief_markdown(brief))


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


def _check_output_destination(
    raw: str,
    *,
    goal_path: str,
    source_label: str = "the goal file",
    artifact_label: str = "its own context pack",
) -> Path:
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
            f"refusing to overwrite {source_label} with {artifact_label}; "
            "choose a different --output path"
        )
    return target


def _write_pack_output(
    path: str, text: str, *, force: bool, goal_path: str
) -> None:
    target = _check_output_destination(path, goal_path=goal_path)
    publish_text_atomic(target, text, force=force, what="context pack")


def _memory_selection(args, *, objective: str, profile: str, budget_bytes: int):
    """Recall memories for a pack; returns (context items, report or None)."""

    store_path = getattr(args, "memory_store", None)
    if not store_path:
        return [], None
    store = MemoryStore(store_path)
    state = store.snapshots(as_of=getattr(args, "memory_as_of", None))
    selection = recall_memories(
        state["memories"],
        objective=objective,
        profile=profile,
        tags=getattr(args, "memory_tag", []) or [],
        budget_bytes=budget_bytes,
        max_sensitivity=getattr(
            args, "memory_max_sensitivity", MEMORY_DEFAULT_SENSITIVITY
        ),
        as_of=getattr(args, "memory_as_of", None),
        scope_values=(
            {"workspace": args.memory_workspace}
            if getattr(args, "memory_workspace", None)
            else None
        ),
    )
    warnings = list(selection["warnings"])
    if state["damaged"]:
        warnings.append(
            {
                "code": "memory_store_damaged",
                "detail": (
                    f"{len(state['damaged'])} damaged line(s) in the memory "
                    "store were skipped; run 'aperio mem-list' to review them"
                ),
            }
        )
    report = {
        "store": "operator-selected memory store",
        "memory_ids": [memory["memory_id"] for memory in selection["selected"]],
        "counts": {
            "considered": selection["budget"]["items_considered"],
            "included": selection["budget"]["items_included"],
            "excluded": selection["excluded"]["total"],
        },
        "excluded": selection["excluded"]["by_reason"],
        "conflicts": selection["conflicts"],
        "limitation": RECALL_LIMITATION,
        "warnings": warnings,
    }
    return (
        memory_context_items(
            selection["selected"],
            as_of=getattr(args, "memory_as_of", None),
        ),
        report,
    )


def _cmd_mem_propose(args) -> None:
    content = args.content
    if args.content_file:
        content = read_utf8_text_file(
            args.content_file,
            label="memory content file",
            max_bytes=MEMORY_MAX_CONTENT_BYTES,
        )
    scope = {"kind": args.scope, "profile": args.scope_profile, "value": args.scope_value}
    store = MemoryStore(args.store)
    result = store.propose(
        {
            "type": args.memory_type,
            "title": args.title,
            "content": content,
            "scope": scope,
            "source": {"kind": args.source, "ref": args.source_ref},
            "tags": args.tag,
            "sensitivity": args.sensitivity,
            "evidence": args.evidence,
            "valid_from": args.valid_from,
            "valid_until": args.valid_until,
            "supersedes": args.supersedes,
            "actor": args.actor,
        }
    )
    memory = result["memory"]
    if args.as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "created": result["created"],
                    "duplicate": result["duplicate"],
                    "memory": memory,
                    "store_created": result["created"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return
    if result["duplicate"]:
        print(
            f"Memory {memory['memory_id']} already holds this exact content; "
            "nothing was written."
        )
    else:
        print(f"Proposed memory {memory['memory_id']} in {args.store}")
    _print_memory_summary(memory)
    print()
    print(
        "State: proposed - stored, not trusted. Review it with "
        f"'aperio mem-review {args.store} {memory['memory_id']} --accept' "
        "or --retract."
    )


def _print_memory_summary(memory: dict) -> None:
    scope = memory["scope"]
    scope_text = scope["kind"]
    if scope.get("profile"):
        scope_text += f":{scope['profile']}"
    if scope.get("value"):
        scope_text += f":{scope['value']}"
    print(f"Title:       {memory['title']}")
    print(f"Type:        {memory['type']}")
    print(f"Scope:       {scope_text}")
    print(f"State:       {memory['state']} (trust: {memory['trust']})")
    print(f"Sensitivity: {memory['sensitivity']}")
    print(
        "Source:      "
        f"{memory['source']['kind']}"
        + (f" / {memory['source']['ref']}" if memory["source"].get("ref") else "")
    )
    if memory.get("tags"):
        print("Tags:        " + ", ".join(memory["tags"]))
    if memory.get("evidence"):
        print("Evidence:    " + "; ".join(memory["evidence"]))
    if memory.get("valid_until"):
        print(f"Valid until: {memory['valid_until']}")
    print(f"Identity:    {memory['memory_id']} (content-derived)")
    print(f"Content SHA-256: {memory['content_sha256']}")


def _cmd_mem_review(args) -> None:
    store = MemoryStore(args.store)
    memory = store.review(
        args.memory_id,
        args.action,
        actor=args.actor,
        note=args.note,
        evidence=args.evidence,
        reason=args.reason,
    )
    if args.as_json:
        sys.stdout.write(
            json.dumps(memory, ensure_ascii=False, indent=2) + "\n"
        )
        return
    print(f"Memory {memory['memory_id']} is now {memory['state']} "
          f"(trust: {memory['trust']}, revision {memory['revision']})")
    if memory["state"] == "attested":
        print(
            "Accepted for its declared scope. Acceptance is not factual "
            "verification: the claim itself was not independently checked. "
            "Use 'aperio mem-review --verify --evidence ... --actor ...' if "
            "evidence supports it."
        )
    elif memory["state"] == "verified":
        verification = memory.get("verification") or {}
        evidence = verification.get("evidence") or []
        print(
            f"Evidence recorded: {len(evidence)} reference(s), reviewer "
            f"{verification.get('reviewer')!r}."
        )
        print(
            "Aperio stores the claim and the reference; it does not re-check "
            "the evidence and cannot authenticate the reviewer. This records "
            "declared separation of identities, not proven independence."
        )
    elif memory["state"] == "retracted":
        print(
            "Retracted. It stays in the store and the review history, and "
            "is excluded from normal recall."
        )


def _cmd_mem_list(args) -> None:
    store = MemoryStore(args.store)
    state = store.snapshots(as_of=args.as_of)
    memories = []
    for memory_id in sorted(state["memories"]):
        memory = state["memories"][memory_id]
        if args.state and memory["state"] not in args.state:
            continue
        if (
            MEMORY_SENSITIVITY_RANK[memory["sensitivity"]]
            > MEMORY_SENSITIVITY_RANK[args.max_sensitivity]
        ):
            continue
        if not args.include_inactive and memory["state"] in (
            "superseded", "retracted", "expired",
        ):
            continue
        memories.append(memory)
    if args.as_json:
        sys.stdout.write(
            json.dumps(
                {"memories": memories, "damaged": state["damaged"]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return
    if not memories:
        print(f"No memories in {args.store} matched these filters.")
    for memory in memories:
        validity = memory.get("validity") or {}
        note = ""
        if validity.get("reason") == "future":
            note = f"  (not yet valid: {validity.get('detail')})"
        elif validity.get("reason") == "expired":
            note = f"  (not current: {validity.get('detail')})"
        print(
            f"{memory['memory_id']}  {memory['state']:<10} "
            f"{memory['trust']:<10} {memory['type']:<16} "
            f"{memory['title']}{note}"
        )
    if state["damaged"]:
        print()
        print(
            f"Damaged: {len(state['damaged'])} line(s) could not be read and "
            "were skipped (reported, never silently repaired):"
        )
        for problem in state["damaged"][:5]:
            print(f"  line {problem['line']}: {problem['reason']}")
    print()
    print("Inspect one memory: aperio mem-show <store> <memory_id>")


def _cmd_mem_show(args) -> None:
    store = MemoryStore(args.store)
    state = store.snapshots()
    memory = state["memories"].get(args.memory_id)
    if memory is None:
        raise StateError(f"no memory {args.memory_id} in {args.store}")
    if args.as_json:
        sys.stdout.write(
            json.dumps(memory, ensure_ascii=False, indent=2) + "\n"
        )
        return
    _print_memory_summary(memory)
    validity = memory.get("validity") or {}
    if validity:
        print(
            f"Validity: {'current' if validity.get('current') else validity.get('detail')}"
            f" (evaluated {validity.get('evaluated_on')}; "
            f"{validity.get('rule')})"
        )
    verification = memory.get("verification") or {}
    if verification.get("basis") == "unrecorded":
        print(
            "Verification: recorded without proof of support - treated as "
            "attested, not evidence-backed."
        )
        for reason in verification.get("reasons") or []:
            print(f"  reason: {reason}")
        print(
            "  Re-run 'aperio mem-review --verify --evidence ... --actor ...' "
            "to record support."
        )
    elif verification.get("basis") == "evidence":
        print(
            "Verification: supported by recorded evidence "
            f"({len(verification.get('evidence') or [])} reference(s), "
            f"reviewer {verification.get('reviewer')!r})"
        )
    else:
        print(
            "Verification: none (acceptance records authority for scope, not "
            "factual proof)"
        )
    print()
    if memory["history"]:
        print("Review history (append-only):")
        for event in memory["history"]:
            print(
                f"  rev {event['revision']}: {event['action']} "
                f"{event['from_state']} -> {event['to_state']} "
                f"at {event['at']}"
                + (f" by {event['actor']}" if event.get("actor") else "")
            )
            if event.get("note"):
                print(f"    note: {event['note']}")
            if event.get("reason"):
                print(f"    reason: {event['reason']}")
            if event.get("evidence"):
                print(f"    evidence: {'; '.join(event['evidence'])}")
    else:
        print("Review history: none yet (proposed only).")
    print()
    print(_quote_text_block(memory["content"]))


def _quote_text_block(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def _cmd_mem_recall(args) -> None:
    objective = args.objective or ""
    profile = args.profile
    if args.goal:
        goal = load_goal_file(args.goal)
        objective = goal["objective"]
        profile = goal["profile"]
    store = MemoryStore(args.store)
    state = store.snapshots(as_of=args.as_of)
    selection = recall_memories(
        state["memories"],
        objective=objective,
        profile=profile,
        tags=args.tag,
        budget_bytes=args.budget,
        max_sensitivity=args.max_sensitivity,
        as_of=args.as_of,
        scope_values={"workspace": args.workspace} if args.workspace else None,
    )
    if args.as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "objective": objective,
                    "profile": profile,
                    "selection": selection,
                    "damaged": state["damaged"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return
    print(f"Objective: {objective or '(none)'}")
    print(f"Profile:   {profile}")
    print(f"Evaluated: {selection['as_of']} ({selection['validity']['rule']})")
    print(
        f"Selected:  {selection['budget']['items_included']} of "
        f"{selection['budget']['items_considered']} memories "
        f"({selection['budget']['used_bytes']} of "
        f"{selection['budget']['limit_bytes']} bytes)"
    )
    print()
    if not selection["selected"]:
        print("No memory matched this objective and filter set.")
    for memory in selection["selected"]:
        print(
            f"{memory['memory_id']}  {memory['state']:<10} "
            f"{memory['trust']:<10} {memory['title']}"
        )
        print(f"  scope: {memory['scope']['kind']}, "
              f"relevance {memory['relevance']}, score {memory['score']}")
        print("  " + _quote_text_block(memory["content"]).replace("\n", "\n  "))
        print()
    if selection["excluded"]["by_reason"]:
        print("Excluded: " + ", ".join(
            f"{reason}={count}"
            for reason, count in selection["excluded"]["by_reason"].items()
        ))
    for warning in selection["warnings"]:
        print(f"Warning ({warning['code']}): {warning['detail']}")
    print()
    print(f"Note: {RECALL_LIMITATION}")


def _cmd_mem_export(args) -> None:
    store = MemoryStore(args.store)
    document = export_memories(
        store,
        memory_ids=args.id,
        tags=args.tag,
        max_sensitivity=args.max_sensitivity,
        include_inactive=args.include_inactive,
        include_private=args.include_private,
        as_of=args.as_of,
    )
    text = render_export(document)
    if args.output:
        target = _check_output_destination(
            args.output,
            goal_path=args.store,
            source_label="the memory store",
            artifact_label="its own export",
        )
        publish_text_atomic(
            target, text, force=args.force,
            what="memory exchange file",
        )
        print(
            f"Exported {document['counts']['memories']} memories to "
            f"{args.output}"
        )
        print(
            "The file contains only selected memory content, scope, "
            "provenance, review state, and content hashes. It contains no "
            "paths, credentials, or project state."
        )
        return
    sys.stdout.write(text)


def _cmd_mem_import(args) -> None:
    document = load_exchange_file(args.exchange_file)
    store = MemoryStore(args.store)
    report = import_memories(
        store,
        document,
        actor=args.actor,
        dry_run=args.dry_run,
        max_sensitivity=args.max_sensitivity,
    )
    if args.as_json:
        sys.stdout.write(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        return
    prefix = "Would import" if args.dry_run else "Imported"
    print(f"{prefix}: {summarize_import(report)}")
    for memory_id in report["imported"]:
        print(
            f"  {memory_id}  declared state in file: "
            f"{report['declared_states'].get(memory_id, 'proposed')}"
        )
    if report["duplicates"]:
        print(
            "  duplicates (same content identity already present): "
            + ", ".join(report["duplicates"])
        )
    if report["excluded_sensitivity"]:
        print(
            "  excluded by the sensitivity filter: "
            + ", ".join(report["excluded_sensitivity"])
        )
    for memory_id, claim in report["unsupported_trust_claims"].items():
        print(f"  noted: {memory_id} - {claim}")
    for memory_id, evidence in report["transfer_evidence"].items():
        print(
            f"  review evidence transferred for {memory_id}: "
            + "; ".join(evidence)
        )
    for conflict in report["conflicts"]:
        print(
            f"  conflict: {conflict['incoming_memory_id']} and "
            f"{conflict['existing_memory_id']} describe "
            f"'{conflict['title']}' differently; both are kept"
        )
    if report["imported"] and not args.dry_run:
        print()
        print(
            "Every imported memory is stored as 'proposed'. A file can "
            "transfer content and provenance, never trust: review each one "
            "with 'aperio mem-review' before relying on it."
        )

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
    memory_items, memory_report = _memory_selection(
        args,
        objective=goal["objective"],
        profile=goal["profile"],
        budget_bytes=args.budget,
    )
    document = build_goal_context_pack(
        goal,
        workspace=args.workspace,
        budget_bytes=args.budget,
        imported_items=imported_items,
        proposed_items=proposed_items,
        memory_items=memory_items,
        memory_report=memory_report,
        memory_as_of=args.memory_as_of,
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

    _refresh_after_transition(
        Path(getattr(args, 'project_directory')) / ".aperio", 'project phase changed'
    )

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
    for task in sorted(
        plan["tasks"],
        key=lambda t: (priority_rank(t), t["id"]),
    ):
        if task["id"] in executable:
            marker = "ready"
        elif task["id"] in ready:
            marker = "dep-ready"
        else:
            marker = f"{task['status']:>11}"
        owner = f" (owner: {task['owner']})" if task["owner"] else ""
        priority = task.get("priority", "normal")
        priority_note = "" if priority == "normal" else f" [{priority}]"
        print(f"  [{marker}] {task['id']}: {task['title']}{owner}"
              f"{priority_note}")
    if any(t.get("priority", "normal") != "normal" for t in plan["tasks"]):
        print(
            "  (listed by priority, then id; dispatch follows the same "
            "order)"
        )


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

    _refresh_after_transition(
        Path(getattr(args, 'project_directory')) / ".aperio", 'run outcome reported'
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

    _refresh_after_transition(
        Path(getattr(args, 'project_directory')) / ".aperio", 'task status changed'
    )

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


# ---- Step 01 onboarding: parser registration (inserted into build_parser) ----

def _register_onboarding_parsers(subparsers) -> None:
    """Register the Step 01 universal onboarding command family.

    Formatting and flag plumbing only; all behavior lives in the
    onboarding_* modules (the CLI never contains business logic).
    """

    def _dir_arg(parser, name="DIR"):
        parser.add_argument(
            name, nargs="?", default=".",
            metavar="DIR",
            help="project directory (default: current directory)",
        )
        parser.add_argument(
            "--json", dest="as_json", action="store_true",
            help="emit the machine-readable onboarding protocol document",
        )

    start_parser = subparsers.add_parser(
        "onboarding-start",
        help="universal onboarding entry point: discover and ask ONE thing",
        description=(
            "Step 01 universal entry point. Strictly read-only: classifies "
            "the workspace and returns exactly one next action (confirm "
            "initialization, choose intent, answer a question, or resolve "
            "an ambiguous state). Never mutates anything. Resumes an "
            "existing onboarding session when one is present."
        ),
    )
    _dir_arg(start_parser)

    confirm_parser = subparsers.add_parser(
        "onboarding-confirm-init",
        help="operator-approved initialization (the only sanctioned first mutation)",
        description=(
            "Runs the existing safe init mechanism after explicit operator "
            "approval (--approve). Non-destructive by construction: only "
            "missing scaffolding is created, existing files are never "
            "overwritten, and a repeated call never destroys data."
        ),
    )
    _dir_arg(confirm_parser)
    confirm_parser.add_argument(
        "--approve", action="store_true",
        help="explicit operator approval to initialize (required)",
    )
    confirm_parser.add_argument(
        "--intent", metavar="TEXT",
        help="stated purpose for existing projects (recorded in the session)",
    )

    decline_parser = subparsers.add_parser(
        "onboarding-decline",
        help="decline onboarding: exit gracefully, change nothing",
    )
    _dir_arg(decline_parser)

    intent_parser = subparsers.add_parser(
        "onboarding-intent",
        help="record the operator's chosen intent for an existing project",
    )
    _dir_arg(intent_parser)
    intent_parser.add_argument(
        "--choice", required=True,
        choices=[c["id"] for c in _OB_INTENT_CHOICES],
        help="which suggested response the operator chose",
    )
    intent_parser.add_argument(
        "--note", metavar="TEXT",
        help="free-text detail (used for 'other')",
    )

    status_parser = subparsers.add_parser(
        "onboarding-status",
        help="show onboarding session state (read-only)",
    )
    _dir_arg(status_parser)

    answer_parser = subparsers.add_parser(
        "onboarding-answer",
        help="record ONE operator answer; returns the next single question",
    )
    _dir_arg(answer_parser)
    answer_parser.add_argument("question_id", metavar="QUESTION_ID")
    answer_parser.add_argument(
        "--answer", required=True, metavar="TEXT",
        help="the operator's verbatim answer ('Not sure yet' and 'Decide "
             "later' are recorded as such)",
    )

    revise_parser = subparsers.add_parser(
        "onboarding-revise",
        help="revise a previous answer (history is preserved)",
    )
    _dir_arg(revise_parser)
    revise_parser.add_argument("question_id", metavar="QUESTION_ID")
    revise_parser.add_argument("--answer", required=True, metavar="TEXT")

    pause_parser = subparsers.add_parser(
        "onboarding-pause",
        help="pause onboarding (state is already durable)",
    )
    _dir_arg(pause_parser)

    cancel_parser = subparsers.add_parser(
        "onboarding-cancel",
        help="explicitly cancel onboarding (state preserved, marked cancelled)",
    )
    _dir_arg(cancel_parser)
    cancel_parser.add_argument(
        "--confirm", action="store_true",
        help="explicit operator confirmation (required)",
    )

    reconstruct_parser = subparsers.add_parser(
        "onboarding-reconstruct",
        help="read-only reconstruction of existing project context",
        description=(
            "Inventories relevant documents, extracts pending-work claims "
            "with provenance, detects claim conflicts and untrusted-content "
            "risks, and reports coverage gaps that drive the gap interview. "
            "Never modifies project files. When an onboarding session "
            "exists, a bounded summary is recorded in the session."
        ),
    )
    _dir_arg(reconstruct_parser)
    reconstruct_parser.add_argument(
        "--output", metavar="FILE", help="also write the report to a file",
    )
    reconstruct_parser.add_argument(
        "--force", action="store_true", help="allow overwriting --output",
    )

    propose_parser = subparsers.add_parser(
        "onboarding-propose-standardization",
        help="build the standardization proposal (read-only)",
    )
    _dir_arg(propose_parser)
    propose_parser.add_argument(
        "--output", metavar="FILE", help="write the Markdown preview to a file",
    )
    propose_parser.add_argument("--force", action="store_true")

    apply_parser = subparsers.add_parser(
        "onboarding-apply-standardization",
        help="apply an approved standardization proposal",
        description=(
            "Applies the proposal: creates only NEW files (init repair + "
            "mapping record) after an explicit approval scoped to the "
            "exact proposal id is recorded in the session. Existing files "
            "are never modified."
        ),
    )
    _dir_arg(apply_parser)
    apply_parser.add_argument(
        "--proposal", required=True, metavar="ID", help="proposal id to apply",
    )
    apply_parser.add_argument(
        "--approve", action="store_true",
        help="explicit operator approval for THIS proposal id (required)",
    )

    discover_parser = subparsers.add_parser(
        "onboarding-discover-tools",
        help="discover hosts, tools, and MCP configuration (read-only)",
    )
    _dir_arg(discover_parser)
    discover_parser.add_argument(
        "--user-config", action="store_true",
        help="opt-in: also scan documented user-level config paths",
    )
    discover_parser.add_argument(
        "--probe", metavar="TOOL",
        help="opt-in: run one bounded version probe for this executable",
    )

    recommend_parser = subparsers.add_parser(
        "onboarding-recommend-tools",
        help="project-relevant tool recommendations (static, honest registry)",
    )
    _dir_arg(recommend_parser)
    recommend_parser.add_argument(
        "--category", metavar="TEXT",
        help="project category (e.g. 'Web application') to tailor relevance",
    )

    plan_parser = subparsers.add_parser(
        "onboarding-tool-plan",
        help="build a reviewable install plan (installs nothing)",
    )
    _dir_arg(plan_parser)
    plan_parser.add_argument("tool_id", metavar="TOOL_ID")

    install_parser = subparsers.add_parser(
        "onboarding-tool-install",
        help="execute an approved install plan",
    )
    _dir_arg(install_parser)
    install_parser.add_argument("tool_id", metavar="TOOL_ID")
    install_parser.add_argument(
        "--approve", action="store_true",
        help="explicit operator approval for installing THIS tool (required)",
    )
    install_parser.add_argument(
        "--authorize-install", action="store_true",
        help="second independent authorization gate (required)",
    )

    readiness_parser = subparsers.add_parser(
        "onboarding-readiness",
        help="evaluate the onboarding completion gate",
    )
    _dir_arg(readiness_parser)

    report_parser = subparsers.add_parser(
        "onboarding-report",
        help="generate the structured readiness report",
    )
    _dir_arg(report_parser)
    report_parser.add_argument(
        "--output", metavar="FILE", help="write the Markdown report to a file",
    )
    report_parser.add_argument("--force", action="store_true")


# ---- Step 01 onboarding: command handlers (inserted before main) ----

def _emit_onboarding(doc: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        return
    sys.stdout.write(f"{doc.get('prompt', '')}\n")
    question = doc.get("question")
    if question:
        sys.stdout.write(f"\nQuestion [{question['id']} — {question['category']}]:\n")
        sys.stdout.write(f"  {question['prompt']}\n")
        sys.stdout.write(f"  (why: {question['purpose']})\n")
        if question.get("suggestions"):
            sys.stdout.write("  Suggestions: " + "; ".join(question["suggestions"]) + "\n")
        sys.stdout.write(
            "  Answer with: aperio onboarding-answer DIR "
            f"{question['id']} --answer \"...\" (or 'Not sure yet' / 'Decide later')\n"
        )
    choices = doc.get("choices")
    if choices:
        sys.stdout.write("  Options:\n")
        for choice in choices:
            label = choice.get("label", choice.get("id"))
            sys.stdout.write(f"    - {choice.get('id')}: {label}\n")
    progress = doc.get("progress")
    if progress and progress.get("outstanding_required"):
        sys.stdout.write(
            "  Outstanding required: " + ", ".join(progress["outstanding_required"]) + "\n"
        )


def _onboarding_workspace(args) -> str:
    return getattr(args, "DIR", ".") or "."


def _cmd_onboarding_start(args) -> None:
    _emit_onboarding(_ob_start(_onboarding_workspace(args)), args.as_json)


def _cmd_onboarding_confirm_init(args) -> None:
    if not args.approve:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "action_required": "confirm_initialization",
            "prompt": (
                "Initialization requires explicit approval. The operator must "
                "choose first; re-run with --approve only after they consent. "
                "Nothing was modified."
            ),
        }
        _emit_onboarding(doc, args.as_json)
        return
    doc = _ob_confirm_initialization(_onboarding_workspace(args), intent=args.intent)
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_decline(args) -> None:
    _emit_onboarding(_ob_decline_initialization(_onboarding_workspace(args)), args.as_json)


def _cmd_onboarding_intent(args) -> None:
    apdir = Path(args.DIR) / ".aperio" if args.DIR else Path(".aperio")
    doc = _ob_record_intent(apdir, args.choice, custom_text=args.note)
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_status(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    try:
        session = _ob_read_session(apdir)
    except NotInitializedError:
        session = None
    if session is None:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "action_required": "none",
            "prompt": "No onboarding session in this project.",
        }
    else:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "session_id": session.get("session_id"),
            "stage": session.get("stage"),
            "classification": session.get("classification"),
            "project_category": session.get("project_category"),
            "intent": session.get("intent"),
            "answers_recorded": len(session.get("answers", {})),
            "unresolved_open": len(
                [u for u in session.get("unresolved", []) if u.get("resolved_at") is None]
            ),
            "cancelled": session.get("cancelled"),
            "action_required": "none",
            "prompt": (
                f"Onboarding session {session.get('session_id')} — stage: "
                f"{session.get('stage')}, answers: {len(session.get('answers', {}))}."
            ),
        }
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_answer(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    doc = _ob_answer(apdir, args.question_id, args.answer)
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_revise(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    doc = _ob_revise(apdir, args.question_id, args.answer)
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_pause(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    _emit_onboarding(_ob_pause(apdir), args.as_json)


def _cmd_onboarding_cancel(args) -> None:
    if not args.confirm:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "action_required": "confirm_cancellation",
            "prompt": "Cancellation requires --confirm from the operator. Nothing was modified.",
        }
        _emit_onboarding(doc, args.as_json)
        return
    apdir = (Path(args.DIR or ".") / ".aperio")
    _emit_onboarding(_ob_cancel(apdir), args.as_json)


def _cmd_onboarding_reconstruct(args) -> None:
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    result = _ob_reconstruct_workspace(workspace, discovery)
    apdir = Path(workspace) / ".aperio"
    recorded = False
    if _ob_session_exists(apdir):
        summary = _ob_store_reconstruction(apdir, result)
        recorded = True
    payload = result.to_dict()
    payload["recorded_in_session"] = recorded
    if args.as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    lines = [
        f"Reconstruction of {workspace} ({len(result.documents)} documents, read-only):",
        f"  Documented pending tasks (claims, not approved): {len(result.pending_tasks)}",
        f"  Documented completed claims (unverified): {len(result.completed_claims)}",
        f"  Claim conflicts: {len(result.claim_conflicts)}",
        f"  Untrusted-content risk findings: {len(result.risk_findings)}",
        f"  Topics with no documented coverage: {', '.join(result.missing_topics) or 'none'}",
    ]
    if recorded:
        lines.append("  Summary recorded in the onboarding session (gap interview updated).")
    sys.stdout.write("\n".join(lines) + "\n")
    if args.output:
        from aperio.onboarding_reconstruct import RECONSTRUCTION_FORMAT

        publish_text_atomic(
            Path(args.output), json.dumps(payload, indent=2) + "\n",
            force=args.force, what="reconstruction report",
        )
        print(f"Report written to {args.output}")


def _cmd_onboarding_propose_standardization(args) -> None:
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    reconstruction = _ob_reconstruct_workspace(workspace, discovery)
    proposal = _ob_propose_standardization(discovery, reconstruction)
    apdir = Path(workspace) / ".aperio"
    saved = None
    if require_supported_state_ok(apdir):
        saved = _ob_save_proposal(apdir, proposal)
    doc = {
        "format": "aperio-onboarding-protocol", "version": 1,
        "action_required": "review_proposal",
        "proposal_id": proposal.proposal_id,
        "saved_to": str(saved) if saved else None,
        "prompt": (
            f"Proposal {proposal.proposal_id} built (read-only). Review the "
            "preview, then apply with 'aperio onboarding-apply-standardization "
            f"--proposal {proposal.proposal_id} --approve'. Existing files are "
            "never modified."
        ),
    }
    if args.as_json:
        doc["proposal"] = proposal.to_dict()
        sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(_ob_render_proposal_markdown(proposal))
        sys.stdout.write(f"\n{doc['prompt']}\n")
        if saved:
            print(f"Proposal saved to {saved}")
    if args.output:
        publish_text_atomic(
            Path(args.output), _ob_render_proposal_markdown(proposal),
            force=args.force, what="standardization preview",
        )
        print(f"Preview written to {args.output}")


def _cmd_onboarding_apply_standardization(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    proposal = _ob_load_proposal(apdir, args.proposal)
    if not args.approve:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "action_required": "approve_standardization",
            "prompt": (
                "Applying requires explicit operator approval (--approve). "
                "Nothing was modified."
            ),
        }
        _emit_onboarding(doc, args.as_json)
        return
    _ob_record_approval(apdir, action=_OB_APPLY_ACTION, scope=proposal.proposal_id)
    result = _ob_apply_standardization(apdir, proposal)
    verification = _ob_verify_standardization(
        apdir, proposal,
        {d["path"]: d["sha256"] for d in proposal.existing_documents},
    )
    doc = {
        "format": "aperio-onboarding-protocol", "version": 1,
        "action_required": "none",
        "applied": result,
        "verification": verification,
        "prompt": (
            f"Applied {proposal.proposal_id}. Created: "
            f"{', '.join(result['created'])}. Verification: "
            f"{'all checks passed' if verification['all_ok'] else 'DRIFT REPORTED (see verification)'}"
        ),
    }
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_discover_tools(args) -> None:
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    report = _ob_discover_tools(workspace, discovery, include_user_config=args.user_config)
    if args.probe:
        probed = _ob_probe_tool(report, args.probe)
        report.executables = [
            probed if t.tool_id == args.probe else t for t in report.executables
        ]
    payload = report.to_dict()
    if args.as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    lines = [f"Tool discovery for {workspace} (read-only):"]
    for finding in report.project_local:
        lines.append(f"  [{finding.status}] {finding.tool_id}: {'; '.join(finding.evidence) or finding.detail}")
    installed = [t.tool_id for t in report.executables if t.status == "installed"]
    missing = [t.tool_id for t in report.executables if t.status == "missing"]
    lines.append(f"  Installed on PATH: {', '.join(installed) or 'none'}")
    lines.append(f"  Missing: {', '.join(missing) or 'none'}")
    for host in report.hosts:
        marker = "detected" if host["detected_in_project"] else "not detected"
        lines.append(f"  Host {host['host_id']}: {host['support']} ({marker})")
    for note in payload["notes"]:
        lines.append(f"  Note: {note}")
    sys.stdout.write("\n".join(lines) + "\n")


def _cmd_onboarding_recommend_tools(args) -> None:
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    reconstruction = _ob_reconstruct_workspace(workspace, discovery)
    report = _ob_discover_tools(workspace, discovery)
    category = args.category
    if category is None:
        apdir = Path(workspace) / ".aperio"
        if _ob_session_exists(apdir):
            session = _ob_read_session(apdir)
            category = session.get("project_category")
    recs = recommend_tools(discovery, reconstruction, report, project_category=category)
    if args.as_json:
        sys.stdout.write(json.dumps({"recommendations": recs}, indent=2) + "\n")
        return
    lines = [f"Tool recommendations for {workspace} (category: {category or 'unknown'}):"]
    for rec in recs:
        lines.append(f"  {rec['name']} ({rec['tool_id']}): {rec['purpose']}")
        lines.append(f"    Relevance: {rec['relevance']}")
        lines.append(f"    Compatibility: {rec['compatibility']}")
        lines.append(f"    Source: {rec['declared_source']}")
        lines.append(f"    Plan: aperio onboarding-tool-plan DIR {rec['tool_id']}")
    if not recs:
        lines.append("  (no recommendations for this project)")
    sys.stdout.write("\n".join(lines) + "\n")


def _cmd_onboarding_tool_plan(args) -> None:
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    report = _ob_discover_tools(workspace, discovery)
    plan = _ob_build_install_plan(args.tool_id, report)
    if args.as_json:
        sys.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        return
    lines = [
        f"Install plan for {plan['1_name']} ({args.tool_id}):",
        f"  Purpose: {plan['2_intended_purpose']}",
        f"  Source: {plan['3_source']}",
        f"  Version: {plan['4_candidate_version']}",
        f"  Compatibility: {plan['5_compatibility']}",
        f"  Permissions: {plan['6_required_permissions']}",
        f"  Network: {plan['7_required_network_access']}",
        f"  Command: {plan['8_install_command'] or '(manual-only)'}",
        f"  Security: {plan['9_security_considerations']}",
        f"  Removal: {plan['10_removal']}",
        f"  Manual fallback: {plan['manual_fallback']}",
        f"  Execution: {plan['execution']}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


def _cmd_onboarding_tool_install(args) -> None:
    if not args.approve or not args.authorize_install:
        doc = {
            "format": "aperio-onboarding-protocol", "version": 1,
            "action_required": "approve_install",
            "prompt": (
                "Installation requires BOTH --approve (operator decision) and "
                "--authorize-install. Nothing was executed."
            ),
        }
        _emit_onboarding(doc, args.as_json)
        return
    apdir = (Path(args.DIR or ".") / ".aperio")
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    report = _ob_discover_tools(workspace, discovery)
    plan = _ob_build_install_plan(args.tool_id, report)
    _ob_record_approval(apdir, action=_OB_INSTALL_ACTION, scope=args.tool_id)
    record = _ob_execute_install(apdir, plan, authorize_install=True)
    doc = {
        "format": "aperio-onboarding-protocol", "version": 1,
        "action_required": "none",
        "record": record,
        "prompt": (
            f"Install command for {args.tool_id} completed. Installed state is "
            "NOT verified by this result: re-run 'aperio onboarding-discover-tools' "
            "to reassess honestly."
        ),
    }
    _emit_onboarding(doc, args.as_json)


def _cmd_onboarding_readiness(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    session = _ob_read_session(apdir)
    result = _ob_evaluate_readiness(session)
    if args.as_json:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2) + "\n")
        return
    lines = [
        f"Onboarding readiness: {result.outcome}",
    ]
    lines += [f"  - {r}" for r in result.reasons]
    if result.outstanding_required:
        lines.append(f"  Outstanding required: {', '.join(result.outstanding_required)}")
    if result.open_critical:
        lines.append(f"  Open critical: {', '.join(result.open_critical)}")
    sys.stdout.write("\n".join(lines) + "\n")


def _cmd_onboarding_report(args) -> None:
    apdir = (Path(args.DIR or ".") / ".aperio")
    session = _ob_read_session(apdir)
    workspace = _onboarding_workspace(args)
    discovery = _ob_discover_workspace(workspace)
    reconstruction = _ob_reconstruct_workspace(workspace, discovery)
    tools_report = _ob_discover_tools(workspace, discovery)
    readiness = _ob_evaluate_readiness(session)
    recommendations = _ob_recommend_tools(
        discovery, reconstruction, tools_report,
        project_category=session.get("project_category"),
    )
    report = _ob_build_report(
        session,
        discovery=discovery,
        reconstruction=reconstruction,
        tools_report=tools_report,
        recommendations=recommendations,
        readiness=readiness,
    )
    if args.as_json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return
    markdown = _ob_render_report_markdown(report)
    sys.stdout.write(markdown)
    if args.output:
        publish_text_atomic(
            Path(args.output), markdown, force=args.force, what="onboarding report"
        )
        print(f"Report written to {args.output}")


def require_supported_state_ok(apdir) -> bool:
    try:
        _ob_require_supported_state(apdir)
        return True
    except AperioError:
        return False


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
        elif args.command == "preflight":
            _cmd_preflight(args)
        elif args.command == "quality-check":
            _cmd_quality_check(args)
        elif args.command == "feedback-check":
            _cmd_feedback_check(args)
        elif args.command == "feedback-review":
            _cmd_feedback_review(args)
        elif args.command == "feedback-lesson":
            _cmd_feedback_lesson(args)
        elif args.command == "feedback-compare":
            _cmd_feedback_compare(args)
        elif args.command == "improvement-apply":
            _cmd_improvement_apply(args)
        elif args.command == "exchange-export":
            _cmd_exchange_export(args)
        elif args.command == "exchange-import":
            _cmd_exchange_import(args)
        elif args.command == "agent-brief":
            _cmd_agent_brief(args)
        elif args.command == "benchmark-cases":
            _cmd_benchmark_cases(args)
        elif args.command == "benchmark-run":
            _cmd_benchmark_run(args)
        elif args.command == "capture":
            _cmd_capture(args)
        elif args.command == "inbox":
            _cmd_inbox(args)
        elif args.command == "inbox-resolve":
            _cmd_inbox_resolve(args)
        elif args.command == "checkpoint-write":
            _cmd_checkpoint_write(args)
        elif args.command == "checkpoint-show":
            _cmd_checkpoint_show(args)
        elif args.command == "recover":
            _cmd_recover(args)
        elif args.command == "plan-propose-from-goal":
            _cmd_plan_propose_from_goal(args)
        elif args.command == "orchestrate-assess":
            _cmd_orchestrate_assess(args)
        elif args.command == "orchestrate-plan":
            _cmd_orchestrate_plan(args)
        elif args.command == "orchestrate-brief":
            _cmd_orchestrate_brief(args)
        elif args.command == "mem-propose":
            _cmd_mem_propose(args)
        elif args.command == "mem-review":
            _cmd_mem_review(args)
        elif args.command == "mem-list":
            _cmd_mem_list(args)
        elif args.command == "mem-show":
            _cmd_mem_show(args)
        elif args.command == "mem-recall":
            _cmd_mem_recall(args)
        elif args.command == "mem-export":
            _cmd_mem_export(args)
        elif args.command == "mem-import":
            _cmd_mem_import(args)
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
        elif args.command == "onboarding-start":
            _cmd_onboarding_start(args)
        elif args.command == "onboarding-confirm-init":
            _cmd_onboarding_confirm_init(args)
        elif args.command == "onboarding-decline":
            _cmd_onboarding_decline(args)
        elif args.command == "onboarding-intent":
            _cmd_onboarding_intent(args)
        elif args.command == "onboarding-status":
            _cmd_onboarding_status(args)
        elif args.command == "onboarding-answer":
            _cmd_onboarding_answer(args)
        elif args.command == "onboarding-revise":
            _cmd_onboarding_revise(args)
        elif args.command == "onboarding-pause":
            _cmd_onboarding_pause(args)
        elif args.command == "onboarding-cancel":
            _cmd_onboarding_cancel(args)
        elif args.command == "onboarding-reconstruct":
            _cmd_onboarding_reconstruct(args)
        elif args.command == "onboarding-propose-standardization":
            _cmd_onboarding_propose_standardization(args)
        elif args.command == "onboarding-apply-standardization":
            _cmd_onboarding_apply_standardization(args)
        elif args.command == "onboarding-discover-tools":
            _cmd_onboarding_discover_tools(args)
        elif args.command == "onboarding-recommend-tools":
            _cmd_onboarding_recommend_tools(args)
        elif args.command == "onboarding-tool-plan":
            _cmd_onboarding_tool_plan(args)
        elif args.command == "onboarding-tool-install":
            _cmd_onboarding_tool_install(args)
        elif args.command == "onboarding-readiness":
            _cmd_onboarding_readiness(args)
        elif args.command == "onboarding-report":
            _cmd_onboarding_report(args)
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
