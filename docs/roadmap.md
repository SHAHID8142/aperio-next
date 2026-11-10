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

- **Former provider release blocker — CLOSED in 0.17.1:** the Feature 07
  metadata-bound mismatch (`adapters._bounded_usage()` accepted up to
  `10**12` while `runtime._validate_provider_result()` rejected values
  above `1_000_000_000`; Claude `num_turns`/`duration_ms` shared it) is
  resolved with one shared contract: `adapters.MAX_PROVIDER_METADATA_VALUE`
  / `MAX_PROVIDER_COUNT_VALUE` / `MAX_PROVIDER_COST_USD` are the single
  definition both the adapter parsers and the runtime persistence
  validator use. Beyond-contract magnitudes are recorded as honest
  absence, never persisted as values that would fail validation, so an
  adapter-accepted result can no longer strand a run. Boundary and
  end-to-end tests: `tests/test_provider_metadata_bounds.py`. See
  `docs/feature-07.md`.

Everything below is future work. Nothing in this list works today; do not
claim otherwise in code, docs, or demos. Each item lands as its own
assignment ("one feature per assignment").

## M3 — universal portable memory (implemented in 0.10.0)

M3 was originally framed as reviewed goal hardening. That hardening landed in
0.9.1/0.9.2 (output publication, list bounds, profile types, instruction
authority, attachment forwarding). The portable-memory foundation authorized
afterwards is what 0.10.0 delivers:

- memory-schema v1 records with content-derived identity and bounded fields;
- append-only JSONL stores with chain-hash corruption detection, advisory
  locking, atomic creation, and damaged-line reporting;
- explicit trust states (`proposed`, `attested`, `verified`, `superseded`,
  `retracted`, derived `expired`) with append-only review transitions and no
  automatic promotion;
- deterministic retrieval (scope, keyword overlap, review state, freshness,
  budget) with explicit exclusion reasons and conflict reporting;
- optional context-pack integration through `--memory-store`, placed after
  operator context and never able to override it;
- versioned portable exchange (`aperio-memory-export` v1) that preserves
  identity and provenance while importing everything as `proposed`;
- standalone operation with no project, provider, or model API.

## M3 release gate (0.10.1)

The independent gate review confirmed three P1 defects in the 0.10.0 memory
foundation; all three are corrected in 0.10.1:

- **P1-A — verification without support.** `--verify` accepted no evidence
  and no reviewer, so an unsupported claim could be labelled verified by
  invoking the command. Verification now requires an evidence reference and
  a declared reviewer, refuses a reviewer identical to the declared proposer,
  and reports its own basis (`evidence`, `unrecorded`, `not-verified`).
  Pre-gate `verified` records stay readable and are reported as `unrecorded`
  with an `attested` trust label, never silently re-promoted.
- **P1-B — expiry not applied in normal use.** Validity was only evaluated
  when an optional `as_of` was supplied. Reads now default to today (UTC),
  validate the date strictly, apply inclusive `valid_from`/`valid_until`
  bounds to recall, packs, and exports, and count `expired`/`future`
  exclusions visibly.
- **P1-C — unchecked append.** A single unchecked `os.write` could lose or
  truncate an event while reporting success. Appends now open the store
  without following symlinks, prove the descriptor is the validated file,
  re-verify the chain through that descriptor, retry short writes, fail on
  zero-byte writes and I/O errors, and refuse further appends after damage.
  `fsync` is issued; crash/power-loss atomicity is explicitly not promised.

The same cycle closed a privacy-relevant model limit: content-derived identity
excluded `sensitivity` and validity windows, so re-proposing identical text
with a stricter classification was silently ignored. That now fails closed,
and imports report such a mismatch as a conflict.

## M3 finalization (0.11.0)

The completed M3 release closes the remaining consistency gaps found while
auditing every consumer of recorded state, trust, and validity:

