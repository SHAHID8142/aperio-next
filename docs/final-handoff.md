# Final handoff — Aperio Next 1.2.0rc1 (for independent audit)

Prepared per the product-completion directives. Every claim names its
evidence location; unverifiable items are labeled unverifiable, not
omitted. Chunk-level detail for the 1.2 program lives in
[master-plan-v3.md](master-plan-v3.md); the 1.1 program in
[master-plan-v2.md](master-plan-v2.md).

## A. Exact source identity

- **Repository:** https://github.com/SHAHID8142/aperio-next
- **Branches:**
  - `main` @ `83af3f5` — 1.1.0rc1 (fast-forward pushes only; no
    force-push, ever).
  - `feature/master-v3-completion` — the 1.2.0rc1 release candidate:
    `83af3f5` + `cb417f1` (two-level roadmap) + `ff01c1c` (bounded
    retry) + `8b96079` (host-doctor) + `9044fbe` (install docs) + the
    1.2.0rc1 packaging/handoff commit. **Not merged to main** (merging
    shared branches was outside this assignment's authorization).
  - `feature/step01-universal-onboarding` — a separate agent's Step 01
    universal-onboarding implementation (18 `onboarding-*` commands,
    discovery/interview/reconstruction/standardization/tool-gates,
    security + E2E suites; 1504 tests green at `6bea2e2`). **Not
    merged**; its integration is the first follow-up chunk, together
    with the `aperio next` entry command that routes through its
    facade.
- **Release version:** 1.2.0rc1 (pyproject.toml == `aperio.__version__`,
  asserted by tests/test_version.py)
- **Working tree:** clean at the handoff commit; `dist/` artifacts are
  build outputs and are not committed.

## B. Completed capabilities (feature → modules → commands → guarantee → tests)

### 1.2 additions (this program)

| Capability | Modules | Commands/APIs | Actual guarantee | Limits | Tests |
| --- | --- | --- | --- | --- | --- |
| Two-level planning | `roadmapmodel.py`, `roadmapstore.py`; plan-schema v3 linkage | `roadmap-create/show/approve/revise`; `plan-create --milestone` | LEVEL-1 master roadmap (lifecycle, milestones, capabilities, dependencies, risks, acceptance conditions) draft-until-approved with the plan's governance rules; the detailed plan stays the only execution engine; milestone progress derived from plan linkage, never stored | Roadmap is a planning document, not a scheduler; no roadmap adoption path (recreate under governance) | test_roadmap.py (29) |
| Bounded retry | `recovery.py` + runtime gate | `run-start --remediation`, `recovery-report` | Max 3 attempts per deterministic problem signature; retries require declared new evidence/hypothesis (verbatim repeats refused); 4th attempt refused with escalation guidance (blocked, report, decision-add, doctor, operator); enforced in the Python boundary | Signature keys on recorded outcome text — two causes with identical presentation share a budget; coordination boundary, not a root-cause analyzer | test_recovery.py (17) |
| Host compatibility doctor | `hostcompat.py` | `host-doctor [--probe]` | Six-level vocabulary; `native_verified`/`experimental` defined but **never emitted** (no verified native integration exists; none invented); provider CLIs passively probed; every host names its fallback | A clean probe proves a binary runs, not that a session works; cooperative hosts are `not_yet_tested` by design | test_hostcompat.py (13) |
| Fresh-machine install | docs/install.md | — | Verified clean-venv wheel install (3.14, macOS arm64): version/host-doctor/init/status + package data | Windows/Linux/3.11/3.13 documented as not tested | manual verification |

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
or README "Install (development)". The Step 01 onboarding journey
(new/existing project, interview, readiness) is exercised by the
seven automated scenarios on its branch (tests/test_onboarding_e2e.py)
— pending integration.

## D. Verification evidence

- **Local (this machine, macOS arm64):**
  - Python 3.14.7 (repo venv): **1403 passed** on
    `feature/master-v3-completion` after Chunk D (1344 baseline + 59
    new); each chunk ran the full suite before its commit.
  - Clean venv, built wheel (1.1.0rc1 artifact at audit time — rebuild
    for 1.2.0rc1): install + `--version` + `host-doctor --json` +
    `init` + `status` journey verified; skill packs and benchmark
    cases confirmed as package data. The 1.1 wheel also passed the
    full 1344-test suite on Python 3.12.14 (prior program).
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

## E. Open risks

| Risk | Severity | Evidence | Impact | Next action |
| --- | --- | --- | --- | --- |
| CI billing lock (zero CI runs) | high (verification gap, not a code defect) | `gh run list` — every run fails at start | No independent CI verification; local-only evidence | Owner resolves billing; re-run the matrix |
| Step 01 branch unmerged (integration debt) | high (coordination, not a code defect) | branch list; master-plan-v3 topology section | Two branches to reconcile (cli.py additions on both sides) before `aperio next` exists | Authorized integration: merge main into the Step 01 branch, resolve, run both suites, then add `aperio next` |
| Timing-sensitive concurrency tests flaked twice under heavy local load (planstore unique-ids; intake suite once) | medium | master-plan-v2 Chunk 02 note | Possible intermittent CI failure after unblocking | De-flake (widen bounded waits or mark retry) |
| Keyword triage misroutes | medium (by design, labeled) | test_intake.py honesty labels | An operator may misread routing as understanding | The honesty label renders everywhere; skill/prompt guidance reinforces review |
| Live provider execution unverified | medium | providers.md states it plainly | Adapters may need real-world flag drift fixes | Run `agent-doctor` + authorized dispatch with credentials when available |
| Retry signature granularity | low (documented) | recovery.py docstring; troubleshooting.md | Two causes with identical recorded presentation share one budget; same cause presenting differently does not | Treat recovery-report as the human-readable evidence; refine normalization only with evidence |
| 3.11/3.13/Windows untested | low-medium | release notes, install.md | Unknown platform behavior | Extend the verification matrix when environments exist |
| Loopback UI token visible to same-machine processes | low | limitations.md | Local-only exposure | Run only on trusted machines; CLI is the alternative |

## F. Release verdict

**RELEASE CANDIDATE — EXTERNAL VALIDATION PENDING.**

The documented local product workflow is installed, tested, and usable
(1403 tests on the 1.2 branch, two interpreters through the 1.1
programs, clean wheel-install journey), and every external-integration
boundary (CI, live providers, additional platforms, Step 01
integration) is stated with its blocker rather than claimed. What
remains before unrestricted deployment claims: CI execution (billing),
live provider validation, the additional platform matrix, and the
Step 01 branch integration — all external to this branch's code.

## Audit reproduction guide

```sh
git clone https://github.com/SHAHID8142/aperio-next && cd aperio-next
git checkout feature/master-v3-completion
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # full suite (1403 tests, ~1 min)
.venv/bin/python -m pytest tests/test_roadmap.py tests/test_recovery.py tests/test_hostcompat.py
.venv/bin/python -m aperio host-doctor --json          # compatibility matrix
.venv/bin/python -m aperio --help                      # full command surface
python -m pip wheel . --no-deps -w dist/               # packaging check
```
