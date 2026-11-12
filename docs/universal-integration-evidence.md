# Universal agent integration — verification evidence

Branch `feat/universal-agent-integration` (base: main @ `9d4816b`,
1.2.0rc2). Recorded 2026-11-12 on macOS arm64 (Darwin 27.2.0). Every claim
below names the exact command that produced it; anything not executed is
named as a gap, not implied.

## Environment baseline

| Fact | Evidence |
| --- | --- |
| main @ 9d4816b = 1.2.0rc2, PR #1 merged | `git log --oneline -3`, `gh pr list --state all` |
| 1691 baseline tests green | `.venv/bin/python -m pytest` → `1691 passed in 78.15s` |
| Hosts present | `~/.zcode`, `~/.claude`, `~/.config/opencode`, `~/.codex`, `~/.agents`, `~/.gemini`, `~/.cursor` plus `~/.cline`, `~/.goose`, `~/.kilocode`, `~/.continue`, `~/.kiro`, `~/.qoder`, `~/.roo`, `~/.trae`, `~/.junie`, `~/.openhands`, `~/.augment`, `~/.autohand`, `~/.openclaw`, `~/.grok`, `~/.hermes`, `~/.windsurf`, `~/.rovodev`, `~/.copilot`, `~/.crush`, `~/.codebuddy` dirs (26 skill homes); `claude 2.1.322`, `opencode v2.0.32`, `cline 3.0.74`, `goose 1.55.0`, `kilocode 7.8.15`, `hermes v0.22.5`; codex/gemini/cursor executables absent |
| Legacy artifact | `~/.claude/skills/aperio/` = old "Aperio v2" skill (2026-11-01), separate product lineage; **left in place** (no name collision with `aperio-next`; removal is an owner decision) |

## Scenario A — fresh installation (clean room)

Isolated `HOME=/tmp/aperio-npm-home`, `npm_config_prefix=/tmp/aperio-npm-prefix`:

```
npm install -g dist/aperio-next-1.2.0-rc.2.tgz   → added 1 package
apario-next install
  → Python runtime found (python3.14)
  → Engine environment created; Engine verified: aperio 1.2.0rc2
  → Skills registered: Claude Code (2 files), OpenCode (2 files)
  → Detected-but-pending: ZCode, Codex, Gemini, Cursor (hosts absent in the
    sandbox — honest pending, nothing fabricated)
```

Two real defects were caught by this clean-room run and fixed before any
success claim: `cand.args.concat` TypeError in python discovery (twice).

## Scenario B — native activation

| Host | Registration | Activation |
| --- | --- | --- |
| ZCode | installed at real `~/.zcode/skills/aperio-next/SKILL.md` + `~/.zcode/commands/aperio-next.md`; frontmatter valid (name+description, 533 chars ≤ 1024 limit, regression-tested) | registration/discovery-verified; explicit in-session re-invocation not run in a fresh model turn on this machine |
| Claude Code v2.1.322 | registered (`~/.claude/…`, skill + command) | **live end-to-end**: skill discovered, verified engine invoked, `aperio next --json` → `aperio_project` |
| OpenCode v2.0.32 | registered (`~/.config/opencode/…`, skill + command) | **live end-to-end**: same, `aperio_project` |
| Cline 3.0.74 | registered (`~/.cline/skills/aperio-next/SKILL.md`) | **live end-to-end**: skill auto-loaded, engine invoked, `aperio_project` |
| Goose | registered (`~/.goose/skills/…`; also visible via `~/.agents/skills`) | **discovery live** (`goose skills list` shows `aperio-next`); model turn blocked by provider 500 |
| Codex / Gemini / Cursor | files written at documented locations | **not exercised in a live model turn** (no launchable binary here) — honest gap |
| 20 Agent Skills hosts (Kilo, Continue, Kiro, Qoder, Roo, Trae, Junie, OpenHands, Augment, Autohand, OpenClaw, Grok, Hermes, Windsurf, Rovo Dev, Copilot, Crush, CodeBuddy) | registered at `~/.<tool>/skills/aperio-next/SKILL.md` on the real machine | registration/discovery-verified; no live model turn (binary absent or provider billing/500 blocked the turn) |

Engine invocation from the protocol (any host): verified — the skill's
commands were executed live in this session (`aperio next --json`, `memory`,
`work-status`, `recover`, full workflow below).

## Scenario C — new project; D — existing project; F — persistence

Scratch project `/tmp/aperio-scenario-proj` (README.md + app.py):

