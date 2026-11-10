# Optional workflow: client website delivery (walkthrough)

This page is a **template layer**, not a new engine. It maps one common
engagement — building and delivering a client website — onto Aperio's
universal primitives: goals, plans, tasks, decisions, quality reports,
feedback, exchange, and portable memory. Three standing rules:

- **It advises; it never overrides.** Operator requirements always
  outrank this template, and nothing here bypasses plan approval or the
  independent-review gates (Feature 06) when they are enabled.
- **The example is a placeholder.** `example-project` is illustrative
  only: no client names, data, metrics, or outcomes, and every
  deployment or report shown is a **simulation sketch**, not captured
  output.
- **Honesty is part of the workflow.** Where Aperio cannot know
  something — whether a site renders correctly, whether a deployment
  worked, whether a client consented — the walkthrough says so.

## Stage overview

| # | Stage | Aperio primitives |
| --- | --- | --- |
| 1 | Client discovery | goal-schema v1, `goal-check`, `preflight` |
| 2 | Proposal and agreement | goal and plan content, `decision-add` |
| 3 | Asset readiness | plan tasks, decisions, the `.aperio/local/` boundary |
| 4 | Blank-folder initialization | `init`, `status`, `memory` |
| 5 | Planning and architecture | `plan-propose-from-goal` (planned), `plan-create`, `plan-approve` |
| 6 | UX/UI design | design sub-goal, `agent-brief`, `quality-check`, reviews |
| 7 | Frontend implementation | task lifecycle with evidence, `quality-check` |
| 8 | Backend and CMS | tasks, decisions, the secret boundary |
| 9 | Integration and QA | recorded evidence, runs, the feedback loop |
| 10 | Client review | `capture` / `inbox`, revisions through plan gates |
| 11 | Staging and production | `run-preview` vs `run-start --authorize-exec` |
| 12 | Production validation | labeled evidence vs claims |
| 13 | Client handover | `integrity`, `export`, delivery checklist |

## 1. Client discovery — capture the assignment

Everything the client says in the first conversation becomes goal-schema
v1 fields (see [goal-contract.md](goal-contract.md)). Write it down
before any work starts:

| Discovery input | Goal field |
| --- | --- |
| Business goals, what success looks like | `objective`, `intended_outcome` |
| Audience | `audience` |
| Scope, features | `requirements`, `deliverables` |
| Technology, budget, deadline limits | `constraints` |
| Ownership and dependencies taken as given | `assumptions` |
| What could break the engagement | `risks` |
| Unknowns (dates, content, third parties) | `uncertainties` |
| What proof must accompany the result | `evidence_requirements` |
| Done when | `acceptance_criteria` |

```sh
aperio goal-check example-goal.json      # gaps reported as gaps, never filled
aperio preflight example-goal.json       # assumptions, risks, conflicts
aperio profiles                          # advisory suggestions per profile
```

Missing information is reported as missing; Aperio never invents a
constraint, deliverable, or acceptance criterion to make a goal look
complete, and profile suggestions are advisory data, never requirements.

## 2. Proposal and agreement — scope on paper, not law

Scope assumptions, acceptance criteria, milestones, revisions,
responsibilities, and exclusions are represented as ordinary goal and
plan content: assumptions → `assumptions` (preflight reports each as
unvalidated); acceptance criteria → `acceptance_criteria` (checked
textually by `quality-check` later); milestones and revisions → plan
tasks and `plan-revise` (Stage 5); responsibilities → goal text plus the
optional governance roles (`planner`, `implementer`, `reviewer` — Aperio
workflow roles, not contractual parties); exclusions → explicit
constraints and a `decision-add` entry.

**This does not generate a legally binding agreement or client consent.**
Goal, plan, and decision files are internal project records. Contracts,
signatures, and consent happen through your own contracting process —
never through an Aperio command, and never implied by a `plan-approve`
stamp or a decision entry.

## 3. Asset readiness — supplied material without secrets

Supplied assets, licensing, content, and access prerequisites are
tracked as plan tasks with preconditions, plus decisions for provenance:

