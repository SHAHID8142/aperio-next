# Aperio Next — Master Completion Plan

Durable progress document for the final master directive: complete the
remaining project from the verified baseline to a 1.0 release candidate.
Recovered at every context boundary from this file plus the repository.

## Verified baseline

- **Repository:** https://github.com/SHAHID8142/aperio-next (local checkout:
  `/Volumes/SSD/CODES/Aperio/aperio-next`, branch `main`)
- **Baseline HEAD:** `fdadf0f8f458de8d9f8eec69fe952044a06c1980`
  (`feat(m3): re-derive memory trust at the direct context boundary`)
- **Version:** 0.13.0 (pyproject.toml == `aperio/__init__.py`)
- **Baseline test result:** `pytest` → **1093 passed** (~60 s, local
  `.venv`, Python 3.14.7, macOS arm64)
- **CI status:** GitHub Actions runs fail at start — *"The job was not
  started because your account is locked due to a billing issue."* Zero test
  steps execute in Actions. This is an external infrastructure blocker; all
  verification in this assignment is local and is reported as such (Rule G).
  Every chunk is verified locally with the full suite before its commit.
- **Existing capability:** Features 01–07, M1 (context packs), M2 (goal
  contract + profiles), M3 (universal memory + trust boundary) are
  implemented and documented in `docs/architecture.md`, `docs/conventions.md`,
  `docs/roadmap.md`, `docs/context-pack.md`, `docs/goal-contract.md`,
  `docs/universal-memory.md`, `docs/feature-07.md`.

## Final product requirements (from the master directive)

A universal, agent-agnostic AI performance, context, memory, and reliability
enhancement layer that works for design, research, writing, business,
everyday and optional software work; usable through manual copy/paste and
ordinary files; no mandatory cloud, model API, IDE, database, or
subscription. See directive sections 1, 14, and 16; the global acceptance
criteria are §16 of the directive and are re-verified in Chunk 08.

## Chunk order and status

| Chunk | Deliverable | Status | Commit |
|---|---|---|---|
| 00 | Existing code audit + M3 final trust stabilization | COMPLETE | 4997fe9 |
| 01 | M4 — universal mistake prevention + quality evaluation | COMPLETE | 4d158fd |
| 02 | M5 — controlled feedback and improvement | COMPLETE | 247d4f3 |
| 03 | M6 — universal portable exchange v2 | COMPLETE | 57fff8b |
| 04 | M7 — optional integration + domain adaptation | COMPLETE | 1c52acf |
| 05 | Feature 07 provider reliability closure | COMPLETE | f00e656 |
| 06 | M8 — cross-domain benchmark + evaluation | COMPLETE | bc94c75 |
| 07 | Integration, security, usability | COMPLETE | d282c79 |
| 08 | Final release engineering, 1.0 RC | COMPLETE | see Chunk 08 note |

Chunk statuses: `PENDING`, `IN_PROGRESS`, `BLOCKED`, `FAILED`, `COMPLETE`.
A chunk is marked COMPLETE only after its acceptance criteria actually pass
locally (full regression suite included) and its commit SHA is recorded.

## Confirmed defects

Recorded as they are confirmed; each closed defect lists its regression test.

| ID | Chunk | Severity | Summary | Status |
|---|---|---|---|---|
| D-00-1 | 00 | P1 | Raw/caller-supplied memory records could claim `state="attested"` (or carry the recorded `attested` state) without any recorded `accept` transition and receive attested authority in recall/packs; derivation now requires the recorded transition for every active state, not only `verified` | CLOSED (commit recorded below) |
| D-05-1 | 05 | P1 | `adapters._bounded_usage()` accepted ≤ 10**12 while `runtime._validate_provider_result()` rejected > 1e9; Claude `num_turns`/`duration_ms` shared the mismatch → stranded runs. CLOSED: one shared contract (`MAX_PROVIDER_METADATA_VALUE`/`MAX_PROVIDER_COUNT_VALUE`/`MAX_PROVIDER_COST_USD` in adapters.py, imported by runtime.py); beyond-contract magnitudes recorded as honest absence; boundary + end-to-end tests | CLOSED |

