# Universal Context Packs (M1)

`aperio context-pack` prepares a portable, inspectable snapshot for **any**
AI agent working on **any** knowledge-work task. It is a context-preparation
tool, not an agent, execution engine, approval system, or performance claim.

## Command

```text
aperio context-pack [WORKSPACE] [options]
```

`WORKSPACE` is optional. Without it, the command runs standalone and requires
no `.aperio/` directory, plan, repository, provider, or database.

| Option | Meaning |
|---|---|
| `--objective TEXT` | Task objective as UTF-8 text |
| `--objective-file FILE` | Read the objective from a UTF-8 file (max 64 KiB) |
| `--profile general\|software` | Context profile; `general` is the default |
| `--budget BYTES` | Context-item content budget (0..1 MiB; default 8192) |
| `--brief TEXT` / `--brief-file FILE` | Optional operator brief (max 32 KiB) |
| `--constraint TEXT` | Explicit requirement/constraint; repeatable |
| `--assumption TEXT` | Explicit assumption; repeatable |
| `--risk TEXT` | Explicit risk; repeatable |
| `--quality-criterion TEXT` | Explicit quality criterion; repeatable |
| `--expected-output TEXT` | Expected deliverable/result |
| `--output-format TEXT` | Expected structure/format |
| `--audience TEXT` | Intended audience |
| `--missing-information TEXT` | Known unknown; repeatable |
| `--imported-context FILE` | Operator-selected external material, labeled `imported`; repeatable |
| `--proposed-context FILE` | Operator-selected agent claim, labeled `proposed`; repeatable |
| `--json` | Emit the JSON pack instead of Markdown |

Exactly one of `--objective` / `--objective-file` should be used. In
project-backed mode the objective may be omitted when the current plan or a
non-template structured handoff supplies one; the pack records that fallback
in `objective_source`.

Errors use the normal Aperio form (`aperio: error: ...`), never a traceback.
The command is read-only: it does not initialize, repair, export, import, or
modify the workspace.

## Examples

### Graphic design

```sh
aperio context-pack \
  --objective "Design a calm identity for a community garden" \
  --constraint "Use accessible contrast and large type" \
  --quality-criterion "Legible at small sizes" \
  --expected-output "One visual direction plus a poster layout" \
  --audience "Neighborhood residents"
```

### Research

```sh
aperio context-pack \
  --objective "Summarize the evidence on urban heat islands" \
  --constraint "Cite every factual claim and its date" \
  --quality-criterion "Separate evidence from interpretation" \
  --missing-information "Local temperature data is not available"
```

### Writing

```sh
aperio context-pack \
  --objective "Write a 700-word explainer about sleep debt" \
  --constraint "Use plain language and no medical advice" \
  --expected-output "A titled article" \
  --audience "General readers"
```

### Business analysis

```sh
aperio context-pack \
  --objective "Assess whether to open a second store" \
  --assumption "Current revenue stays within 10%" \
  --quality-criterion "Every calculation is reproducible" \
  --expected-output "Recommendation with assumptions and risks"
```

### General / conversational

```sh
aperio context-pack \
  --objective "Compare two scheduling approaches for a small team" \
  --quality-criterion "State trade-offs and unknown constraints"
```

### Project-backed

```sh
aperio context-pack ./my-project \
  --objective "Prepare the next design review" \
  --profile general
```

Readable project inputs are limited to the shareable surface:

- `.aperio/brief.md`
- `.aperio/decisions.md`
- a non-template `.aperio/handoff.md`
- plan and execution summaries
- review records that are still applicable to the current work attempt
  (see [review applicability](#review-applicability))

Every read is optional. A damaged optional artifact is skipped with a warning;
the rest of the pack remains usable. State itself must be valid and supported.

### Optional software profile

```sh
aperio context-pack ./my-project \
  --profile software \
  --objective "Review the implementation plan"
```

The `software` profile is explicit. It does not change the trust model or
grant extra authority; it only renders software-aware guidance. A full
configurable domain-profile registry belongs to M2.

### Imported and proposed material

```sh
aperio context-pack \
  --objective "Assess the vendor proposal" \
  --imported-context vendor-notes.txt \
  --proposed-context agent-summary.txt
```

These files are read only because the operator names them. Their paths and
filenames are not copied into the pack. Imported material remains
semantically untrusted; proposed agent claims are not facts.

## Output and hash contract

The JSON envelope is:

```json
{
  "format": "aperio-context-pack",
  "format_version": 1,
  "generated_at": "2026-01-01T00:00:00Z",
  "aperio_version": "0.8.0",
  "content_sha256": "…",
  "content": { }
}
```

`content` contains:

- `objective`, `objective_source`, and `profile`;
- `operator_brief`, `constraints`, `assumptions`, `risks`;
- `quality_criteria` and `output_contract`;
- `missing_information` and `completeness`;
- selected `context_items` with source, provenance, trust, sensitivity, and
  content bytes;
- `excluded` counts/reasons and `budget` accounting;
- `warnings` with safe, bounded codes/details.

`excluded.by_reason` mixes three kinds of exclusion: source-level
(`unsupported_schema`, `unreadable`, `malformed_source`, `duplicate`,
`unsafe_symlink`, `source_limit`), selection-level (`budget`, `item_limit`,
`sensitivity`, `invalid_content`, `oversize`), and review applicability
(`superseded`, `previous-plan-revision`, `no-approved-plan`, `unattributed`,
`previous-attempt`, `evidence-changed`, `not-independent`,
`task-not-active`, `unknown-task`, `plan-scoped`).
`budget.items_excluded` counts only the candidate context items that were
not selected, so it can be smaller than `excluded.total`.

`content_sha256` is SHA-256 over canonical UTF-8 JSON of `content` only.
`generated_at` and `aperio_version` are generation metadata and are excluded,
so identical inputs produce the same content hash at different times.

Markdown is rendered from the same `content`. The renderer re-verifies the
hash before emitting anything.

## Trust labels

| Label | Meaning |
|---|---|
| `authoritative` | Operator instruction/requirements, operator brief, or approved plan |
| `verified` | Approved independent review that is still applicable to the current plan revision, work attempt, and task revision |
| `attested` | Operator/project record that is not independently verified (brief, handoff, decision log, execution metadata) |
| `proposed` | Agent-generated or otherwise unverified claim |
| `imported` | External material; structurally accepted but semantically untrusted |

### Review applicability

An approval is only current while it still applies. `verified` review context
is limited to the **latest** review for a task at the current material plan
revision that is `approved` *and* matches the task's current work attempt and
task revision — the same conditions Feature 06 enforces for completion.

Every other record is historical and is never shown as current verification.
It is counted under `excluded.by_reason` with one of:

| Reason | Meaning |
|---|---|
| `superseded` | a newer review for the same task exists |
| `previous-plan-revision` | recorded against an earlier material plan revision |
| `no-approved-plan` | no approved plan revision is current |
| `unattributed` | predates attempt-bound attribution |
| `previous-attempt` | covers an earlier attempt (task reopened or reclaimed) |
| `evidence-changed` | work or evidence changed after submission |
| `not-independent` | decided by its own submitter or implementer |
| `task-not-active` | the task is no longer in a state the approval covers |
| `unknown-task` | targets a task that is not in the current plan |
| `plan-scoped` | targets the plan, not a task |

When the latest review for a task is `pending`, `changes-requested`, or
`rejected`, that decision is included as an **`attested`** `review-decision`
item (with its findings) and the older approval is excluded as `superseded`.
A newer decision therefore never lets a stale approval look current, and a
task that is reopened or whose evidence changes loses its verified review
context until a fresh review is approved.

Classification is read-only. Immutable review records are never modified,
and the Feature 06 completion gate is unchanged — this classification mirrors
it, it does not replace or relax it.

Historical decision-log entries are **not** upgraded to verified facts.
Repetition, model confidence, and self-evaluation never promote a claim.
Imported content keeps its `imported` status.

Trust labels describe provenance and review status. They are **not**
authentication and do not guarantee correctness.

## Budget policy

`--budget` limits the **UTF-8 content bytes of selected context items** only.
The scope is reported as `selected_context_content_utf8_bytes`.

- Objective, explicit requirements, output contract, completeness, and
  missing-information fields are outside the budget and are never silently
  truncated.
- An item is included whole or excluded whole. A statement is never cut
  mid-sentence.
- Deterministic priority: operator brief, project brief, handoff, plan,
  execution metadata, approved reviews, relevance-ranked decisions, then
  imported/proposed background.
- Tie-breaking is stable; no embeddings, vector store, remote search, or LLM
  ranking is used.
- Over-budget items are counted under `excluded.by_reason.budget`; excluded
  content is never echoed.

## Privacy boundaries

The pack never includes:

- `.aperio/local/`;
- credentials, environment secrets, or private configuration;
- raw provider output or machine-local logs;
- absolute local paths in system-generated fields;
- hidden or unrelated material.

Operator-supplied objective/brief/context is included because the operator
explicitly selected it. Imported/proposed files are read only when named on
the command line. Review the generated Markdown before pasting it into any
external service; context instructions cannot sandbox or control the
receiving model.

## What this does not do

- It does not execute the task or launch an agent/provider.
- It does not approve, verify, or complete project work.
- It does not create canonical memory events (that is future work).
- It does not prove performance improvement; controlled evaluation is
  required.
- It does not provide a full configurable domain-profile system (M2) or a
  second memory implementation.

## Concurrency and snapshot limits

A project-backed pack is a **best-effort read-only snapshot**, not an atomic
cross-artifact transaction. A concurrent Aperio writer may update one
artifact while the pack is reading another, so a pack assembled during active
writes can contain a slightly older brief, handoff, or plan summary. No lock
is taken (and no writer is blocked). Generate a fresh pack after a write
completes if cross-artifact consistency matters.

## Backward compatibility

M1 adds no schema migration, no required artifact, no third-party runtime
dependency, no provider requirement, and no change to plan approval,
governance, export/import, or task-completion semantics. Existing Features
01–07 remain available, and the software workflow remains an optional
profile.
