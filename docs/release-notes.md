# Release notes

## 1.2.0rc2 — hardening audit (final)

An independent, evidence-driven audit of `main` @ `9773ba4` produced fixes for
fourteen findings (three critical/high severity areas), a modernized
installation path, and a rewritten beginner README. Full detail, per-finding
reproduction and evidence: [audit-final.md](audit-final.md).

Correctness and security fixes:

- **Bounded subprocess output** everywhere Aperio runs an external tool —
  limits are enforced *while* output is produced, not after buffering.
- **Windows-safe process termination** — the process-group kill is POSIX-only
  and platform-guarded; a vanished lock is no longer misread as a non-file
  occupant (a real concurrency TOCTOU that caused intermittent intake failures).
- **Complete prerequisite validation** for tool installs (`node`+`npm`+`npx`
  are all required) and **approval bound to the exact plan fingerprint**, so an
  approved plan cannot be swapped for a different command.
- **Secret redaction** before any tool output reaches shareable session state.
- **Bounded-task gate:** project-scale goals that are not decomposed into
  verifiable tasks are refused at the plan bridge instead of collapsing into
  one self-certified task.
- **Task-level retry ceiling** so varying error text or adapter switches cannot
  reset the per-signature budget.
- **Incremental directory scanning** and byte-bounded file reads.
- **Opt-in interactive wizard** (`aperio start`) for beginners; `aperio next`
  stays read-only and scriptable.
- **Honest host compatibility:** each host row now records six separate
  verification facts, so a passive probe is never read as a live integration.
- **Installation modernized** to `uv tool install` (verified from wheel and an
  immutable git commit) with a `pipx` fallback; all verified on macOS arm64.
- **Final remediation:** `aperio start` asks for initialization consent on an
  existing project instead of crashing; install approval is bound to the exact
  reviewed plan (an unbound legacy approval is refused); and the atomic
  task-size rule is enforced at goal assessment, plan-create, plan-approve and
  dispatch, so it cannot be bypassed.
- **Wizard consent fails closed:** a missing or failing approval callback is a
  refusal, never an implicit "yes".
- **Whole sections are not atomic:** an entire Hero Section (or any feature
  unit spanning several implementation areas) is refused as one task, while
  bounded slices — markup-only, styling-only, a copy change — stay valid.
- **Bounded subprocess drain:** a descendant that keeps a pipe open can no
  longer hold a call past its deadline (`drain_grace_seconds`, `drain_stalled`).
- **Wizard `/back` re-prompts:** going back withdraws the previous answer
  instead of overwriting it with a placeholder.
- **Bounded work is not confused with its location:** "Fix a typo in the
  website" bridges normally, while "an entire website" stays blocked.
- **One atomicity rule for both schemas:** plan tasks are checked through
  `acceptance_criteria` as well as `completion_criteria`.
- **Tests are not treated as scope:** requiring "unit tests pass" no longer
  makes a bounded task look like a whole feature.

Verification: **1,688 tests** pass on CPython 3.11, 3.12, 3.13 and 3.14
(macOS arm64). GitHub-hosted CI still has not executed (account billing lock);
Windows and Linux remain UNVERIFIED by this audit.

## 1.2.0rc2 — Step 01 integration release candidate

This candidate integrates the two previously separate completion lines:
the v1.2 roadmap/recovery/host-compatibility work and the complete Step 01
universal-onboarding implementation.

- **Canonical entry:** `aperio next [DIR] [--json]` is a strictly read-only
  route through the onboarding facade. It classifies or resumes the workspace
  and returns exactly one next action; it never initializes or installs.
- **Universal onboarding:** all 18 `onboarding-*` commands are included:
  discovery, durable sessions, adaptive interview, reconstruction,
  standardization, tool discovery/recommendation, two-gate installation,
  readiness, and reporting.
- **Integrated history:** `feature/master-v3-completion` and
  `feature/step01-universal-onboarding` were merged on a dedicated integration
  branch before main; the only textual merge conflict was the README document
  index, where both document entries were retained.
- **Release identity:** version advanced from 1.2.0rc1 to 1.2.0rc2 so the
  integrated artifact cannot be confused with the earlier unintegrated RC.
- **Verification:** 1601 passed with no skips on Python 3.14.7 after
  integration. The audit removed a stale Step 01 schema-compatibility skip by
  updating its fixture to the current task contract, restoring coverage of
  reconstruction from a real Aperio plan.

The external verification limits remain: GitHub-hosted CI has not executed,
live provider calls were not run, and Windows/Linux/Python 3.11/3.13 were not
tested in this integration session.