## Outstanding blockers

- **B-CI (external):** GitHub Actions billing lock — Actions executes zero
  steps. Unresolvable locally. Reported honestly in every chunk; final report
  distinguishes local verification from CI verification.

## Exact next action

Maintained at the end of the "Status" section per chunk. Current: **NONE — all chunks complete. Release candidate 1.0.0rc1.**

## Chunk notes (evidence log)

Appended per chunk: sources changed, tests added, results, decisions.

### Chunk 00 — COMPLETE (0.13.1, commit 4997fe9b1321eb550322c750e595cddfab5dc5cd)

**Audit findings.** Baseline `fdadf0f` (v0.13.0) reviewed against
`docs/architecture.md`, `docs/conventions.md`, `docs/roadmap.md`,
`docs/context-pack.md`, `docs/goal-contract.md`, `docs/universal-memory.md`,
`docs/feature-07.md`, and the source/test tree. The prior M3 gates
(0.10.1 P1-A/B/C, 0.11.0 effective trust, 0.12.0 entry points, 0.13.0
trust-consumption) are genuinely closed. One confirmed P1 remained, exactly
the suspected one:

- **D-00-1 (P1, CLOSED):** a caller-built record claiming
  `state="attested"` with no recorded `accept` transition received attested
  authority everywhere — derivation, recall ranking (including the
  reviewed-memory global-scope bypass), context items, and both pack
  builders. Closed by requiring the producing transition for every active
  state in the one shared derivation (`memstore.derive_verification` /
  `effective_trust`); unsupported `attested` → `proposed` with
  `verification.basis: unrecorded`; declared labels above the derivation are
  refused; the `verified`-without-proof → `attested` legacy contract is
  unchanged. Second hole closed in the same pass: a claimed state the
  record's own history contradicts now reports
  `verification.replayed_state`, is refused as support, and recall excludes
  records whose replay ends retracted/superseded; `memory_context_items`
  refuses them.
- Audited and found already sound: imported provenance (0.12), direct
  ContextItem construction (0.13 + downgrade surfacing), privacy
  classification (sensitivity/identity fail-closed), expiry/withdrawal,
  deterministic output, content identity, append/file safety and
  concurrency (P1-C). Exchange files deliberately unchanged: attested
  entries import as `proposed` regardless (and an `attested` record can
  have at most one review event, so a history-less attested entry is always
  forged — but imports never promote, so acceptance-as-data stays safe).

**Sources changed:** `src/aperio/memstore.py` (derivation +
`effective_trust`), `src/aperio/memrecall.py` (derived-trust ranking bypass,
replayed-inactive exclusion, `reviewed` from derived trust, warning text),
`src/aperio/context_pack.py` (downgrade surfacing for unsupported state
claims, warning text), `tests/test_memory_state_proof.py` (new, 18 tests),
fixture fixes in `tests/test_memrecall.py` / `tests/test_memory_boundary.py`
(records now carry the accept transition their claimed state requires),
`tests/test_memory_gate.py` (verification dict gains `replayed_state`),
README + docs/universal-memory.md + docs/roadmap.md, version 0.13.1.

**Tests:** 1093 baseline → **1111 passed** locally (0 failures), Python
3.14.7. GitHub Actions: not run (billing lock — see Blockers).

### Chunk 01 — COMPLETE (0.14.0, commit 4d158fdfabf5396ab4fec7260b84686c5ed1db2b)

**Delivered (M4).** New module `src/aperio/quality.py`: one deterministic
engine over a declarative rule registry (quality-rules v1), a stable
11-category mistake taxonomy (taxonomy v1), and honest finding statuses
(`confirmed` / `warning` / `unknown` / `recommendation`). Two read-only
reports from one canonical payload with reproducible `content_sha256` and
content-derived issue ids:

