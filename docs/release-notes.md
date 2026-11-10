# Release notes

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