```sh
aperio decision-add . --title "Asset intake" \
  --context "Client supplied logo and imagery for example-project" \
  --decision "License file received; usage rights stated in writing" \
  --consequences "Asset-dependent tasks can start; terms bind future edits"
```

Credentials, tokens, and keys follow one rule: **they never enter
shareable state.** `.aperio/local/` is the only place machine-local and
sensitive data may live, and it is excluded from context packs, portable
export, and every shareable record by construction — not by filtering.
No password, key, or token belongs in a goal, brief, decision, plan,
handoff, or any document you intend to share. Access handoffs go through
your own secure channel outside Aperio.

## 4. Blank-folder initialization — what `aperio init` does and does not do

Starting from an empty folder:

```sh
aperio init .          # creates .aperio/ project state; safe to re-run
aperio status .        # what exists now (read-only)
aperio memory . --json # the shareable context any agent can pick up
```

**`aperio init` creates `.aperio/` project state — it does not scaffold a
website.** No `index.html`, no framework, no CMS configuration is
created. Building the site's own files is the work of the operator and
their agents; Aperio records, checks, and carries that work. `init`
never overwrites existing files and only repairs what is missing.

## 5. Planning and architecture — from goal to approved plan

The goal's optional `work` decomposition is *descriptive* — a plan for a
reader, not an execution lifecycle. The goal→plan bridge converts it
into a plan **proposal**:

```sh
aperio plan-propose-from-goal example-goal.json --output plan-proposal.json
# planned in this program, shipping before release (master-plan-v2,
# Chunk 03); flags shown are illustrative

aperio plan-create . --file plan-proposal.json   # gated create; approval is draft
aperio task-list .                               # readiness is derived, never stored
aperio plan-approve . --revision 1 --by owner    # after genuine human review
```

- **A work breakdown is never presented as an approved plan.** Creation
  goes through the normal `plan-create` gate and starts as a draft.
- **Approval is an operational marker, not cryptographic proof.** The
  operator is responsible for genuine review; Aperio never auto-approves.
- **Every material revision resets approval to draft.** `aperio
  plan-revise . --file plan-v2.json --revises 1` requires renewed
  approval before execution tracking resumes.

Dependencies are validated (unique ids, no cycles); the plan lives in
`.aperio/plan.json` (plan-schema v1). Architecture decisions — data
models, routing, hosting — are recorded with `decision-add`.

## 6. UX/UI design — what a tool can check and what it cannot

Sitemap, navigation, layouts, responsive behavior, hierarchy, design
tokens, typography, color and contrast, component states, interactions,
and accessibility are design work. Aperio supports it as knowledge work,
not as a design engine:

- A design sub-goal (`"profile": "design"`) carries audience,
  deliverables, and constraints; `aperio agent-brief design-goal.json
  --output design-brief.md` produces a paste-ready package for any agent.
- `aperio quality-check design-goal.json --deliverable design-notes.md
  --require-section "Accessibility"` checks that stated rules and
  required sections appear in the written design record.

**Client design approval is operator sign-off** — a person looks at the
designs and decides. Record it as a decision; when governance is
enabled, route the same sign-off through the review gates
(`review-submit`, `review-decide`):

```sh
aperio decision-add . --title "Design sign-off" \
  --context "Home and services layouts presented to client" \
  --decision "Client approved the layouts after one revision round" \
  --consequences "Implementation tasks may proceed against this design"
```

Honest limits: Aperio reads text, not pixels. Measured contrast,
rendered responsive behavior, and interaction feel are judged with
browser tooling outside Aperio; those tools' results are the evidence
you record. An accessibility claim in the design notes is a claim until
someone measures or tests it.

## 7. Frontend implementation — evidence-required completion

Components, routing, client state, responsive UI, and accessibility are
plan tasks. Completion is not a status change alone — it requires
recorded evidence and limitations in the same operation:

```sh
aperio task-status . TASK-002 --status in-progress --owner agent-a
aperio quality-check example-goal.json --deliverable frontend-notes.md \
  --require-section "Accessibility checklist" --forbid-text "REPLACE-WITH-REAL-KEY"
aperio task-status . TASK-002 --status completed --result passed \
  --evidence "quality report on frontend-notes.md: required sections present" \
  --limitations "text-level checks only; rendered pages not inspected by Aperio"
```