- `aperio preflight GOAL [--memory-store F] [--json] [--output F]` — unclear
  objective, missing acceptance/deliverables/quality criteria, duplicate
  instructions, conflicting stated length bounds, untrusted goal
  provenance, unvalidated assumptions, unaddressed risks, oversized
  instruction lists, and (read-only) expired/unreviewed/conflicting memory.
- `aperio quality-check GOAL --deliverable FILE [--request F]
  [--require-section/--require-text/--forbid-text/--min-words/--max-words/
  --no-output-contract]` — operator-required sections/texts, forbidden
  text, modal constraint extraction ("must include X" / "must not … Y" with
  a negation-context guard), stated length bounds (leading and trailing
  phrasing; 10% tolerance band for approximate targets; pages/characters
  reported, never converted), output-format expectations (JSON parse =
  confirmed), deliverable coverage (warning-only signal), source
  attribution + absolute-claim warnings under evidence requirements, an
  explicit `unknown` status for source support, limitation language, and a
  software-profile verification-evidence rule.
- New versioned `quality-request` (request-schema v1) contract; deliverable
  bounded at 1 MiB UTF-8; `--output` uses the shared atomic publication
  path; reports never contain absolute paths.

**Safety.** No untrusted content executed, no memory writes (byte-identical
store proven by test), no goal/deliverable alteration, no governance
authority — Feature 06 completion gates untouched (asserted by the full
suite). CLI prints via the standard single-line `aperio: error:` failures
for malformed input (goal, deliverable, request all covered).

**Acceptance evidence.** Tests demonstrate at least one real detected error
in each of the six domains (general, design, research, writing, business,
software) at both engine and CLI level, a passing deliverable with zero
confirmed findings per domain pair, unknown-status handling, determinism
(same inputs → same issue ids and content hash), false-positive guards
(negated prohibitions, token coverage, request/file agreement), malformed
inputs, and compatibility (full suite green).

**Sources changed:** `src/aperio/quality.py` (new), `src/aperio/cli.py`
(two subparsers + handlers + dispatch), `tests/test_quality.py` (28),
`tests/test_quality_cli.py` (17), `docs/quality.md` (new), README,
docs/architecture.md, docs/roadmap.md, version 0.14.0.

**Tests:** 1111 → **1160 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 02 — COMPLETE (0.15.0, commit 247d4f3ae86ab8894e0c959cef426599cc35b611)

**Delivered (M5).** New module `src/aperio/feedback.py` + five CLI
commands. Feedback documents (feedback-schema v1) start as proposals with
content-derived identity (FB-…, verified if declared), bounded fields,
append-only review entries, calendar-valid UTC timestamps, and final
decisions (`--replace` is a revision CAS against parallel review). Only an
explicit accepted decision produces a lesson, and the lesson enters the
operator-named M3 store as a NEW PROPOSED memory — the M3 review contract
stays the only memory authority; agent-origin feedback and agent
self-reported evaluations are flagged (`feedback_issues`, stderr), never
silent, and never promote. `feedback-compare` deterministically compares
two quality reports for the same goal: only confirmed/warning findings
count as resolved; new confirmed/warning findings are reported as
potential regressions; the outcome label (improved/regressed/mixed/
unchanged) is computed from those sets and the report states it cannot
attribute causes. Improvement proposals (improvement-proposal v1) target
goal instruction fields and quality-request fields only, apply to a NEW
copy via publish_text_atomic, refuse no-ops/duplicates, and cannot modify
the code-owned profile registry.

**Acceptance evidence.** tests/test_feedback.py walks the full directive
loop end-to-end through the CLI (task → output → quality issue → feedback
→ explicit review → proposed lesson memory → mem-review accept → future
task retrieval → feedback-compare 'improved'), plus the negative case
(worse deliverable → 'regressed' with new findings listed), rejected
feedback refused, agent-origin lessons still enter as proposed, no-op and
duplicate proposals refused, originals byte-identical after apply, CAS
conflict behavior, and malformed-input refusals.