- **Effective trust is derived, not asserted.** A `verified` state is reported
  as verified only when the recorded transition carries an evidence reference,
  a reviewer identifier, and different declared identifiers for proposer and
  reviewer. Anything else keeps its history untouched but is published as
  `attested` with `verification.basis: unrecorded` and the exact reasons —
  consistently in snapshots, ranking, JSON, Markdown, packs and exchange
  files. Neither a missing reviewer nor a self-review counts as support.
- **Exchange honesty.** Validation accepts the two combinations a real store
  can produce (`verified`/`verified`, `verified`/`attested`) and rejects
  forged ones; imports report `unsupported_trust_claims` and carry any
  transferred review evidence under `transfer_evidence`; imported memories are
  always `proposed`.
- **UTC evaluation.** The default evaluation date comes from
  `datetime.now(timezone.utc)`, so results no longer depend on the machine's
  timezone; `--as-of` stays deterministic, bounds stay inclusive, and packs
  re-check the window instead of trusting the caller.
- **Durability and privacy re-audited:** 5 concurrent identical proposals
  produce one record and four reported duplicates with no damage; the size cap
  refuses rather than truncates; owner-only permissions stay in place.

What this release still does **not** claim: the hash chain is corruption
detection, not authentication; the absence of complete trailing records cannot
be detected without an external anchor; `--actor` identifiers and evidence
references are declarations, not proof; a reviewer may be honestly but not
independently chosen; no write path promises crash or power-loss atomicity;
and the Feature 07 provider metadata bound defect remains out of scope.

## M3 trust boundary (0.12.0)

The trust/provenance review of the M3 completion found four entry-point gaps
and closed them with one rule rather than four patches:

- **Supplied labels could promote a record.** Retrieval read a caller's
  `trust` string before checking it against the recorded history, so a record
  claiming `state: proposed` with `trust: verified` was ranked as verified.
  Every entry point now resolves trust through `memstore.resolve_trust()`,
  which derives the verification basis from the record's own review events and
  refuses a claim the history does not support. A self-declared
  `verification.basis: evidence` is not proof of a review.
- **Context items could manufacture a verified memory.** The packer accepted
  any item with `source: memory` and an enumerated trust label. It now
  re-validates identity (`memory_id` ↔ `source_ref`), recomputes the content
  hash and rejects tampering, requires the evidence contract behind a
  `verified` label (downgrading it otherwise with a warning), refuses
  operator/authoritative labels on memory, and forces `private` records to
  local-only.
- **A cached validity answer could outlive the window.** `memory_context_items`
  carried `validity.current` from an earlier read, and the packer only checked
  that boolean. The packer now owns the date: `--memory-as-of` (default today
  UTC) is validated, applied to every item, recorded in
  `content.memory.validity`, and a differing earlier date produces
  `memory_validity_reevaluated` instead of a silent mix.
- **Repeated import lost the original source.** Import replaced the source
  object, discarding the original reference. Import now keeps the immediate
  provenance as `kind: import` while preserving the original `ref` and quoting
  the original kind, note and declared state inside the bounded source note,
  and reports both structurally under `transfer_provenance`. Identity, scope,
  sensitivity and evidence are unchanged, and the record still enters as
  `proposed`.

## M3 trust-consumption completion (0.13.0)

A second review of the trust boundary found that two entry points still let a
claim stand where only a recorded transition should:

- **The pack believed metadata.** `context_pack` accepted a memory item's
  `verification_basis`, `verification_evidence` and
  `reviewer_distinct_from_proposer` as the evidence contract. A caller-built
  item with those three fields set therefore entered a pack as `verified`
  without any review transition. Items now carry their review history, and the
  pack re-derives trust from it with the same function the store uses; a label
  above the derivation is rewritten down with `memory_item_trust_downgraded`,
  and the display facts (basis, evidence, reviewer, reasons) are taken from
  the derivation rather than from the claim.
- **A declaration could stand in for a lost history.** `resolve_trust()`
  accepted "the history was truncated" plus a coherent-looking verification
  block as proof. Snapshots now keep the whole history, so a real review can
  always be re-derived, and the substitution path is gone: a `verified` state
  whose transitions do not include the verification reads `unrecorded` /
  `attested` with the reason stated. Declared bases and labels may only repeat
  or understate the derivation, never exceed it.
