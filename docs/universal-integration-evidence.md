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
| Hosts present | `~/.zcode`, `~/.claude`, `~/.config/opencode`, `~/.codex`, `~/.agents`, `~/.gemini`, `~/.cursor` dirs; `claude 2.1.322`, `opencode v2.0.32`; codex/gemini/cursor executables absent |
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
| ZCode | installed at real `~/.zcode/skills/aperio-next/SKILL.md` + `~/.zcode/commands/aperio-next.md`; frontmatter valid (name+description, 533 chars ≤ 1024 limit, regression-tested) | **this session's host** — assets installed in the live user scope and validated against the host's documented discovery rules; explicit re-invocation in a fresh session is the remaining manual check |
| Claude Code / OpenCode / Codex / Gemini / Cursor | files written at documented locations (real machine: all six hosts) | **not exercised in live sessions** — honest gap |

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
- Final: **1713 passed** (`1713 passed in 78.41s`) = +20 integrations tests,
  +2 onboarding regression tests; zero regressions.

## Not verified / open gaps

- Live in-session activation outside ZCode (needs real sessions in Claude
  Code, OpenCode, Codex, Gemini, Cursor).
- Windows and Linux execution (launcher + engine).
- npm/PyPI publication (requires owner authorization).
- CI (GitHub Actions billing lock persists — zero hosted runs; all
  verification above is local).
- Removal of the legacy `~/.claude/skills/aperio/` v2 skill (owner
  decision; reversible; no conflict with `aperio-next`).