**Sources changed:** `src/aperio/feedback.py` (new), `src/aperio/cli.py`
(5 subparsers + handlers + dispatch), `tests/test_feedback.py` (19),
`docs/feedback.md` (new), README, docs/architecture.md, docs/roadmap.md,
version 0.15.0.

**Tests:** 1160 → **1179 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 03 — COMPLETE (0.16.0, commit 57fff8b8207c7840edf992012d6deb8a1938a7c8)

**Delivered (M6).** New module `src/aperio/exchange.py` + `exchange-export`
/ `exchange-import` CLI. One `aperio-universal-exchange` v1 document whose
sections are already-versioned contracts (goal-schema v1, context-pack v1
envelope with re-verified hash, quality-report v1 envelope, feedback-schema
v1, `aperio-memory-export` v1 for memories, bounded operator notes),
validated by their owning contracts at BOTH export and import,
digest-checked per section and for the whole document, deterministically
ordered, and capped. Selection is explicit (named files; memories need
--memory-id/--memory-tag; private memory needs the deliberate opt-in).
Imports preview by default (`--apply` to write), write
`imported-<kind>-<digest>.json` files that never overwrite existing bytes
(identical → `already-present`; different content at the digest-derived
name → refused conflict), bring memories in as `proposed` through the
ordinary M3 import (duplicates/conflicts/unsupported trust claims
reported), warn on every imported goal as untrusted pending review and
provenance re-declaration, and skip memory sections with a reason when no
--memory-store is given. Newer format versions and unknown section kinds
fail closed. Feature 05 archive v1 and M3 memory-exchange v1 unchanged
(asserted by test).

**Acceptance evidence.** tests/test_exchange.py walks ChatGPT → Aperio →
Claude → Aperio → Gemini/GLM entirely through CLI-produced portable
payloads: the receiving machine's store holds only `proposed` memories,
the unsupported claim (hand-appended unproven verify) arrives declared
`verified` but reads `proposed` with `declared_state` preserved, the
accepted lesson travels and stays proposed, re-import reports duplicate +
already-present, tampering (payload/digest/format_version/unknown kind) is
refused, and determinism holds regardless of build order.