`quality-check` verifies what the goal *explicitly, objectively*
requires and labels every finding: **confirmed** (a rule proved it),
**warning** (a signal — judge it), **unknown** (the tool cannot decide —
you decide), or **recommendation** (advice). A clean report means
"nothing explicitly required was found missing"; it is not a certificate
of correctness.

When governance is enabled, completion additionally requires an approved
independent review at the current material revision, submitted by the
implementer and decided by a different actor:

```sh
aperio review-submit . --task TASK-002 --scope "responsive layout" \
  --scope "accessibility checklist" --evidence "quality report FRONT-QA-1" \
  --limitations "text-level checks only" --actor agent-a
aperio review-decide . REV-0001 --outcome approved --actor reviewer-b
```

A decided review is immutable; reworked work gets a new review id. This
workflow never bypasses those gates.

## 8. Backend and CMS — contracts decided, secrets never stored

Data models, API contracts, authorization, validation, uploads and
media, publishing workflows, and error handling are plan tasks. Aperio
does not operate a CMS or test an API. It records contract decisions
(`decision-add`) so future agents inherit the reasoning, tracks each
subsystem with explicit completion criteria, and enforces the secret
boundary: credential-bearing configuration lives outside shareable
state, adapter configs are validated with known keys and never copied
into records, and run records carry only bounded neutral metadata — raw
output, environment values, and paths stay machine-local.

A textual guard on written configuration notes:

```sh
aperio quality-check example-goal.json --deliverable deploy-notes.md \
  --forbid-text "REPLACE-WITH-REAL-KEY" --require-section "Rollback"
```

That proves absence of the literal token in that file — not that no
secret exists anywhere. A scanner run outside Aperio is the right tool
for that, and its output becomes your recorded evidence.

## 9. Integration and QA — recorded evidence, feedback loop

Functional, integration, accessibility, performance, and security
checks produce evidence. Aperio does not run browsers or scanners
itself: run the checks outside Aperio and record the results as task
evidence, or run a configured test command through the `local-process`
adapter with explicit authorization:

```sh
aperio run-preview . TASK-005 --agent local-process --config test-runner.json
aperio run-start . TASK-005 --agent local-process --config test-runner.json \
  --authorize-exec
```

`run-preview` is side-effect-free; without `--authorize-exec` nothing
launches. With the `manual` adapter instead, the run stays
`awaiting-external` until `aperio run-report . RUN-0001 --file
qa-result.json` records the outcome. Exit 0 is never verification or
completion — the evidence and limitations recorded on the task are what
count. Quality reports are derived artifacts: regenerate them, never
edit them. Problems found in QA feed the improvement loop:

```sh
aperio feedback-check qa-feedback.json          # describe problem + lesson
aperio feedback-review qa-feedback.json --decision accepted \
  --actor owner --output reviewed.json          # you decide, explicitly
aperio feedback-lesson reviewed.json --store ~/notes/lessons.mem --actor owner
aperio feedback-compare --before report-before.json --after report-after.json
```

An accepted lesson enters the memory store as a **proposed** memory and
stays there until a recorded review promotes it. Nothing auto-improves.

## 10. Client review — brain dumps, revisions, sign-off

Client feedback mid-implementation arrives as a stream of thoughts. The
Brain Dump Guard (see [brain-dump.md](brain-dump.md)) captures it
without letting it hijack the current task:

```sh
aperio capture . --message "Move the contact form above the fold. The \
  gallery might be nice someday. Also the footer link is broken."
aperio inbox .
aperio inbox-resolve . INT-0001 0 --action acknowledge --actor owner
```

Nothing captured executes; urgency language inside the text has no
authority — only your explicit `--urgent` flag does. Turning a captured
item into work is a separate, reviewed promotion through the plan gates,
so an inbox can never mutate an approved plan silently. Unresolved
issues stay visible: blocked tasks with reasons, open `uncertainties`,
and unaccepted review findings. Sign-off is recorded when the client
(via the operator) confirms — as a decision entry, or as task
completion with the acceptance evidence. This records that sign-off
*happened*; it is not client consent in any legal sense.