## 1.2.0rc1 — master-directive v3 release candidate

The third master directive (product integration, universal agent
compatibility, secure autonomous workflows, release readiness) delivered,
on top of 1.1.0rc1, on the `feature/master-v3-completion` branch:

- **Two-level planning** (docs/planning.md) — `aperio roadmap-create /
  roadmap-show / roadmap-approve / roadmap-revise` add the LEVEL-1
  master roadmap (roadmap-schema v1: lifecycle, milestones with
  capabilities/dependencies/risks/acceptance conditions) above the
  existing detailed plan, which remains the only execution engine.
  Roadmaps are draft-until-approved with the same governance,
  attribution, and separation-of-duties rules as plans; there is no
  second planner and no stored milestone progress (progress is derived
  from which milestone the current plan implements).
- **plan-schema v3** — plans may optionally declare the master-roadmap
  milestone they implement (`plan-create --milestone MILESTONE-1`).
  v1/v2 documents remain fully readable without migration; the
  explicit v2→v3 upgrade edge re-stamps schema_version only; portable
  archives from 1.1.0rc1 import unchanged.
- **Bounded retry with problem signatures** (anti-loop protection) —
  every failed run is keyed by a deterministic problem signature; at
  most **3 attempts** are allowed per signature; retries require a
  declared `--remediation` (new evidence or a changed hypothesis, with
  verbatim repeats refused), and the 4th attempt on the same signature
  is refused with escalation guidance. New read-only
  `aperio recovery-report` shows the debugging history. Enforced in
  the Python boundary, not by prompt instructions.
- **Host compatibility doctor** — `aperio host-doctor [--probe]`
  reports the honest six-level compatibility vocabulary per documented
  host. `native_verified` and `experimental` are defined but **never
  emitted** by this version (no verified native integration exists and
  none is invented); provider CLIs get a passive executable/flags
  probe; every cooperative host names its canonical-CLI fallback.
- **Fresh-machine install path** (docs/install.md) — prerequisites,
  venv recipes for macOS/Linux/Windows, wheel sourcing, first checks,
  upgrade and uninstall guidance, and platform honesty. Verified by a
  clean-venv install of the built wheel (Python 3.14, macOS arm64).
- Test suite: 1403 passed (1344 baseline + 59 new) on Python 3.14.

Still external (unchanged): GitHub Actions CI (account billing lock —
zero hosted runs), live provider execution, Windows/3.11/3.13
verification. The Step 01 universal-onboarding implementation lives on
the separate `feature/step01-universal-onboarding` branch pending
integration; see docs/final-handoff.md.

## 1.1.0rc1 — product-completion release candidate

The second master program (the "final product completion" before the
independent external audit) delivered, on top of 1.0.0rc1:

- **Brain Dump Guard** — `aperio capture` preserves mid-task messages
  verbatim in an append-only chain-hashed intake log; a deterministic
  keyword heuristic routes items (related → checkpoint first, unrelated
  → queued, ideas → never executed) and is labeled as heuristic
  everywhere; urgency authority has exactly one channel (the operator's
  `--urgent` flag — urgency words in text are detected and reported
  with authority: none); `inbox`/`inbox-resolve` review the queue.
  Cooperative same-chat protocol documented; never claimed as host
  interception.
- **Durable continuity** — `checkpoint-write/show/recover`: a CAS
  checkpoint derived only from durable artifacts, refreshed at
  meaningful transitions, with drift detection, verified-vs-unverified
  separation, and gated resume eligibility.
- **Goal-to-plan bridge + priority scheduling** — `plan-propose-from-goal`
  converts a goal's descriptive decomposition into a plan proposal
  through the ordinary gated plan-create (a breakdown is never an
  approved plan); plan-schema v2 adds optional per-task priority with
  v1 documents fully readable; the scheduler orders by (priority rank,
  task id) with visible rationale.
- **Controlled orchestration** — `orchestrate-assess/plan/brief`:
  explainable single-agent vs specialist recommendation from
  operator-declared capabilities, sequential-only (parallel not
  offered), delegations through the ordinary gates, scoped specialist
  briefs.
- **Skill packs** — SKILL.md-compatible, trust-aware: imports untrusted
  until explicit review; content changes reset trust; conflicts block
  composition; exports approved-only; 11 builtin packs (requirements,
  UX/IA, design systems, accessibility, frontend, backend/CMS,
  security, testing, release, technical SEO, SEO content reporting).