```
aperio next . --json
  → classification: existing_non_aperio, action: choose_intent (read-only)
apario onboarding-start . → intent menu, zero mutation
apario onboarding-confirm-init . --approve --intent organize
  → "Project context scaffolded (nothing existing was overwritten)"
apario onboarding-reconstruct . → 1 document inventoried, gaps listed
apario onboarding-answer . <18 required questions>
apario onboarding-readiness . → READY_WITH_PENDING (deferred answers recorded as such)
apario integrations install --scope project .
  → .zcode/skills/aperio-next/SKILL.md written INSIDE the managed project
Fresh-session simulation (cwd=/tmp):
apario next $P → resumes protocol; apario memory $P → durable context loaded
```

## Scenario E — real coding task through the orchestration workflow

Task: "add greet(name) to app.py, verified by execution". Full engine path:

```
decision-add DEC-0001 → plan-create (schema-validated; 3 rejections learned:
  task fields, priority vocab, evidence fields) → plan-approve rev 1
  ("operational marker; genuine human review is the operator's
  responsibility") → run-start TASK-001 --agent manual (RUN-0001,
  read-only handoff prepared) → [implementation] → acceptance executed:
  `python3 -c "from app import greet; assert greet('Aperio') ==
  'hello, Aperio!'" → hello, Aperio!` → review-submit refused for
  unassigned actor → policy-init + policy-assign (planner/implementer/
  reviewer) → review-submit refused for wrong-role actor → refused for
  non-implementer identity → submitted as recorded implementer
  (manual:RUN-0001) → review-decide approved by independent-reviewer
  → task-status completion refused twice (evidence not bound to review;
  limitations mismatch) → completed ONLY with the reviewer-approved
  evidence+limitations verbatim and --result passed
  → handoff-write (schema-validated, 7 sections) → run-report recorded
  ("external result recorded (completed). Task verification is still a
  separate, manual Feature-03 step.") → runs-reconcile: no incomplete runs
  → checkpoint-write (rev 8) → recover: "Status: current; Verified
  completed work: 1 task(s) with recorded evidence"
```

Gates that fired (all correct, none bypassed): governance required for
review; role-permission check; implementer-identity binding; evidence
binding to the approved review; `--result passed` requirement; handoff
schema validation.

## Scenario H — lifecycle

- Idempotent reinstall: test suite (`test_install_is_idempotent`) + live
  re-install over existing assets on the real machine.
- Foreign-file conflict: refusal with the file named
  (`test_install_refuses_foreign_file`, `test_remove_keeps_foreign_file_and_reports_error`).
- Doctor matrix: verified in the clean room (`apario-next doctor`).
- Uninstall: managed-only removal verified; engine root removed; project
  data explicitly preserved.

## Scenario I — failure recovery

- Missing Python 3.11+: `env -i PATH="/usr/bin:/bin" node … install` →
  `apario-next: error: Python 3.11 or newer is required but was not found.
  Install it from https://www.python.org/downloads/ … then re-run` (first
  attempt with /usr/local/bin on PATH found uv's python3.14 — noted, not
  claimed as recovery).
- Corrupted session file: `apario onboarding-status` → "corrupt (invalid
  JSON …); aperio will not modify it. Inspect or restore the file
  manually." (file restored afterwards).
- Session-less project status/intent: actionable errors, no tracebacks
  (regression-tested).

## Scenario J — clean-room repeatability

The Scenario A environment was created fresh (no developer state) for every
clean-room run; the full `install → doctor → uninstall` round trip was
repeated after each launcher fix. Scenario C/E ran against a scratch
project with no developer-machine dependencies.

## Test totals

- Baseline: 1691 passed (main @ 9d4816b).
- After universal-integration: **1713 passed** = +20 integrations tests,
  +2 onboarding regression tests; zero regressions.
- After P0 remediation (FIX A–E): **1747 passed** (`1747 passed`),
  **16 Node launcher tests** (`node --test packaging/npm/test/`, all pass).
  The earlier "13 Node tests" figure is superseded.
- After final hardening (this round, macOS arm64, Python 3.14.9):
  **1763 passed** (`.venv/bin/python -m pytest`), **17 Node launcher tests**
  (`node --test test/` in `packaging/npm`, `pass 17 / fail 0`). Additions this
  round versus 1747: a parametrized legacy-PATH collision test covering both
  skill and command assets for md and toml hosts (+7), onboarding
  integration contract tests (+4), a rewritten/added pair of launcher
  uninstall tests for the missing-engine case, catalog tests locking in
  the expanded 26-host set (+3), and manifest-recording tests (+2).

## Host catalog expansion (this round)

