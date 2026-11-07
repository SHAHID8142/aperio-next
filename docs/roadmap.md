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

## Planned — NOT implemented

Everything below is future work. Nothing in this list works today; do not
claim otherwise in code, docs, or demos. Each item lands as its own
assignment ("one feature per assignment").

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