- **One derivation, one contract.** `_verification()` is no longer called from
  three places with three notions of proof: `derive_verification()` is the only
  entry, used by snapshots, recall, `memory_context_items()`, both pack
  builders and the exchange validator. Exchange entries still import as
  `proposed`, with claimed trust never applied.
- **Conflicting and withdrawn memory is excluded, not interpreted.** A pack
  drops an item whose claimed state disagrees with its transitions
  (`memory_state_inconsistent`), and retracted or superseded memory
  (`memory_not_active`); `memory_context_items()` refuses the latter outright.
- **Packs say where trust came from.** `content.memory.trust_derivation` and
  `trust_note` state that labels are re-derived from recorded review
  transitions and that hashes are integrity checks, not authentication of the
  producer.

## M3 state-proof closure (0.13.1)

The final-master audit of the M3 trust boundary found that the 0.12/0.13 rule
("authority comes from recorded review events") was enforced for `verified`
but not one tier lower:

- **An `attested` claim needed no `accept` event.** A caller-built record
  with `state: "attested"` and an empty (or accept-free) history received
  attested authority everywhere: the derivation mapped the state to its label
  unconditionally, retrieval ranked it above proposed and let it use the
  reviewed-memory global-scope bypass, and both pack builders labelled it
  attested. The derivation now requires the recorded transition that
  produces every active state: an unsupported `attested` claim is downgraded
  to `proposed` with `verification.basis: unrecorded` and the reason stated,
  consistently in `resolve_trust`, retrieval, context items, both pack
  builders and the effective-trust rule. A declared `attested` label over
  that history is refused, like any other self-promoting claim. Genuine
  store records are unaffected — replay always carries the transition — and
  the documented `verified`-without-proof → `attested` legacy behaviour is
  unchanged.
