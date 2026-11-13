# Final handoff — Aperio Next 1.2.0rc2 (for independent audit)

Current completion evidence: [release-candidate-evidence.md](release-candidate-evidence.md).
The revision-2 run passed 1769 Python and 34 Node tests, verified native
Codex coding and fresh recovery, and independently resolved eight HIGH
findings. Earlier counts and host-access statements below are historical.

Prepared per the product-completion directives. Every claim names its
evidence location; unverifiable items are labeled unverifiable, not
omitted. Chunk-level detail for the 1.2 program lives in
[master-plan-v3.md](master-plan-v3.md); the 1.1 program in
[master-plan-v2.md](master-plan-v2.md).

> **Universal agent integration program (2026-11-12, branch
> `feat/universal-agent-integration`):** added native skill registration
> (`aperio integrations`, **26 hosts** at their documented locations,
> managed-marker ownership rules), a zero-dependency npm
> launcher (`aperio-next install|doctor|update|uninstall`, clean-room
> verified from the packed tgz), an actionable-error fix for
> session-less onboarding commands, and the beginner-first README +
> [docs/integrations.md](integrations.md). Per-scenario evidence:
> **[universal-integration-evidence.md](universal-integration-evidence.md)**.
> Live in-session activation is verified end-to-end in Claude Code v2.1.322,
> OpenCode v2.0.32 and Cline 3.0.74 (Goose discovery confirmed live); the
> remaining hosts are registration/discovery-verified only on this machine.
> 1763 Python tests and 17 Node launcher tests green.
> The earlier "1761", "1758 / 16", "1747 / 16" and "1713 / 13" counts are
> superseded (see the evidence doc's P0 remediation record).

> **1.2 hardening audit:** an independent, adversarial audit of `main` @
> `9773ba4` reproduced and fixed fourteen findings (including two critical),
> then two remediation rounds closed five further defects (wizard init consent,
> an install-approval fingerprint bypass, task-size gates that only ran on one
> of five paths, wizard consent that failed open, and section-sized tasks such
> as an entire Hero Section being treated as atomic). Installation was modernized to `uv tool install` and the
> README rewritten for beginners.
> Full evidence, reproduction steps, gate table, and the honest remaining
> risks are in **[audit-final.md](audit-final.md)**. That document supersedes
> any older test count below.

## A. Exact source identity

- **Repository:** https://github.com/SHAHID8142/aperio-next
- **Audit branch:** `audit/1.2-final-hardening` (additive commits on `9773ba4`)
- **Audited source SHA:** `5715380` (code + tests; the README install pins it)
- **Source histories:** `feature/master-v3-completion` @ `8a3fe94` and
  `feature/step01-universal-onboarding` @ `6bea2e2`, integrated on
  `integration/v1.2-final`. Main is updated only after the complete audit
  passes; no force-push is used.
- **Release version:** 1.2.0rc2 (pyproject.toml == `aperio.__version__`,
  asserted by tests/test_version.py)
- **Working tree:** clean at the handoff commit; `dist/` artifacts are
  build outputs and are not committed.
- **Verdict:** **RELEASE CANDIDATE — SPECIFIC VALIDATION PENDING** (Windows,
  Linux, live authenticated agent runs, and hosted CI remain unverified).

## B. Completed capabilities (feature → modules → commands → guarantee → tests)

### 1.2 additions (this program)

| Capability | Modules | Commands/APIs | Actual guarantee | Limits | Tests |
| --- | --- | --- | --- | --- | --- |
| Two-level planning | `roadmapmodel.py`, `roadmapstore.py`; plan-schema v3 linkage | `roadmap-create/show/approve/revise`; `plan-create --milestone` | LEVEL-1 master roadmap (lifecycle, milestones, capabilities, dependencies, risks, acceptance conditions) draft-until-approved with the plan's governance rules; the detailed plan stays the only execution engine; milestone progress derived from plan linkage, never stored | Roadmap is a planning document, not a scheduler; no roadmap adoption path (recreate under governance) | test_roadmap.py (29) |
| Bounded retry | `recovery.py` + runtime gate | `run-start --remediation`, `recovery-report` | Max 3 attempts per deterministic problem signature; retries require declared new evidence/hypothesis (verbatim repeats refused); 4th attempt refused with escalation guidance (blocked, report, decision-add, doctor, operator); enforced in the Python boundary | Signature keys on recorded outcome text — two causes with identical presentation share a budget; coordination boundary, not a root-cause analyzer | test_recovery.py (17) |
| Host compatibility doctor | `hostcompat.py` | `host-doctor [--probe]` | Six-level vocabulary; `native_verified`/`experimental` defined but **never emitted** (no verified native integration exists; none invented); provider CLIs passively probed; every host names its fallback | A clean probe proves a binary runs, not that a session works; cooperative hosts are `not_yet_tested` by design | test_hostcompat.py (13) |
| Fresh-machine install | docs/install.md | — | Verified clean-venv wheel install (3.14, macOS arm64): version/host-doctor/init/status + package data | Windows/Linux/3.11/3.13 documented as not tested | manual verification |
| Universal onboarding | `onboarding*.py` | `next`; 18 `onboarding-*` commands | Read-only discovery, durable adaptive interview, controlled standardization/tool gates, and honest readiness reporting | No native commercial-host integration claimed | `test_onboarding_*.py` |

### 1.1 base (unchanged; see master-plan-v2.md)

Brain Dump Guard (`capture`/`inbox`), continuity (`checkpoint`/`recover`),
goal→plan bridge, priority scheduling, orchestration (assess/plan/brief),
11 skill packs with trust gates, consolidated UX (`work-status`,
`serve-ui`), plus the 1.0 base (quality, feedback, exchange, memory,
benchmark). Full table in master-plan-v2.md §B.

## C. User-facing walkthrough

The complete 1.0/1.1 experience (install → goal → plan → execution →
brain dumps → routing → recovery → specialist/skill → verification →
handoff) is exercised END-TO-END in `tests/test_e2e_website.py`
(`test_full_client_website_az_simulation`). The 1.2 additions are
covered by their own suites (`test_roadmap.py`, `test_recovery.py`,
`test_hostcompat.py`), including the CLI roundtrips and governance
paths. Install instructions: [install.md](install.md) (fresh machine)
or README "Install (development)". The integrated Step 01 onboarding journey
(new/existing project, interview, readiness) is exercised by the seven
automated scenarios in `tests/test_onboarding_e2e.py`; `aperio next` has
additional CLI tests for read-only routing and resume behavior.

## D. Verification evidence

- **Current (1.2 hardening audit, macOS arm64):** **1,691 passed, 0 skipped**
  on CPython **3.11, 3.12, 3.13 and 3.14**, no flake after the lock TOCTOU
  fix. Clean isolated `uv tool install` (wheel and pinned git commit) and
  `pipx` install verified outside the repo, including upgrade and uninstall.
  The remediations added 54 regression tests, each written before its fix and
  failing first: R01–R03 (wizard init consent, install-approval fingerprint
  binding, task-size checks on create/approve/dispatch), R04–R05 (fail-closed
  wizard consent, whole-section atomicity for a Hero Section), and R06–R07
  (deadline-bounded subprocess drain, `/back` that re-prompts instead of
  overwriting), and R08–R09 from the Codex re-review (bounded work that names a
  product, and scope hidden in native plan `acceptance_criteria`), and R10
  (verification criteria wrongly counted as implementation breadth), plus
  R11–R12 (test-only work as a slice, `/back` returning to the intent
  chooser), and R13–R14 (goal assessment for declared decompositions, recovery
  report agreeing with the task-wide ceiling), and R15 (an interrupt leaving a
  captured child running, the guard for a child that never started, and the
  breadth marker governing only its own object, and dimensional adjectives not
  counting as breadth). See [audit-final.md](audit-final.md).
- **Earlier integrated run (historical):** Python 3.14.7 (repo venv) — 1601
  passed, 0 skipped after integrating v1.2 and Step 01. The 1.1 line passed
  1344 tests on Python 3.12.14; those counts are historical.
  - A clean 1.2.0rc2 wheel was installed into a fresh Python 3.14 venv;
    `--version`, `next` on an empty workspace, and `host-doctor --json`
    passed; the wheel contained all 11 skill packs and 14 benchmark cases.
- **CI:** GitHub Actions executed **zero** test steps for the entire
  0.13→1.2 series — the account billing lock ("The job was not started
  because your account is locked due to a billing issue"). This is an
  external blocker; owner action at github.com/settings/billing. No CI
  success is claimed anywhere.
- **Live-provider tests:** none (no credentials). Provider adapters are
  verified against fake executables exercising the real process
  boundary (tests/test_provider_adapters.py,
  test_provider_metadata_bounds.py). `host-doctor --probe` and
  `agent-doctor` are passive and never log in.
- **Python 3.11/3.13, Windows:** declared supported, not installed/
  tested on this machine.

## E. Independent review status

Automated third-party review of this branch ran eight rounds. Every finding
was reproduced first, fixed with a regression test that failed before the fix,
and re-checked by the following round. The final round, against the audited
code commit `5715380`, reported **no findings**.

## F. Open risks

| Risk | Severity | Evidence | Impact | Next action |
| --- | --- | --- | --- | --- |
| CI billing lock (zero CI runs) | high (verification gap, not a code defect) | `gh run list` — every run fails at start | No independent CI verification; local-only evidence | Owner resolves billing; re-run the matrix |
| Timing-sensitive concurrency tests flaked twice under heavy local load (planstore unique-ids; intake suite once) | medium | master-plan-v2 Chunk 02 note | Possible intermittent CI failure after unblocking | De-flake (widen bounded waits or mark retry) |
| Keyword triage misroutes | medium (by design, labeled) | test_intake.py honesty labels | An operator may misread routing as understanding | The honesty label renders everywhere; skill/prompt guidance reinforces review |
| Live provider execution unverified | medium | providers.md states it plainly | Adapters may need real-world flag drift fixes | Run `agent-doctor` + authorized dispatch with credentials when available |
| Retry signature granularity | low (documented) | recovery.py docstring; troubleshooting.md | Two causes with identical recorded presentation share one budget; same cause presenting differently does not | Treat recovery-report as the human-readable evidence; refine normalization only with evidence |
| 3.11/3.13/Windows untested | low-medium | release notes, install.md | Unknown platform behavior | Extend the verification matrix when environments exist |
| Loopback UI token visible to same-machine processes | low | limitations.md | Local-only exposure | Run only on trusted machines; CLI is the alternative |

## G. Merge status and first commands

**Merged.** PR #1 merged into `main` on 2026-11-11; merge commit
`6399a81eb2040b83dbc71cf3983049fcd813fe15`. The 49-commit audit history is
preserved (merge commit, not squash). `main` carries no branch protection, so
no rule or required check was bypassed; hosted CI is red only from an account
billing lock (every job fails in ~2 s with 0 steps executed).

Verified on `main` after the merge: 1691 tests × 4 interpreters, clean wheel,
clean `uv`/`pipx` installs, `--version`, `--help`, `next`, `host-doctor`, a real
PTY onboarding run (decline changes nothing; accept scaffolds and records
intent), resume after `/quit`, portable export→import, and honest recovery.

**Commands to run first**

```bash
uv tool install "git+https://github.com/SHAHID8142/aperio-next.git@6399a81"
aperio --version            # aperio 1.2.0rc2
aperio host-doctor          # honest per-agent compatibility, no probes by default
aperio next .               # read-only: what Aperio would do here
aperio start .              # guided onboarding (TTY); asks before writing .aperio/
```

## H. Release verdict

**RELEASE CANDIDATE — EXTERNAL VALIDATION PENDING.**

The documented local product workflow is installed, tested, and usable
(the integrated suite on Python 3.14, two interpreters through the 1.1
programs, clean wheel-install journey), and every external-integration
boundary (CI, live providers, additional platforms) is stated with its
blocker rather than claimed. What
remains before unrestricted deployment claims: CI execution (billing),
live provider validation, and the additional platform matrix.

## Audit reproduction guide

```sh
git clone https://github.com/SHAHID8142/aperio-next && cd aperio-next
git checkout main
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # full integrated suite
.venv/bin/python -m pytest tests/test_roadmap.py tests/test_recovery.py tests/test_hostcompat.py
.venv/bin/python -m pytest tests/test_onboarding_cli.py tests/test_onboarding_e2e.py tests/test_onboarding_security.py
.venv/bin/python -m aperio next . --json              # canonical read-only entry
.venv/bin/python -m aperio host-doctor --json          # compatibility matrix
.venv/bin/python -m aperio --help                      # full command surface
python -m pip wheel . --no-deps -w dist/               # packaging check
```