- **Workflows** — docs/website-delivery.md (13-stage client walkthrough)
  and docs/seo-30-day.md (30-day SEO with no-guarantees/no-invented-data
  rules); host honesty in docs/host-integration.md.
- **UX** — consolidated `aperio work-status`; optional `aperio
  serve-ui`: a READ-ONLY loopback UI (127.0.0.1 only, per-launch URL
  token, GET-only, no write endpoints, no remote assets).

**Verification (local, honestly reported):** 1341+ tests green on
Python 3.14.7; one cross-interpreter run on 3.12.14; the wheel builds
with benchmark cases and skill packs as package data. GitHub Actions
remains blocked by the account billing lock (zero CI steps executed).
One non-reproducible test flake observed twice under concurrent local
load (triaged post-RC). Live provider execution and 3.11/3.13/Windows
remain unverified. See [limitations.md](limitations.md).


## 1.0.0rc1 — release candidate

Aperio Next 1.0.0rc1 is the completed release candidate: the universal
context/goal/memory layer (M1–M3), the quality/feedback/exchange/guidance
layer (M4–M7), the evaluation framework (M8), the Features 01–07 project
workflows, and the Chunk 07 integration/security/compatibility/usability
audits. All master-plan chunks 00–08 are complete; the final report
distinguishes verified evidence from unsupported claims.

**Verified locally (this machine):** 1248 tests green on Python 3.14.7 and
Python 3.12.14 (the latter against the built wheel in a clean venv); the
wheel builds with the benchmark case set as package data and the
installed CLI runs from outside the repository. **Not verified:** GitHub
Actions (account billing lock — zero CI steps have executed for the whole
series; every green claim above is local), Python 3.11/3.13 on this
machine (not installed; declared supported and exercised in the blocked
CI matrix), Windows (not tested), and live provider end-to-end execution
(no credentials; adapters verified against fake CLIs and the documented
interfaces).

## What 1.0 contains (by milestone)

- **Features 01–07** (0.1–0.8 series): versioned project state, portable
  agent memory with an append-only decision log, plan/task governance
  with independent review gates, an agent execution runtime with manual
  and local-process adapters, portable export/import with integrity
  checking, opt-in workflow governance with immutable review records,
  and provider-neutral codex-cli/claude-cli adapters.
- **M1** (0.8.0): universal read-only context packs, `general` default,
  deterministic budgeting, trust labels.
- **M2** (0.9.0): goal-schema v1, six data-only profiles, goal→pack.
- **M3** (0.10–0.13): universal memory stores, review states, honest
  trust derivation, portable memory exchange; four gate reviews closed
  the verification, expiry, append-safety, entry-point, and
  state-proof trust gaps.
- **M4** (0.14.0): preflight and deliverable quality reports
  (quality-report v1, taxonomy v1, quality-rules v1) with honest
  statuses.
- **M5** (0.15.0): feedback documents with explicit review, lessons via
  the M3 contract, before/after comparison with regressions reported,
  improvement proposals applied only to new files.
- **M6** (0.16.0): the universal exchange (aperio-universal-exchange v1)
  with per-section owning-contract validation, digest-checked integrity,
  dry-run imports, and `imported-*` naming.
- **M7** (0.17.0): agent-ready briefs, advisory profile self-checks,
  tested manual workflows, provider support/limits documentation.
- **Chunk 05** (0.17.1): one shared provider-metadata contract closing
  the documented Feature 07 P1 mismatch.
- **M8** (0.18.0): the offline cross-domain benchmark (14 versioned
  cases, all six domains) with honest scope statements and no aggregate
  score.
- **Chunk 07** (0.19.0): the full-pipeline integration test, the
  security audit (one P1 closed: brief structure forging via memory
  content), the compatibility audit, and the security-model document.

## Compatibility

- All schemas remain v1: state, plan, goal, profile, portable archive,
  memory exchange; the new quality/feedback/exchange/benchmark formats
  are v1 as well. No data migration is required or performed; `aperio
  upgrade`'s registry remains intentionally empty.
- Every Feature 01–07 command and every M1–M3 behavior is unchanged
  (pinned by tests/test_compatibility.py and the full suite).
- No third-party runtime dependencies; Python ≥ 3.11.

## Known limitations

See [limitations.md](limitations.md). The short version: hashes detect
corruption, not authorship; actor identifiers are declarations; the
benchmark measures the tool on fixtures, not agent performance; CI is
currently blocked by a GitHub billing issue, so the release candidate's
verification is local; live provider execution is unverified.