- **A claimed state its own history contradicts is no longer supportable.**
  The derivation now reports `verification.replayed_state` (the state the
  record's event log ends in). A claimed recorded state that disagrees with
  it is reported as `unrecorded` with the contradiction named; retrieval
  excludes a record whose replay ends retracted/superseded regardless of the
  claimed state, and context-item conversion refuses it — the pack's
  `memory_state_inconsistent` drop already covered packs.
- Recall's `memory_verification_unrecorded` warning and the pack's
  `memory_item_trust_downgraded` warning were generalised to cover state
  claims, and `memory_context_items` derives its `reviewed` flag from the
  derived trust instead of the claimed state name.

Exchange files are unchanged: an `attested`-claiming entry is still accepted
as data (a truncated genuine export cannot be distinguished from a forged
one — although an `attested` record can have at most one review event, so a
history-less attested entry is always forged) and always imports as
`proposed`; nothing is promoted either way.

## M7 — optional agent/provider integration and domain adaptation (implemented in 0.17.0)

`aperio agent-brief GOAL [--memory-store F]` assembles the portable
guidance package from the validated operator objective, explicit
requirements, relevant reviewed memory (attested/verified only — proposed
memory is listed separately as unverified, private memory never enters),
accepted lessons, applicable quality criteria plus the profile's new
advisory `suggested_checks`, unresolved uncertainties, the output
contract, and the trust boundaries. Only an operator-declared goal can
become a brief — agent/import-declared goals are refused with an
actionable message until the operator re-declares them. No provider is
contacted; pasting is the transport. Domain adaptation is strengthened as
data: all six profiles now carry advisory suggested self-checks rendered
under an explicit advisory heading, still never gating anything. The
manual copy/paste workflows for ChatGPT/Claude/Gemini/GLM-class agents
are documented and machine-tested (docs/agent-workflows.md), and provider
adapter support, safety model, and the explicit statement that live
provider end-to-end execution is NOT verified are consolidated in
docs/providers.md. Provider integrations stay optional, fail closed
before any mutation, and never bypass trust or governance.

## M6 — universal portable exchange (implemented in 0.16.0)

`aperio exchange-export` and `aperio exchange-import` add the second-
generation exchange facility: one `aperio-universal-exchange` v1 document
whose sections are already-versioned contracts (goal-schema v1,
`aperio-context-pack` v1, `aperio-quality-report` v1, feedback-schema v1,
`aperio-memory-export` v1, bounded operator notes), each validated by its
owning contract at export AND import, each carrying a content digest, the
whole document digest-checked and deterministically ordered. Selection is
explicit (named files; memories need `--memory-id`/`--memory-tag`).
Imports preview by default, write `imported-<kind>-<digest>.json` files
that never overwrite existing bytes (identical content reports
`already-present`; a different-content occupant is a refused conflict),
bring memories in as `proposed` through the ordinary M3 import rules
(duplicates, conflicts, and unsupported trust claims all reported), and
warn on every imported goal that it is untrusted input pending review and
provenance re-declaration. A newer format version or unknown section kind
fails closed. The Feature 05 project archive v1 and the standalone M3
memory exchange v1 are unchanged. See `docs/exchange.md`.

## M5 — controlled feedback and improvement (implemented in 0.15.0)

`aperio feedback-check`, `feedback-review`, `feedback-lesson`,
`feedback-compare`, and `improvement-apply` implement the controlled loop
task → output → quality issue → feedback → approved lesson → future task →
measurable comparison. Feedback documents (feedback-schema v1) start as
proposals with content-derived identity; a decided document is final and
`--replace` is a revision CAS. Only an explicit accepted decision produces
a lesson, and the lesson is written into the operator-named M3 store as a
NEW PROPOSED memory — the M3 review contract remains the only memory
authority, so nothing is promoted by feedback, repetition, or agent
self-report (which is flagged, never silent). `feedback-compare`
deterministically compares two quality reports for the same goal: only
confirmed/warning findings count as resolved, new confirmed/warning
findings are potential regressions and are always reported, and the report
states it cannot attribute causes. Improvement proposals
(improvement-proposal v1) target goal instruction fields and
quality-request fields, apply to a new copy through the atomic publication
path, refuse no-ops, and cannot modify the code-owned profile registry.
See `docs/feedback.md`.

## M4 — universal mistake prevention and quality evaluation (implemented in 0.14.0)

`aperio preflight goal.json` and `aperio quality-check goal.json
--deliverable FILE` deliver the quality engine as one deterministic engine
over a declarative rule registry (quality-rules v1) and a stable mistake
taxonomy (taxonomy v1). Findings carry rule id, category, severity, status
(`confirmed` / `warning` / `unknown` / `recommendation`), the requirement
they came from, bounded evidence, an explanation, and a corrective action;
issue identity and the report's `content_sha256` are reproducible from the
inputs. Preflight covers unclear/missing expectations, conflicting or
duplicated instructions, untrusted goal provenance, unvalidated
assumptions, oversized instruction lists, and — with `--memory-store` —
expired, unreviewed, or conflicting memory (read-only). Deliverable checks
cover operator-named sections/texts, modal constraint phrases, stated
length bounds, output-format expectations, deliverable coverage, source
attribution under evidence requirements, limitation language, and a
software-profile verification-evidence rule. Statuses are honest: absence
of a mention is a warning, never a confirmed failure; cited sources are
reported as `unknown` support, never verified. No memory writes, no
content alteration, no governance authority. See `docs/quality.md`.

## Next (requires independent review and explicit instruction)

- Remaining M3 hardening whatever the review confirms (for example goal
  persistence/provenance review, or importing a validated goal into an
  existing plan with an explicit, non-destructive adoption path). Do not start
  M4 before M3 is independently reviewed.

## Later / exploratory

- Role specializations (planner, implementer, reviewer) as pluggable
  policies — no model/vendor coupling
- Additional provider CLIs behind the same contract (only after official
  documented non-interactive interfaces are verified)
- Project archival and history summaries derived from the append-only log

Stability rule: each feature must keep every prior safety invariant from
`docs/conventions.md` true, with tests, before it is called done.
