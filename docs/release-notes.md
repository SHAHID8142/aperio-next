# Release notes

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
