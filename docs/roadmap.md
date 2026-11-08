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
  new run IDs, and additive memory integration. Aperio manages and
  records execution — it does not implement model integrations, and
  ``--authorize-exec`` is a procedural human-in-the-loop checkpoint, not
  a sandbox.

Everything below is future work. Nothing in this list works today; do not
claim otherwise in code, docs, or demos. Each item lands as its own
assignment ("one feature per assignment").

## Feature 05 — Cross-machine portability tooling

Export/import of the shareable subset of `.aperio/` (explicitly excluding
`local/`), integrity checks, and a defined upgrade path between schema
versions (state, handoff, and plan schemas).

## Later / exploratory

- Role specializations (planner, implementer, reviewer) as pluggable
  policies — no model/vendor coupling
- Integration adapters for external agent runtimes
- Project archival and history summaries derived from the append-only log

Stability rule: each feature must keep every prior safety invariant from
`docs/conventions.md` true, with tests, before it is called done.