## 11. Staging and production — authorization, dry-run versus real

Aperio does not deploy websites. A deployment is a process: run it with
your own tooling and record what happened, or configure it through an
adapter. Either way, any real process launch requires **explicit
`--authorize-exec`** on the dispatch command — without it, dispatch is
a read-only preparation step — and **a dry-run is not a real
deployment**: `run-preview` launches nothing; `--dry-run` on
`export`/`import` writes nothing.

```sh
# SIMULATION sketch — do not treat as captured output
aperio run-preview . TASK-009 --agent local-process --config deploy-staging.json
aperio run-start . TASK-009 --agent local-process --config deploy-staging.json \
  --authorize-exec
```

Before authorizing, run the operator's checklist. Aperio can record the
decision; it cannot perform the checks:

| Check | Owner | Where it is recorded |
| --- | --- | --- |
| Staging vs production configuration | operator | `decision-add` |
| No secrets in state, logs, or deliverables | operator | scan outside Aperio + decision |
| Backup of the current production site | operator | `decision-add` with backup location |
| Rollback procedure written and reachable | operator | task + decision |
| Production environment reachable and correct | operator | recorded evidence after the run |

After any interrupted or ambiguous run, `aperio runs-reconcile .`
reports state without inventing success — and this document makes no
claims of deployment success either.

## 12. Production validation — label every claim

Post-deployment statements fall into four kinds, and Aperio keeps the
labels distinct:

| Kind | What it is | How it is labeled |
| --- | --- | --- |
| Recorded execution | A run through an adapter, with exit code and neutral metadata | run record; still not verification by itself |
| Automated checks | Tool output (smoke suite, link checker) cited as evidence | recorded evidence with limitations on the task |
| Manual acceptance | A human judged the site on production | attested via `decision-add`; Aperio does not verify judgment |
| Unverified claim | An agent asserts "the site is live" with no run or evidence | **proposed** trust; never promoted by repetition |

Never present a proposed claim as evidence, and never let an agent's
confident summary replace a recorded run. Whether the production site
actually works is known only from checks that actually ran and
acceptance that actually happened.

## 13. Client handover — documentation and the delivery checklist

Handover is ordinary deliverable work plus a final integrity pass:

```sh
aperio integrity .                       # read-only consistency report
aperio export . --dry-run                # preview exactly what would travel
aperio export . --output aperio-state.zip
```

The archive is the project's *memory* (state, brief, decisions, handoff,
plan, run records in portable projection), not the website itself.
`.aperio/local/` never travels, and manifest hashes detect corruption —
they do not authenticate the archive or prove it secret-free.

| Handover item | Rule |
| --- | --- |
| Documentation | Content and maintenance guides are deliverables; check them with `quality-check` |
| Ownership | Domain, hosting, and code ownership live in the client's contracting process, recorded in your own notes |
| CMS access | Credentials go through your secure channel — **never passwords or keys in shared deliverables** |
| Maintenance obligations | Stated in goal constraints/requirements; remaining work becomes explicit plan tasks or is explicitly out of scope |
| Delivery acceptance | Client acceptance recorded as evidence on the closing task; the Stage 12 rules apply |

Delivery acceptance closes the walkthrough the way every stage opened
it: with recorded, labeled evidence — and with nothing claimed that was
not recorded.

## Where to go next

| Want | Read |
| --- | --- |
| The full goal format | [goal-contract.md](goal-contract.md) |
| Quality rules and their limits | [quality.md](quality.md) |
| Plans, tasks, approval, review gates | [architecture.md](architecture.md) |
| Brain dumps and mid-task capture | [brain-dump.md](brain-dump.md) |
| Memory, trust, and what `verified` means | [universal-memory.md](universal-memory.md) |
| What Aperio does not promise | [security-model.md](security-model.md) |
| Every command | [cli-reference.md](cli-reference.md) |

A companion 30-day SEO walkthrough (planned in this program,
master-plan-v2 Chunk 06) covers search-measurement honesty separately.
