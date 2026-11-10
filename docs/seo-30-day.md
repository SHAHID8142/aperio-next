# Optional workflow: 30-day SEO service (walkthrough)

Companion to [website-delivery.md](website-delivery.md). This page maps one
common engagement — a 30-day SEO service window, optionally following a
website delivery — onto Aperio's universal primitives: goals, plans, tasks,
decisions, quality reports, feedback, and portable memory. It is a
**template layer**, not a new engine: no Search Console integration, no
analytics API, no crawler, and no monitoring service is created or implied.

Standing rules, as in the website-delivery walkthrough:

- **It advises; it never overrides.** Operator requirements outrank this
  template, and nothing here bypasses plan approval or the independent
  review gates (Feature 06) when they are enabled.
- **The example is a placeholder.** Dates shown are illustrative (an
  engagement of 2026-11-16 through 2026-12-15); no client names, data,
  metrics, or outcomes appear anywhere, and every report shown is a
  **simulation sketch**, not captured output.
- **Honesty is part of the workflow.** Where Aperio cannot know something —
  whether a page ranks, whether traffic moved, whether the client consented
  — the walkthrough says so.

## Integrity rules for measured data

These are hard rules of the workflow, not preferences:

1. **Never guarantee rankings, traffic increases, or indexing.** Search
   engines decide crawling, indexing, and ranking on their own schedules.
2. **Never invent keyword rankings, impressions, clicks, conversions, or
   client results.** A number without a retrieved source does not appear.
3. **Label real data, estimates, assumptions, and unknowns separately**, with
   Aperio's honest-status vocabulary: **confirmed** (proved by a rule or tool
   output), **warning** (a signal — judge it), **unknown** (not measurable
   now), **recommendation** (advice, not a fact).
4. **Access before claims.** Search Console or analytics access must be
   confirmed *before any measured result is quoted*. Without access, the
   workflow records honest unknowns and falls back to the client-run manual
   check documented below.
5. **No API integration is claimed.** Aperio has no Search Console or
   analytics integration; the workflow is checklist-driven with operator-run
   external tools. You run the tool, save its output, cite it as evidence;
   Aperio never fetches third-party data or verifies it.
6. **No background monitoring exists.** Monitoring is a scheduled human step
   — a plan task and a calendar entry; nothing observes the site between
   sessions, and no Aperio command re-checks anything on its own.
7. **No fabricated thresholds.** Checklist topics follow current
   authoritative technical SEO guidance categories (crawlability,
   canonicalization, structured data, Core Web Vitals), but this document
   states no threshold numbers: thresholds live in the tools that measure
   them, and the tool's labeled result is the evidence.

## Pre-launch SEO readiness — starts during discovery, not after launch

Search work begins where the website walkthrough begins: in discovery and
architecture (Stages 1–5 of website-delivery.md):

| Readiness area | Goal field / plan task | Check where natural |
| --- | --- | --- |
| Search intent and page mapping | goal `requirements` + plan tasks | textual: `quality-check --require-section` |
| Information architecture and URL structure | goal `constraints` + plan tasks | human review; recorded in decisions |
| Crawlability review (robots, redirects, parameters) | plan task with preconditions | external crawl tool; output cited as evidence |
| Metadata planning (titles, descriptions per page) | goal `deliverables` | `quality-check --require-section "Metadata plan"` |
| Internal linking plan | plan task | human review; decisions |
| Canonical strategy (duplicates, parameters, staging) | goal `constraints` + `decision-add` | `decision-add` records the rule |
| Applicable structured data | plan task | validator run outside Aperio; output cited |
| Mobile usability and performance | goal `quality_criteria` | tool measurements cited with tool + date |
| Sitemap and indexing readiness | plan task | manual inspection recorded as evidence |

The engagement is an ordinary goal:

```json
{
  "goal_id": "SEO-30DAY-CLIENTSITE",
  "objective": "Deliver a 30-day SEO service window for the client's production website: audit, approved corrections, final report",
  "intended_outcome": "Approved corrections implemented with labeled evidence; a final report separating measured data, estimates, assumptions, and unknowns",
  "profile": "business",
  "constraints": [
    "Engagement window: the agreed 30 days (2026-11-16 through 2026-12-15 in this illustration)",
    "No ranking, traffic, or indexing guarantees are stated in any deliverable",
    "Measured search data requires confirmed Search Console or analytics access; without access, affected items are recorded as unknown",
    "Client-approved changes only: no correction ships without a recorded approval"
  ],
  "deliverables": ["Client-facing SEO audit summary (week 1)", "Implemented corrections with before/after evidence", "Final 30-day report", "Next-month recommendation list"],
  "quality_criteria": ["Every measured number cites its tool and retrieval date", "Every finding carries a status label: confirmed, warning, unknown, or recommendation"],
  "acceptance_criteria": ["Final report separates measured data, estimates, assumptions, and unknowns", "No ranking, traffic, or indexing claim without confirmed access", "Every shipped correction records what changed and how it was checked"],
  "evidence_requirements": ["Name the external tool and retrieval date for every cited measurement"],
  "uncertainties": ["Search Console or analytics access may not be granted during the window", "Search engines re-crawl on their own schedule; indexing results are outside anyone's control"]
}
```

```sh
aperio goal-check seo-goal.json      # gaps reported as gaps, never filled
aperio preflight seo-goal.json       # unvalidated assumptions, risks, conflicts
```

**Where the 30-day window lives.** Goal-schema v1 has no date fields and
rejects unknown fields, so the window is carried in constraint text as
above. The mechanism that actually expires data over time is the memory
store's validity windows (`--valid-from`/`--valid-until`), used below.

## Days 1–7 — technical baseline, access, audit summary

```sh
aperio init .
aperio plan-create . --file seo-plan.json      # draft until approved
aperio plan-approve . --revision 1 --by owner  # after genuine human review
```

The plan (excerpt; every task also carries deliverables and acceptance
criteria). `priority` is the plan-schema v2 optional scheduling hint —
ready tasks are ordered by (priority rank, task id) and every ready task
stays visible; v1 documents schedule everything as `normal`:

```json
{
  "objective": "30-day SEO service window: audit, approved corrections, final report",
  "expected_outcome": "Corrections shipped with labeled evidence; report separates measured, estimated, and unknown",
  "acceptance_criteria": ["Final report passes the final-report quality check"],
  "tasks": [
    {"id": "TASK-1", "title": "W1: Search Console/analytics access setup or verification", "objective": "Confirm real access before anything is measured", "deliverables": ["Access status decision entry"], "acceptance_criteria": ["Access recorded as confirmed, or as unknown with the fallback check assigned"], "priority": "critical"},
    {"id": "TASK-2", "title": "W1: Technical SEO baseline audit", "objective": "Crawlability, canonicalization, sitemap/indexing inspection, structured data, Core Web Vitals observations, analytics prerequisites", "deliverables": ["Findings list with status labels"], "acceptance_criteria": ["Every finding labeled confirmed/warning/unknown/recommendation"], "depends_on": ["TASK-1"], "priority": "high"},
    {"id": "TASK-3", "title": "W1: Prioritized issue list and client-facing audit summary", "objective": "Turn week-1 findings into an approved, prioritized fix set", "deliverables": ["Audit summary document"], "acceptance_criteria": ["Sections match the audit request below"], "depends_on": ["TASK-2"], "priority": "high"}
  ]
}
```

Week-2 tasks (approved technical corrections; metadata and internal
linking) depend on TASK-3; week-3 (content and intent review, schema
validation) also depends on TASK-3; week-4 (monitoring and follow-up, then
the final report) depends on the week-2 and week-3 tasks.

Access is set up or verified by a human in the browser; Aperio records the
outcome — it cannot verify the property itself:

```sh
aperio decision-add . --title "Access status" \
  --context "Search Console and analytics access for the engagement" \
  --decision "Search Console access confirmed by client on <date>; analytics viewer access confirmed on <date> (dates recorded verbatim from client confirmation)" \
  --consequences "Measured results quoted from confirmed tools only; anything else stays unknown"
```

The client-facing audit summary is checked textually before it is sent:

```json
{
  "request_schema_version": 1,
  "required_sections": ["Access status", "Confirmed findings", "Warnings", "Unknowns and unmeasured items", "Recommendations"],
  "forbidden_texts": ["TODO", "TBD", "PLACEHOLDER-METRIC"],
  "min_words": 200
}
```

```sh
aperio quality-check seo-goal.json --deliverable audit-summary.md \
  --request audit-request.json
aperio checkpoint-write . --reason "days 1-7: audit summary delivered"
```

`--forbid-text` is a literal presence check and cannot judge context: a
disclaimer containing the forbidden phrase is flagged too, so choose tokens
you want absent verbatim. A clean report means "nothing explicitly required
was found missing" — not that the audit is correct.

## Days 8–14 — approved corrections and evidence

Corrections ship only with client approval, recorded first as a decision
entry (`aperio decision-add`, as in week 1 — approval before work, deferred
items recorded, not silently dropped). Approved items become plan tasks and
move through the evidence-required lifecycle:

```sh
aperio task-status . TASK-4 --status in-progress --owner operator
# ... implement with your own tooling; Aperio records, not renders ...
aperio task-status . TASK-4 --status completed --result passed \
  --evidence "before/after notes: canonical tag added on <url>; verified in rendered page source by operator on <date>" \
  --limitations "operator-side verification only; no search-engine behavior implied"
```

Metadata improvements and internal linking follow the same pattern: a task,
recorded evidence, stated limitations. Performance and accessibility work
is included where relevant, with the tool's own labeled output as evidence
— never a number typed from memory.

Client messages mid-engagement ("we should also rank for X") are captured
with the Brain Dump Guard ([brain-dump.md](brain-dump.md)), never obeyed
mid-task; new scope becomes work only through `plan-revise` with renewed
approval — never silently.

**Before/after evidence.** Text-level quality reports can be compared:

```sh
aperio feedback-compare --before report-before.json --after report-after.json
```

This compares quality reports — not site data: a before/after of search
metrics exists only when access was confirmed in week 1 and both exports
carry their retrieval dates.

## Days 15–21 — content, intent, and schema review

Content and search-intent review asks whether each page serves the intent
it claims to serve — human judgment, recorded on tasks. Approved on-page
improvements follow the week-2 approval rule. Structured data is validated
by a validator run outside Aperio; its own output (valid, or the specific
errors) is the recorded evidence — Aperio adds no schema opinion of its own.

Research and citation quality get textual checks:

```sh
aperio quality-check seo-goal.json --deliverable content-review.md \
  --require-section "Content gaps" --require-section "Citations"
```

When the goal requires evidence, `quality-check` reports
`Q-missing-attribution` (citation language absent — warning) and, when
sources are cited but not readable by the tool, `Q-source-support-unknown`:
whether a source actually supports a claim is **unknown** to the tool and
judged by a human. Content gaps become recorded recommendations or approved
tasks — not silent scope.

Engagement facts enter memory as records with validity windows (dates are
placeholders; every engagement sets real ones). Records start `proposed`;
a recorded review promotes them, and engagement-scoped records expire with
their windows instead of quietly surviving the engagement:

```sh
aperio mem-propose --store ~/notes/seo-client.mem --type requirement \
  --title "Access status" --scope objective --scope-value "30-day SEO engagement" \
  --content "Search Console access confirmed on <date>; analytics viewer access confirmed on <date>. Measured results may be quoted from these tools only." \
  --tag access --tag search-console --evidence "client confirmation recorded as DEC-0001" \
  --valid-from 2026-11-16 --valid-until 2027-11-16

aperio mem-propose --store ~/notes/seo-client.mem --type limitation \
  --title "No measured baseline without access" --scope objective --scope-value "30-day SEO engagement" \
  --content "If analytics access is never granted, all traffic and ranking claims stay unknown; only crawl-side and on-page observations are recorded." \
  --tag honesty --tag measurement --valid-from 2026-11-16 --valid-until 2026-12-15

aperio mem-review ~/notes/seo-client.mem MEM-XXXXXXXXXXXX --accept
```

