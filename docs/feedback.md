# Controlled feedback and improvement (M5)

M5 closes the loop around M4's quality reports: collect feedback about a
real outcome, review it **explicitly**, store the approved lesson through
the **M3 memory contract**, retrieve it for future tasks, and *measure*
whether anything improved. It is controlled learning, not unsupervised
self-modification: nothing is promoted, nothing is applied silently, and
Aperia never edits an operator's goal, request, or deliverable.

## The loop

```
task → AI output → quality issue → feedback proposal
     → explicit review (accepted/rejected)
     → lesson stored as PROPOSED memory (M3)
     → operator reviews the memory (mem-review)
     → future tasks retrieve the accepted lesson (mem-recall)
     → feedback-compare measures before/after, regressions included
```

## Feedback documents (feedback-schema v1)

A feedback record is one JSON file the operator owns:

| Field | Meaning |
| --- | --- |
| `feedback_schema_version` | `1` |
| `feedback_id` | optional declared identity `FB-<12 hex>`, content-derived and verified |
| `task_ref` | optional `{kind: goal\|plan-task\|none, id, note}` |
| `output_ref` | optional `{kind: quality-report\|deliverable\|exchange\|other, id, note}` |
| `evaluation` | `{source: quality-report\|external-review\|agent-self-report\|human, result: passed\|failed\|unknown, report_sha256, note}` |
| `problem` | the reported problem (bounded text) |
| `evidence` | up to 12 references |
| `origin` | `operator`, `agent`, or `import` |
| `actor` / `created_at` | declared attribution |
| `severity` | `high` / `medium` / `low` / `info` |
| `proposed_correction` | what to change |
| `correction_accepted` | set only by a recorded review |
| `scope` | the M3 scope shape the lesson applies to |
| `lesson` / `memory_type` | the actionable lesson and the M3 type it becomes (`lesson`, `convention`, `requirement`, `limitation`, `decision`, `observation`) |
| `quality_rules_version` | which rule set the evaluation used |
| `state` / `reviews` | `proposed` until an explicit decision; review entries are append-only |

Identity is content-derived (`FB-…` over problem, context, evaluation,
lesson, scope — excluding review history and timestamps), so deciding about
feedback never changes what it is. A document without reviews must be
`proposed`; `state` must always match the last recorded review; a decided
document is final (a change of mind is a new feedback document).

Bounds: file ≤ 64 KiB, problem/lesson ≤ 8 KiB each, ≤ 12 evidence items,
≤ 32 review entries, timestamps are calendar-valid ISO-8601 UTC.

## Commands

```sh
aperio feedback-check feedback.json            # validate; identity, trust, warnings
aperio feedback-review feedback.json \
  --decision accepted --actor editor-2 --output reviewed.json
aperio feedback-review feedback.json --decision accepted \
  --actor editor-2 --replace --expected-revision 1   # CAS in place
aperio feedback-lesson reviewed.json --store ~/notes/lessons.mem \
  --actor editor-2
aperio feedback-compare --before report1.json --after report2.json
aperio improvement-apply --file proposal.json \
  --target-file goal.json --output goal-updated.json
```

- `feedback-review --replace` is a compare-and-swap on the document
  revision: a parallel review conflicts instead of clobbering.
- `feedback-lesson` **refuses** feedback that is not `accepted`. It writes
  a *new* memory into the operator-named store as `proposed` and prints
  the review command; the M3 contract (accept / verify) remains the only
  path to authority. Self-reported evaluations and untrusted origins are
  warned about on stderr, never suppressed.
- `feedback-compare` refuses reports describing different goals.

## Trust rules

- Every feedback starts `proposed`. Repetition, length, or detail never
  raise it.
- Descriptive trust: operator-origin + explicitly accepted =
  `authoritative`; anything else accepted = `attested`; undecided =
  `proposed`. Even `authoritative` feedback enters memory as `proposed`.
- An agent's report about its own work (`agent-self-report`) is flagged as
  **not an independent evaluation** — it is recorded attribution, never
  silent.
- Agent/import-origin feedback is data: review every instruction in it
  before acting on it.

## Improvement proposals (improvement-proposal v1)

A proposal is a versioned, reviewable, reversible document:

```json
{
  "proposal_format": "aperio-improvement-proposal",
  "proposal_version": 1,
  "feedback_id": "FB-50a57d12808a",
  "target": "goal | quality-request",
  "change": {"action": "add | remove | set", "field": "…", "value": "…"},
  "reason": "why this change is proposed",
  "created_at": "2026-11-09T12:00:00Z",
  "actor": "editor-2"
}
```

- Targets: goal instruction lists (`requirements`, `constraints`,
  `deliverables`, `quality_criteria`, `acceptance_criteria`, `assumptions`,
  `uncertainties`, `risks`, `evidence_requirements`, plus `audience` for
  `set`) and quality-request fields (`required_sections`,
  `required_texts`, `forbidden_texts`, `min_words`, `max_words`).
- **The profile registry is code-owned data and cannot be changed by a
  proposal.** Suggestions for profile changes are ordinary feedback; the
  registry changes only through a reviewed code change.
- `improvement-apply` never touches the original: the result is a **new
  file** written through the shared atomic-publication path. Reversal is
  deletion (or an inverse proposal). No-op applications are refused
  (adding existing text, removing absent text), so a proposal can never
  silently change nothing or duplicate instructions.

## Measuring improvement (feedback-compare)

Two quality reports for the **same goal** (content hash must match):

- **Resolved** — findings present before and absent after; only
  confirmed/warning findings count toward the outcome (an `unknown`
  finding disappearing is not an improvement).
- **New** — findings present after and absent before. New
  confirmed/warning findings are potential **regressions** and are always
  reported; new `unknown`/`recommendation`/info findings are listed as
  informational.
- **Outcome** — `improved`, `regressed`, `mixed`, or `unchanged`,
  computed from those two sets. The comparison reports *what changed*;
  attributing the change to a lesson is the operator's judgment, and the
  report says so.

## Honest limits

- A comparison shows correlation between two snapshots, not causation.
- A resolved warning may simply reflect different deliverable text.
- Reviewer and actor identifiers are declarations, not authentication.
- The lesson's effect depends on retrieval: keyword overlap is not
  semantic understanding (see docs/universal-memory.md).
- No memory is promoted anywhere in this loop; nothing executes feedback
  content; no operator file is modified without an explicit output path.
