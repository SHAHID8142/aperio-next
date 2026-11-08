# Roadmap

## Implemented

- **Feature 01 — Project foundation:** `aperio --version|init|status`,
  versioned `.aperio/` state, safe-path policy, atomic non-destructive
  persistence.
- **Feature 02 — Portable agent memory & reliable handoff:**
  `aperio memory [--json]` context read API, `aperio decision-add`
  (append-only, injection-hardened), `aperio handoff-write` (validated
  structured handoff, optional compare-and-swap), `aperio phase-set`
  (compare-and-swap phase updates). The append-only decision log is the
  project's audit trail; phase transitions are not auto-logged into it.
- **Feature 03 — Project planning, task graph & execution tracking:**
  versioned plan document (`.aperio/plan.json`, plan-schema v1) with
  `aperio plan-create/plan-show/plan-approve/plan-revise`, a
  dependency-validated task graph (cycle detection, derived readiness),
  an approval gate for execution tracking, and an evidence-based task
  lifecycle via `aperio task-list`/`aperio task-status`. Plans and tasks
  are managed — agents are not executed by aperio.

- **Feature 04 — Model-agnostic agent execution & orchestration runtime:**
  adapter contract with two real adapters (manual handoff, configured
  local-process), versioned execution records under coordination locks,
  approval/eligibility gates, sequential deterministic dispatch with a
  dispatch-scope lock, bounded-output subprocess execution with explicit
  ``--authorize-exec`` authorization, crash reconciliation, retry with
  new run IDs, and additive memory integration. At this stage Aperio
  manages and records execution without invoking a model provider;
  Feature 07 adds documented CLI integrations. ``--authorize-exec`` is a
  procedural human-in-the-loop checkpoint, not a sandbox.

- **Feature 05 — Cross-machine portability, integrity & schema-upgrade
  foundation:** `aperio export` (validated, whitelisted ZIP archive with a
  versioned SHA-256 manifest), `aperio import` (full pre-write validation,
  conflict-preserving, `--replace` approval, lock + rollback), `aperio
  integrity` (read-only consistency report, optional archive check), and
  `aperio upgrade` (dry-run preview, explicit migration registry, backups,
  refusal of future versions). `.aperio/local/` never travels, and
  execution metadata is machine-neutral in transit.

- **Feature 06 — Agent roles, workflow policies & independent review
  gates:** opt-in `policies.json` with planner/implementer/reviewer
  responsibilities, declared actor→role mapping with preserved history,
  separation-of-duties enforcement at Python mutation boundaries, immutable
  `REV-NNNN` review records, verification-plus-review completion gates, an
  append-only actor audit log, governance in portable memory, and policy/
  review/audit integration with export, import, and integrity. Safety gate:
  fail-closed durable plan authorship with explicit adoption/repair,
  attempt-bound reviews, implementer-based reviewer independence, and a
  narrow startup-recovery path with no public completion bypass.

- **Feature 07 — Real AI agent integrations & provider-neutral CLI
  bridge:** provider-neutral `CliAgentAdapter` plus documented
  non-interactive `codex-cli` (`codex exec --json`) and `claude-cli`
  (`claude -p`) adapters. Prompts travel on stdin, arbitrary provider
  `args` are refused, capability probes happen before authorized launch,
  and two independent consents separate process execution from
  workspace-write. Both providers default to read-only; Claude derives
  restrictive `--tools`/`--allowedTools` from one mode. Structured
  JSONL/JSON results are reduced to neutral metadata; bounded pipe cleanup
  prevents descendant-held-pipe hangs. GUI applications (ZCode, Cursor)
  remain explicitly out of scope. See `docs/feature-07.md`.

- **M1 — Universal contracts and read-only context pack (implemented):**
  `aperio context-pack [workspace]` assembles portable context for any
  knowledge-work task, standalone or from an existing Aperio workspace.
  The default `general` profile is domain-neutral; `software` is optional.
  The pack carries provenance/trust labels, explicit completeness,
  deterministic whole-item byte budgeting, Markdown and JSON from one
  canonical payload, reproducible content hashes, imported/proposed
  context labels, and strict read-only/privacy boundaries. Review context is
  `verified` only while the approval is still applicable to the current plan
  revision, work attempt, and task revision; stale approvals are excluded
  and newer pending/rejected decisions appear as attested decisions. No
  model API, provider launch, schema migration, canonical memory ledger, or
  automatic performance claim is introduced.

- **M2 — Universal goal, work & task-profile generalization (implemented):**
  `aperio goal-check`, `aperio goal-pack`, and `aperio profiles` introduce
  goal-schema v1: a versioned, validated, provider-neutral description of
  any knowledge-work assignment (objective, outcome, profile, constraints,
  assumptions, uncertainties, risks, audience, deliverables, quality and
  acceptance criteria, output contract, evidence requirements, optional work
  decomposition, provenance/trust). Two modes work: standalone (no
  `.aperio/`, plan, provider, model API, or code) and project-backed
  (read-only reuse of M1 context). Six data-only profiles exist; `general`
  stays the default and their criteria remain advisory. Work graphs reuse
  the plan-schema v1 dependency validator and are optional, so a single
  question needs no graph. Legacy plans, reviews, decisions, handoffs, and
  the portable archive are untouched; there is no migration and no new
  mandatory project file. See `docs/goal-contract.md`.
  M2 gate fixes: `--output` now publishes through an exclusively created
  temporary file with atomic no-clobber semantics (no symlink following, no
  truncation, no concurrent overwrite, `.aperio/` and goal-file destinations
  refused); goal requirements and constraints keep separate 50-item caps so
  every valid goal converts; and a non-string `profile` is a normal
  validation error rather than an exception. Second gate: `--output` no
  longer degrades to a check-then-replace fallback (atomic no-clobber or a
  fail-closed error), and agent/imported goal instructions are attributed
  under "Proposed goal instructions" instead of appearing as operator
  requirements.

- **Known provider release blocker (unresolved, not part of M1/M2):**
  Feature 07 has a P1 metadata-bound mismatch. `adapters._bounded_usage()`
  accepts values up to `10**12`, while
  `runtime._validate_provider_result()` rejects usage values above
  `1_000_000_000`; Claude `num_turns`/`duration_ms` share the mismatch. A
  structurally valid provider result can therefore strand a run after the
  child exits. Provider execution is not release-ready until this is fixed
  with boundary tests. M1 does not change or silently resolve it.

Everything below is future work. Nothing in this list works today; do not
claim otherwise in code, docs, or demos. Each item lands as its own
assignment ("one feature per assignment").

## Next (requires independent review and explicit instruction)

- **M3 — reviewed universal goal hardening:** whatever the M2 review
  confirms (for example goal persistence/provenance review, or importing a
  validated goal into an existing plan with an explicit, non-destructive
  adoption path). Do not start before M2 is independently reviewed.

## Later / exploratory

- Role specializations (planner, implementer, reviewer) as pluggable
  policies — no model/vendor coupling
- Additional provider CLIs behind the same contract (only after official
  documented non-interactive interfaces are verified)
- Project archival and history summaries derived from the append-only log

Stability rule: each feature must keep every prior safety invariant from
`docs/conventions.md` true, with tests, before it is called done.
