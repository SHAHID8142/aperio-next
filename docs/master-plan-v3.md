# Master implementation plan v3 — product integration directive (2026-11-11)

Durable record for the third master directive ("Complete Product
Integration, Universal Agent Compatibility, Secure Autonomous
Workflows, and Release Readiness"). Companion to
[master-plan-v2.md](master-plan-v2.md) (1.1.0rc1) and the audit entry
point [final-handoff.md](final-handoff.md). Chunks follow the
micro-execution rules: smallest meaningful capability, focused tests,
regression, one coherent commit each.

## Phase 0 — reconnaissance (read-only, completed first)

- Repo: `github.com/SHAHID8142/aperio-next`; main @ `83af3f5` (1.1.0rc1,
  clean, synced with origin at session start).
- Baselines verified this session: **1344 tests green** on main
  (Python 3.14.7, macOS arm64); **1504 tests green** on the separate
  Step 01 worktree branch (see below).
- A capability audit (12-area, file:line evidence) mapped the directive
  requirements onto the existing 1.1 product. Present (no work needed):
  draft/approval plan gate, evidence-before-completion binding,
  independent-review gate, checkpoints/recover, exchange/portable/
  memory portability, work-status + loopback UI, governance roles,
  integrity, no-secrets tests, packaging. The gap list drove the chunks.

## Parallel-work topology (important for the auditor)

- `feature/step01-universal-onboarding` — a SEPARATE agent's isolated
  worktree implementing Step 01 universal onboarding (read-only
  discovery, adaptive interview, durable sessions, reconstruction,
  standardization, tool discovery with two-gate installs, readiness
  gate, 18 `onboarding-*` commands, security + E2E suites, docs).
  Based on `3515fb8` (before the skills commit); 1504 tests green at
  `6bea2e2`. **Not merged** by this assignment (the other agent owned
  that workspace; merging shared branches was out of scope). Its
  integration — and the `aperio next` canonical entry command that
  routes through its onboarding facade — is the first follow-up chunk.
- `feature/master-v3-completion` — THIS assignment's branch, off main
  @ `83af3f5`.

## Final integration follow-up (completed 2026-11-11)

The previously isolated Step 01 history at `6bea2e2` was preserved on
`feature/step01-universal-onboarding`, pushed to origin, and merged with the
v1.2 completion line on `integration/v1.2-final`. The merge retained both
documentation additions and combined the independently developed CLI changes.
The canonical `aperio next` command was then added as a read-only route through
the onboarding facade, with tests proving equivalence to `onboarding-start`,
no mutation in an empty workspace, and session-safe resume behavior.

## Chunk table (branch `feature/master-v3-completion`)

| Chunk | Directive section | Commit | Content | Tests |
| --- | --- | --- | --- | --- |
| E | §6 two-level planning | `cb417f1` | roadmap-schema v1 (`roadmapmodel.py`, `roadmapstore.py`): objective/lifecycle/milestones, strict input vs tolerant reads, create-only `os.link` publish, lock + byte-CAS + atomic rename, symlink refusal, approval gate with fail-closed attribution + separation of duties; plan-schema v3 optional `milestone` linkage (v1/v2 fully readable; v2→v3 re-stamp migration; portable import accepts all readable versions); `roadmap.*` governance actions with pre-roadmap-policy intrinsic hatch; CLI `roadmap-create/show/approve/revise`; docs/planning.md | +29 |
| F | §10 bounded recovery | `ff01c1c` | `recovery.py`: deterministic failure_signature (task+adapter+exit+normalized outcome — never argv/config/env), MAX 3 attempts/signature, retry_gate in `_dispatch_locked` (previews unaffected), required `--remediation` for retries with verbatim-repeat refusal, exhaustion → escalation guidance (blocked, recovery-report, decision-add, agent-doctor, operator); `recovery-report` CLI; runs-dispatch surfaces gated tasks by stopping | +17 |
| D | §7 host compatibility | `8b96079` | `hostcompat.py`: six-level vocabulary with `native_verified`/`experimental` reserved and never emitted; 11-host documented catalog (no invented APIs); `host-doctor [--probe]` reusing agent-doctor passive diagnostics; honest degradation on probe failure; per-row method/evidence/fallback | +13 |
| G | §17 installation | `9044fbe` | docs/install.md (prereqs, venv recipes macOS/Linux/Windows, wheel sourcing, host-doctor first checks, upgrade/uninstall, platform honesty); verified by clean-venv wheel install (3.14): version/host-doctor/init/status journey + package data present | docs |
| H | §23–24 release gates | this commit | version 1.2.0rc1, release notes, this plan, final-handoff v2 | pinned |

Suite after Chunk D: **1403 passed** (1344 baseline + 59 new). Each
chunk ran the full suite before its commit; test-count claims above
are the deltas.

## Deliberate contract changes (documented, not silent)

- `PLAN_SCHEMA_VERSION` 2 → 3; `PLAN_SCHEMA_READ_VERSIONS` (1,2,3).
  The v3 delta is an optional field, so v2 documents are valid v3
  content: `upgrade.py` registers the explicit v2→v3 re-stamp edge, and
  the portable import path now accepts every readable plan version
  (1.1.0rc1 archives import unchanged). Future-version test pins moved
  to version 4; the compat pin records the new boundary.
- `validate_dependency_graph` gained an `entity` kwarg ("task"/"milestone")
  so the shared graph rules serve both artifacts without message drift.
- Retry contract: a dispatch after a failed run now requires
  `--remediation`. Four pre-existing retry tests were updated to
  declare remediation (the intended new behavior — not a weakened
  check); 17 new tests pin the gate.

## Honest unverified list (unchanged from 1.1 + this work)

- GitHub Actions CI: account billing lock — **zero hosted runs** for
  the whole 0.13→1.2 series. External blocker; owner action at
  github.com/settings/billing.
- Live provider execution (codex-cli/claude-cli against real
  services): unverified (no credentials); adapters verified against
  fake executables at the real process boundary.
- Windows, Linux, Python 3.11/3.13: declared supported, not tested on
  this machine.
- Step 01 onboarding is integrated and locally tested. It is not
  live-verified inside any commercial AI host; the terminal CLI remains the
  only runtime-enforced path.
- `aperio next` is implemented as the canonical read-only entry command and
  routes through the Step 01 facade.

## Requirement → evidence quick map

| Directive requirement | Where | Evidence |
| --- | --- | --- |
| Two-level roadmap/milestone planning (§6) | roadmapmodel/roadmapstore + plan v3 linkage | test_roadmap.py (29), docs/planning.md |
| Approval gates preserved (§3F, §6) | roadmapstore approve/revise; planstore unchanged | test_roadmap.py governance tests |
| Atomic execution + evidence (§9) | planmodel/runtime/reviews (existing, preserved) | test_execution.py, test_evidence_binding.py |
| Bounded retry + escalation (§10) | recovery.py + runtime gate | test_recovery.py (17), docs/troubleshooting.md |
| Compatibility levels + doctor (§7) | hostcompat.py + host-doctor | test_hostcompat.py (13), docs/host-integration.md |
| Honest host claims (§7, §26) | reserved levels never emitted | test_hostcompat.py invariant tests |
| Cross-agent portability (§12) | exchange/portable/memportable (existing) + roadmap now portable | test_roadmap.py roundtrip, test_portable.py |
| Fresh-machine install (§17) | docs/install.md + clean-venv verification | wheel install journey (this session) |
| Release safety (§18-J) | no new deps (stdlib-only unchanged), no-secrets suite green | test_no_secrets.py, pyproject |
| Version/packaging | pyproject 1.2.0rc2 == `aperio.__version__` | test_version.py |