## Days 22–30 — monitoring, follow-up, final report

Monitoring is a scheduled human step: a plan task and a calendar entry —
no daemon, no scheduler, no watcher between sessions. The operator opens
Search Console or the analytics tool at the agreed checkpoints, saves the
export, and cites it with its retrieval date. Issue follow-up re-opens or
re-prioritizes through the plan gates, never around them.

```sh
aperio task-status . TASK-7 --status completed --result passed \
  --evidence "Search Console export retrieved <date>; open items 6-7 follow-up notes" \
  --limitations "short observation windows; no trend claims across weeks without both endpoint exports"
```

The final report carries the provenance rules:

| Statement kind | Allowed label | Evidence required |
| --- | --- | --- |
| Measured result (impressions, clicks, positions) | confirmed | tool export retrieved during the window, tool and date recorded |
| On-page change or external tool observation | confirmed; tools per their own label | deploy record, diff, or page source checked on a recorded date; saved tool output with name and version |
| Projection of future traffic or rankings | — | never stated as fact; at most a **recommendation** |
| Client-run manual check result | attested | the client's own reported observation, recorded as theirs |
| Anything without a source | unknown, or omitted | none — unknowns are listed, not estimated |

```json
{
  "request_schema_version": 1,
  "required_sections": ["Access and data status", "Actions completed", "Confirmed findings", "Warnings", "Unknowns and unmeasured items", "Recommendations for the next month"],
  "forbidden_texts": ["TODO", "TBD", "PLACEHOLDER-METRIC"],
  "min_words": 400
}
```

```sh
aperio quality-check seo-goal.json --deliverable final-report.md \
  --request final-request.json
aperio integrity .
aperio export . --output seo-engagement.zip
```

Absolute phrasing under the evidence requirement is reported as an
unsupported claim (`Q-unsupported-absolute-claim`) — a signal for the human
to judge, not a certificate. Quality reports are derived artifacts:
regenerate them, never edit them. The archive is the engagement's *memory*
(state, decisions, plan, run records in portable projection), not the
website; `.aperio/local/` never travels. Next-month recommendations carry
no prediction of results; they seed a new goal only if the client approves
a new engagement.

## When access is never granted — the client-run manual check

Without confirmed access, the workflow records honest unknowns and assigns
this documented manual check, which **the client runs** (~10 minutes):

```text
Client-run check (manual; report the results to the operator):
1. In a search engine, search site:example-domain.example and note which of
   the site's key pages appear. Record the date and the exact query used.
2. Open https://example-domain.example/robots.txt in a browser and confirm
   it does not disallow the pages that should be crawlable.
3. Open https://example-domain.example/sitemap.xml and confirm the key
   pages are listed.
```

The client's reported results are recorded as **attested** observations —
what one person saw on one day, not verified measurement — and every
traffic- or ranking-shaped question stays **unknown** until real access
exists. In every case the workflow's closing rule holds: nothing is claimed
that was not recorded, and nothing recorded is claimed as more than it is.

## Where to go next

| Want | Read |
| --- | --- |
| The sibling walkthrough (website delivery) | [website-delivery.md](website-delivery.md) |
| The full goal format | [goal-contract.md](goal-contract.md) |
| Quality rules and their limits | [quality.md](quality.md) |
| Memory, trust, validity windows | [universal-memory.md](universal-memory.md) |
| Brain dumps and mid-task capture | [brain-dump.md](brain-dump.md) |
| Every command | [cli-reference.md](cli-reference.md) |