The six originally documented hosts were extended to **26** so that the many
agent tools already installed on a real machine receive the same managed
skill. The additional 20 all use the open **Agent Skills** convention
(`~/.<tool>/skills/<name>/SKILL.md`) — the exact layout/frontmatter aperio
already emits and the same per-agent directory catalog the open `skills` CLI
(which Cline itself forwards to via `cline skill`) targets. On the real
test machine all 26 home directories exist and each received a managed
`aperio-next/SKILL.md`; `remove` deleted exactly those files and preserved
unrelated sibling skills. Live model-turn activation was reproduced in
Claude Code, OpenCode and Cline; Goose discovery was confirmed live.
Windows/Linux and live turns for the remaining hosts are not claimed.

## P0 remediation record (FIX A–E)

This round reproduced and fixed additional release-blocking defects after the
1713-test state. Each fix carries failing-first tests.

| Fix | Root cause | Files | Proof |
| --- | --- | --- | --- |
| A — engine identity | command templates hard-coded bare `aperio`, so a legacy `~/.local/bin/aperio` could be invoked | `render_command_body(..., engine_path=...)`, assets `*.md`/`*.toml`; `install_integrations` refuses an unvalidated bare engine | `test_render_command_body_embeds_engine_path_{md,toml}`, `..._quotes_paths_with_spaces`, `test_install_command_never_embeds_legacy_path_over_isolated`, `test_legacy_path_never_leaks_into_skill_or_command` (parametrized over skill+command for md/toml hosts), `test_install_refuses_when_engine_is_unvalidated_bare_name` |
| B — safe uninstall | `cmdUninstall`/CLI accepted unparseable engine output and project-instruction-only failures exited 0; symlinked managed files were silently unlinked; a missing engine with live registrations deleted the root and orphaned them | `packaging/npm/bin/aperio-next.js`, `cli.py` `remove`, `integrations._delete_managed_file` | `uninstall keeps the root when the engine executable is broken`, `...prints garbage (exit 0)`, `uninstall refuses to orphan registrations when the engine bin is missing`, `uninstall removes the root when the engine bin is missing and nothing was registered`, `test_remove_refuses_symlinked_managed_file`, `test_cli_remove_project_instruction_failure_is_nonzero` |
| B — removal-failure honesty | the previous launcher test accepted `exit 0` as a pass, so a silent half-removal could go green | `packaging/npm/test/launcher.test.js` | rewritten `uninstall keeps the engine and exits nonzero when host removal fails` — asserts exit 1, engine kept, foreign blocker still present (fails first without the guard) |
| C — automatic project integration | the durable pointer required a separate manual command | `onboarding.confirm_initialization(integrate=...)` calls `_persist_project_integration`; `--no-project-integration` opt-out; reconstruction strips the owned section | `test_confirm_init_writes_managed_project_pointer`, `..._no_project_integration_opt_out`, `..._preserves_existing_agents_md`, `..._pointer_is_idempotent`, `test_reconstruction_ignores_managed_pointer` |
| D — stronger tests | a wheel-build failure `skip`ped instead of failing; uninstall tests did not prove the intended failure | `tests/test_packaging.py` (`_build_wheel` asserts), `tests/test_integrations.py` | build failure now fails; `test_cli_remove_project_instruction_failure_is_nonzero` asserts the AGENTS.md/Is-a-directory error and that nothing was destroyed |
| E — docs | stale counts and over-broad activation claims | `README.md`, `docs/install.md`, `docs/integrations.md`, this file, `docs/final-handoff.md` | this section; live-vs-registration wording kept separate |

## Not verified / open gaps

- Live in-session activation outside **Claude Code v2.1.322**, **OpenCode
  v2.0.32** and **Cline 3.0.74** (these three were exercised end-to-end:
  skill discovery → verified-engine invocation → `aperio next` →
  `aperio_project`). The remaining hosts are registration/discovery-verified
  only on this machine (no launchable binary, or the model turn was blocked
  by provider billing — Hermes HTTP 402, Goose HTTP 500, Kilo "add credits",
  `pi` no API key, `agy` headless permission prompt). Goose discovery is
  confirmed live via `goose skills list`.
- Windows and Linux execution (launcher + engine).
- npm/PyPI publication (requires owner authorization); `npx aperio-next` is
  **not** publicly published.
- CI (GitHub Actions billing lock persists — zero hosted runs; all
  verification above is local).
- Removal of the legacy `~/.claude/skills/aperio/` v2 skill (owner
  decision; reversible; no conflict with `aperio-next`).
