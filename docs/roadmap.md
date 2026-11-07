# Roadmap — planned, NOT implemented

Everything below is future work. Nothing in this list works today; do not
claim otherwise in code, docs, or demos. Each item lands as its own
assignment ("one feature per assignment").

## Feature 02 — Agent-facing project memory

Structured read/write APIs over `.aperio/` so agent runtimes can consume and
update project state programmatically: state updates with audit trail,
structured decision-log entries, handoff validation.

## Feature 03 — Task planning & tracking

A task/plan schema under `.aperio/` (versioned like state), with explicit
lifecycle states and agent ownership, so multiple agents can coordinate
without stepping on each other.

## Feature 04 — Multi-agent orchestration runtime

Assignment of tasks to agent runtimes (model-agnostic), result collection,
and conflict rules (one writer per file, sequential handoffs).

## Feature 05 — Cross-machine portability tooling

Export/import of the shareable subset of `.aperio/` (explicitly excluding
`local/`), integrity checks, and a defined upgrade path between schema
versions.

## Later / exploratory

- Role specializations (planner, implementer, reviewer) as pluggable
  policies — no model/vendor coupling
- Integration adapters for external agent runtimes
- Project archival and history summaries derived from the append-only log

Stability rule: each feature must keep every prior safety invariant from
`docs/conventions.md` true, with tests, before it is called done.
