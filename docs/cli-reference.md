# CLI reference

All commands are `aperio COMMAND …`. Errors are one actionable
`aperio: error: …` line (exit 1); usage errors exit 2; interruption
exits 130. Never a traceback. `--json` switches the human-readable output
for the documented JSON contract of each feature. Read-only commands are
marked *(read-only)*.

## Project foundation (Feature 01)

| Command | Purpose |
| --- | --- |
| `aperio --version` | print the package version |
| `aperio init DIR` | create or safely repair `.aperio/` (never overwrites user content) |
| `aperio status DIR` | report project state *(read-only)* |

## Portable project memory (Feature 02)

| Command | Purpose |
| --- | --- |
| `aperio memory DIR [--json]` | read the shareable context *(read-only)* |
| `aperio decision-add DIR (--file F \| --title --context --decision --consequences)` | append one decision (`DEC-NNNN`) |
| `aperio handoff-write DIR --file F [--revision N] [--force]` | validated structured handoff replacement |
| `aperio phase-set DIR PHASE` | compare-and-swap phase update |

## Planning and execution tracking (Features 03–04, 06–07)

| Command | Purpose |
| --- | --- |
| `aperio plan-create DIR --file F` | create the plan document (plan-schema v1) |
| `aperio plan-show DIR [--json]` | show plan, approval, tasks *(read-only)* |
| `aperio plan-approve DIR [--revision N] [--by LABEL]` | record the approval stamp |
| `aperio plan-revise DIR --file F --revises N` | full content replacement with lifecycle guards |
| `aperio task-list DIR [--json]` | tasks with derived readiness *(read-only)* |
| `aperio task-status DIR TASK --status S …` | validated lifecycle transitions (evidence-required completion) |
| `aperio agent-list [DIR] [--json]` | adapter registry |
| `aperio agent-doctor DIR --agent ID [--config F] [--json]` | passive capability probe |
| `aperio run-preview / run-start / run-status / runs-list / run-cancel / runs-reconcile / run-report / run-handoff` | execution records, dispatch (`--authorize-exec` required for real processes; `--authorize-write` for write-capable provider modes) |

## Governance (Feature 06)

`roles`, `policy-init`, `policy-show`, `policy-assign`,
`policy-permissions`, `review-submit`, `review-decide`, `review-list`,
`review-status` — opt-in via `policies.json`; nothing changes without it.

## Portability (Feature 05)

| Command | Purpose |
| --- | --- |
| `aperio export DIR [--output F] [--dry-run]` | whitelisted ZIP archive with a SHA-256 manifest |
| `aperio import DIR ARCHIVE [--replace] [--dry-run]` | full pre-write validation, conflicts preserved |
| `aperio integrity DIR [--archive F] [--json]` | read-only consistency report |
| `aperio upgrade DIR [--apply] [--json]` | explicit migration registry preview/apply |

## Universal layer (M1–M3)

| Command | Purpose |
| --- | --- |
| `aperio context-pack [DIR] [options]` | universal context pack *(read-only)* — see [context-pack.md](context-pack.md) |
| `aperio profiles [--json]` | the six task profiles and their advisory suggestions |
| `aperio goal-check GOAL [--json]` | validate a goal, report gaps *(read-only)* |
| `aperio goal-pack GOAL [DIR] [options]` | goal → context pack *(read-only)* |
| `aperio mem-propose --store F …` | propose a memory |
| `aperio mem-review STORE ID --accept\|--verify\|--retract\|--supersede` | record a review (`--verify` needs `--evidence` + `--actor`) |
| `aperio mem-list / mem-show / mem-recall` | inspect and retrieve *(read-only)* |
| `aperio mem-export / mem-import` | portable memory exchange (v1) |

## Quality and feedback (M4–M5)

| Command | Purpose |
| --- | --- |
| `aperio preflight GOAL [--memory-store F] [--json] [--output F]` | mistake prevention before work *(read-only)* |
| `aperio quality-check GOAL --deliverable F [--request F] [--require-section N] [--require-text T] [--forbid-text T] [--min-words N] [--max-words N] [--no-output-contract]` | deliverable evaluation *(read-only)* |
| `aperio feedback-check F [--json]` | validate a feedback document *(read-only)* |
| `aperio feedback-review F --decision D --actor A [--note N] [--at TS] [--output F \| --replace --expected-revision N]` | explicit review decision |
| `aperio feedback-lesson F --store MEM [--actor A]` | accepted lesson → **proposed** memory |
| `aperio feedback-compare --before F --after F [--json]` | before/after quality comparison *(read-only)* |
| `aperio improvement-apply --file P --target-file F --output NEW [--force]` | apply a proposal to a **new copy** |

## Universal exchange (M6)

| Command | Purpose |
| --- | --- |
| `aperio exchange-export [--goal F] [--context-pack F] [--quality-report F] [--feedback F …] [--memory-store F --memory-id ID \| --memory-tag TAG] [--note T …] [--output F]` | one portable exchange document *(read-only except --output)* |
| `aperio exchange-import F --dest-dir DIR [--memory-store MEM] [--max-sensitivity L] [--apply] [--json]` | preview by default; `imported-*` files; memories enter as proposed |

## Agent guidance (M7)

| Command | Purpose |
| --- | --- |
| `aperio agent-brief GOAL [--task-reference T] [--memory-store F …] [--json] [--output F]` | paste-ready instruction package *(read-only)* |

## Benchmark (M8)

| Command | Purpose |
| --- | --- |
| `aperio benchmark-cases [--cases DIR] [--json]` | list the versioned cases *(read-only)* |
| `aperio benchmark-run [--cases DIR] [--only ID …] [--json] [--output F]` | run the cross-domain benchmark *(read-only)* |

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success (findings are a *report*; a quality report with findings still exits 0) |
| 1 | operational error (unsafe path, unreadable state, refused operation) |
| 2 | usage error (argparse) |
| 130 | interrupted |