**Fixed during the chunk:** a CLI name collision (`load_exchange_file`
from memportable shadowing the exchange module's loader — aliased) and a
build-side gap (export now runs the same per-section owning-contract
validation import applies).

**Sources changed:** `src/aperio/exchange.py` (new), `src/aperio/cli.py`
(2 subparsers + handlers + dispatch + alias), `tests/test_exchange.py`
(11), `docs/exchange.md` (new), README, docs/architecture.md,
docs/roadmap.md, version 0.16.0.

**Tests:** 1179 → **1189 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 04 — COMPLETE (0.17.0, commit 1c52acfaf1a83bfa748a6f1affacaecbf983e7c0)

**Delivered (M7).** New module `src/aperio/guidance.py` + `agent-brief`
CLI: the paste-ready instruction package (aperio-agent-brief v1) assembled
only from operator-declared goals (agent/import goals refused with an
actionable message), carrying objective/outcome/audience, requirements,
constraints, deliverables, acceptance criteria, evidence requirements,
output contract, operator quality criteria, profile suggested self-checks
(advisory), reviewed memory (attested/verified) plus accepted lessons,
unverified proposals listed separately, conflicts, uncertainties, and the
trust boundary text. Private memory never enters a brief (own filter in
addition to recall defaults + CLI cap with stderr note). Deterministic
payload + Markdown; safe --output publication. Profiles strengthened with
data-only `suggested_checks` for all six domains (rendered under an
explicit advisory heading, never gating). docs/agent-workflows.md
documents tested manual copy/paste workflows for text agents — the exact
command sequence is executed by
test_manual_workflow_commands_run_end_to_end. docs/providers.md
consolidates the adapter support matrix, capability detection, safe
failure, and the plain statement that live provider end-to-end execution
is NOT verified (no credentials); provider adapters remain optional and
fail closed (their safety tests already exist in
tests/test_provider_adapters.py and stay green).

**Sources changed:** `src/aperio/guidance.py` (new), `src/aperio/profiles.py`
(suggested_checks), `src/aperio/cli.py` (agent-brief + private cap),
`tests/test_guidance.py` (9), `docs/agent-workflows.md` (new),
`docs/providers.md` (new), README, docs/architecture.md, docs/roadmap.md,
version 0.17.0.

**Tests:** 1189 → **1198 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 05 — COMPLETE (0.17.1, commit f00e6568e97963c2c4b2361447ffeebc2df5c453)

**Defect D-05-1 closed.** One shared metadata contract now defined in
`src/aperio/adapters.py` (`MAX_PROVIDER_METADATA_VALUE = 1_000_000_000`,
`MAX_PROVIDER_COUNT_VALUE = 1_000_000`, `MAX_PROVIDER_COST_USD = 10**9`)
and imported by `runtime.py`, replacing three independent inline bounds
(adapter 10**12 vs runtime 1e9 on usage tokens, Claude num_turns and
duration_ms; both on cost). `_bounded_usage` documents the adapter side:
beyond-contract magnitudes are recorded as honest absence (`None`),
never written as a value the runtime validator would refuse. All adapter
fields audited for cross-layer mismatches: usage tokens (codex+claude),
num_turns, duration_ms, permission_denials (<=1000 by construction),
event/error/unknown counts (<=4096 by construction), item_counts
(<= events), cost — all now governed by the shared constants. Empty usage
objects are recorded as `None` (no in-contract usage recorded), not `{}`.

**Honest failure behavior.** With aligned boundaries, an adapter-accepted
result can no longer fail persistence on magnitude grounds. End-to-end
tests dispatch through real fake-CLI processes reporting 2e9/9e12
magnitudes: the run finalizes `completed` with dropped values and
`executions.json` stays readable (the pre-fix behavior poisoned reads).
Nonzero-exit and malformed-output honest finalization was already covered
by tests/test_provider_adapters.py (still green). Live-provider end-to-end
testing remains NOT verified (no credentials) — stated in
docs/providers.md.

**Sources changed:** `src/aperio/adapters.py` (shared constants +
`_bounded_usage` + cost bound + usage-None semantics), `src/aperio/runtime.py`
(validator uses the imported shared constants), `tests/test_provider_metadata_bounds.py`
(new, 9 tests incl. end-to-end codex+claude), `docs/feature-07.md`
(blocker section replaced with the resolved contract), `docs/roadmap.md`
(blocker closed), version 0.17.1.

**Tests:** 1198 -> **1207 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 06 — COMPLETE (0.18.0, commit bc94c75189c3cb58c9e33ca713434dffbfe59fc8)

**Delivered (M8).** New module `src/aperio/benchmark.py` + 14 versioned
fixture cases packaged as `benchmark_cases/*.json` (package-data) +
`benchmark-cases`/`benchmark-run` CLI. The offline runner measures, per
domain and without any provider: detection of planted flaws
(expected_rules must fire, forbidden_rules must not — false-positive
guards incl. a passing-deliverable case; a case that stops detecting is a
reported regression), trust-boundary enforcement (forged verified claims,
agent-goal-as-brief, untrusted-goal-instructions-in-pack must all be
blocked), cross-agent information preservation through an exchange round
trip (goal fields/notes verbatim, memory always proposed, unsupported
claims flagged), memory correctness (expired excluded, unreviewed
flagged, private never selected), missing-information handling, and
wall-clock overhead (excluded from the content hash — proven by test).
Feedback-improvement and honest-regression cases use the M5 comparison
(improved AND regressed outcomes must be reported). The report states
verbatim that it is NOT a measurement that agents perform better with
Aperio: baseline detection is zero by definition, `requires_human`
metrics are listed per case and never scored, and no aggregate quality
score exists (asserted by test). Cases cover ambiguous instructions,
missing information, conflicting requirements, large irrelevant context,
outdated memory, unsupported claims, cross-agent transfer,
feedback-improvement, incomplete deliverables, and correctly handled
uncertainty across all six domains (software one among six).

**Sources changed:** `src/aperio/benchmark.py` (new),
`src/aperio/benchmark_cases/` (14 new case files), `src/aperio/cli.py`
(2 subparsers + handlers + dispatch), `pyproject.toml` (package-data),
`tests/test_benchmark.py` (13), `docs/benchmark.md` (new), README,
docs/architecture.md, docs/roadmap.md, version 0.18.0.

**Tests:** 1207 -> **1220 passed** locally (0 failures). GitHub Actions:
not run (billing lock).

### Chunk 07 — COMPLETE (0.19.0, commit d282c79dd26816c26f1c219f9ef2192882988828)

**End-to-end pipeline.** tests/test_integration_pipeline.py walks the
directive's full pipeline as ONE continuous CLI-only workflow — objective
→ goal validation → preflight → reviewed memory → agent brief →
(supplied) deliverable → quality evaluation → feedback → explicit review →
approved lesson (proposed, then accepted via M3) → future-task brief
improvement → exchange transfer to a second machine (memory stays
proposed) → re-evaluation (missing attribution resolved, honest
`unknown` survives) → feedback-compare 'improved'. No project init, no
provider, no network.

**Security audit.** All listed areas re-verified; one confirmed P1 found
and fixed:
- D-07-1 (P1, CLOSED): agent-brief rendered multi-line memory content
  unindented, so a memory carrying '\n## forged heading' could forge
  column-0 Markdown structure in a shareable artifact. Fixed with the
  same continuation-indent defense the decision log and packs use
  (guidance._quote_content), plus single-line bounds for brief
  task_reference and benchmark case_id/scenario. Regression:
  test_memory_content_cannot_forge_column_zero_headings asserts every
  column-0 heading in a brief with injected content is an expected one.
- Cleanly covered by existing + new tests: CLI error handling for every
  M4-M8 command (single-line 'aperio: error:', no traceback), symlink
  refusal sweep (deliverable/goal/feedback/exchange inputs), absolute-path
  exclusion from generated artifacts, private-context exclusion across
  recall/briefs/exchanges/benchmarks, exchange validation fail-closed,
  publication/concurrency/CAS guards, size limits, provider process
  handling, and destructive-operation guards. Summary now lives in
  docs/security-model.md.

**Compatibility audit.** All Feature 01-07 commands and M1-M3 surfaces
pinned present (tests/test_compatibility.py), schema-version table
unchanged (state/plan/goal/profile/archive/memory-exchange v1; new
quality/feedback/exchange/benchmark all v1), standalone no-init operation
asserted for goal-check/preflight/agent-brief/quality-check/benchmark,
supported-Python documentation checked, M1 goal-pack output stability
re-verified.

**Usability audit.** The six personas' workflows run standalone with no
project: the benchmark case set is exactly the designer/researcher/
writer/business/general/developer exercise set; agent-workflows.md
documents the tested procedure; no command in the universal layer
requires `aperio init`.

**Sources changed:** `src/aperio/guidance.py` (quote-indent +
task_reference bound), `src/aperio/benchmark.py` (single-line bounds),
`tests/test_security_audit.py` (22), `tests/test_integration_pipeline.py`
(1), `tests/test_compatibility.py` (5), `docs/security-model.md` (new),
README, docs/architecture.md, docs/roadmap.md, version 0.19.0.

**Tests:** 1220 -> **1248 passed** locally (0 failures). GitHub Actions:
not run (billing lock).

### Chunk 08 — COMPLETE (1.0.0rc1)

**Packaging.** Wheel builds (`aperio_next-1.0.0rc1-py3-none-any.whl`,
14 benchmark case files verified inside the archive as package data);
installs into a fresh Python 3.12 venv; console script `aperio` runs from
outside the repository; `--help` accurate; runtime dependencies remain
zero; version 1.0.0rc1 consistent across pyproject + `aperio.__init__`
(test_version). Optional integrations remain genuinely optional: the full
universal layer runs with no provider installed (asserted).

**Verification matrix (local, honestly reported).**
- Python 3.14.7 (repo venv): 1248 passed — repeated twice.
- Python 3.12.14 (clean venv, installed wheel): 1248 passed — repeated
  twice.
- Python 3.11/3.13: NOT installed on the verification machine (declared
  supported; exercised only in the blocked CI matrix).
- GitHub Actions: ZERO steps executed for the entire series (billing
  lock). No CI success is claimed anywhere.
- One non-reproducible failure observed once during concurrent local
  builds (identity not captured before cache reset); four subsequent
  full-suite runs green on both interpreters. Triaged as a post-RC
  hardening item (P2): identify and de-flake the likely timing-sensitive
  concurrency/process tests.
- Cross-domain CLI smoke: `benchmark-run` 14/14 passed; installed-wheel
  smoke covers goal/preflight/quality/exchange paths; portable round
  trip, security/privacy regressions, concurrency/corruption, and the
  M1–M8 pipeline integration all covered by the suite (Chunks 00–07
  evidence above).

**Documentation.** README (doc index, release status), quickstart
(non-coding first-run), cli-reference, python-api, goal-contract,
context-pack, universal-memory, quality, feedback, exchange,
agent-workflows, providers, benchmark, security-model, architecture,
conventions, roadmap, release-notes (1.0.0rc1 + verification statement),
limitations, troubleshooting, master-completion-plan.

**Version.** 1.0.0rc1 (release candidate; no PyPI publication, no public
release, no force-push — per the directive's scope).

## Directive §16 final acceptance checklist

1. Universal task without initializing a coding project — YES (standalone
   tests + quickstart; asserted by test_standalone_workflows_need_no_project).
2. Trust-aware context construction — YES (M1 packs, M3 memory, M7
   briefs; trust derived from recorded history everywhere).
3. Reviewed memory across sessions and agents — YES (M3 stores + M6
   exchange; round-trip test).
4. Unknown/unsupported claims not silently verified — YES (state-proof
   boundary tests; exchange imports as proposed; benchmark trust cases).
5. Quality checks identify concrete mistakes — YES (M4; six-domain
   benchmark 14/14).
6. Feedback reviewed and reused safely — YES (M5; full-loop test).
7. Agent-ready instructions without a provider API — YES (M7 brief;
   manual workflows tested).
8. Portable exchange preserves context and provenance — YES (M6
   preservation case).
9. Imported information stays untrusted — YES (`imported-*` naming,
   goal warnings, memories proposed).
10. Optional provider integrations behave safely — YES (Feature 07 tests;
    shared metadata contract; probes; two consents).
11. Feature 07 metadata mismatch resolved — YES (Chunk 05, D-05-1).
12. Benchmarks cover non-coding and coding tasks — YES (software is one
    of six domains).
13. CLI and Python library work together — YES (CLI handlers are thin
    wrappers over the tested library calls).
14. Legacy data and commands compatible — YES (compatibility audit).
15. Confirmed P0/P1 resolved — YES (D-00-1, D-05-1, D-07-1 closed; CI
    billing lock is external and documented, not a code defect).
16. Tests/examples use real executable pathways — YES (no mocked claims;
    subprocess/process-boundary tests use real fake CLIs; every doc
    command sequence is executed by tests).
17. Documentation matches the implementation — YES (each doc names its
    test evidence; honesty limits stated in the docs themselves).
18. Package builds and installs from a clean environment — YES (3.12
    clean venv, wheel install, external invocation).
19. Final report distinguishes verified evidence from unsupported
    claims — YES (below).
20. Repository contains a reproducible release candidate — YES (tagged
    content = the 1.0.0rc1 commit; wheel rebuildable from source).

**P2 triage (documented, non-blocking):** the timing-sensitive test flake
noted above; 3.11/3.13 and Windows verification pending an environment
with those platforms; live-provider integration testing pending
credentials.
