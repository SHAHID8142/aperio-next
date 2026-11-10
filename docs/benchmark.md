# Cross-domain benchmark and evaluation (M8, benchmark-report v1)

A reproducible, offline evaluation framework that measures what Aperio's
tooling **deterministically does** on versioned fixture cases — and that
states, in the report itself, what it does *not* measure.

```sh
aperio benchmark-cases                 # list the versioned cases
aperio benchmark-run                   # run all; Markdown report
aperio benchmark-run --json --output report.json
aperio benchmark-run --only research-unsupported-claims
aperio benchmark-run --cases ./my-cases   # your own benchmark-case v1 set
```

No model API, provider, network, or project is needed. Cases ship with the
package (`src/aperio/benchmark_cases/*.json`) and are validated against
benchmark-case v1 at load.

## What is measured

| Metric | How | Where |
| --- | --- | --- |
| Requirement coverage / constraint satisfaction / format compliance / detected-mistake rate | the M4 engine against fixtures with **planted** flaws; a case fails if a planted rule stops firing or an unexpected confirmed finding appears | per case |
| Unsupported-claim handling | fixtures with absolute claims under evidence requirements; attribution missing → warning; sources cited → **unknown** status (never scored as verified) | research cases |
| Context relevance / memory correctness | fixture stores with expired, unreviewed, conflicting, and private records; the report records exclusions and that private memory is never selected | memory cases |
| Trust-boundary violations | attempted promotions (forged `verified`, agent-goal-as-brief, untrusted goal instructions in a pack) must all be **blocked**; one unblocked attempt fails the run | trust case |
| Cross-agent information preservation | exchange round trip: goal fields, notes, and memories must arrive verbatim, provenance intact, everything still `proposed` | exchange case |
| Missing-information handling | declared uncertainties must be reported as missing and nothing invented | preflight checks |
| Feedback-based improvement / regression rate | before/after/worse deliverables compared with `feedback-compare`; the worse one must be reported as a regression | feedback case |
| Resource and execution overhead | wall-clock seconds per stage, reported as measured durations, **excluded from the content hash** | per case |

## What is deliberately NOT measured

- **Whether AI agents perform better with Aperio.** The report's scope
  statement says so verbatim. The baseline workflow ("submit the
  deliverable with no checks") detects zero issues *by definition*; the
  comparison shows what the tooling adds on the fixture set.
- **Output quality, factual accuracy, design taste.** Every case declares
  `requires_human` — the human-evaluator metrics are listed in the report
  and never scored.
- **No aggregate quality score.** Rates are counts of planted findings
  detected (a tool property), not judgments of output quality.

## Case set (v1)

Fourteen versioned cases across all six domains — design, research,
writing, business, general knowledge work, and software (one among six,
not the dominant one) — covering: ambiguous instructions, missing
information, conflicting requirements, large irrelevant context, outdated
memory, unsupported claims, cross-agent context transfer, feedback-based
improvement, incomplete deliverables, and correctly handled uncertainty.
Each case carries `expected_rules` (must fire), `forbidden_rules` (must
not fire — false-positive guards, including a passing-deliverable case),
`trust_attempts` (must be blocked), `exchange_expectations` (must arrive
verbatim), and `requires_human` (never scored).

## Writing your own cases

A case is one JSON file (benchmark-case v1) in a directory passed via
`--cases`:

```json
{
  "case_schema_version": 1,
  "case_id": "custom-writing-case",
  "domain": "writing",
  "scenario": "minimum length violated",
  "checks": ["quality"],
  "goal": { "goal-schema v1 document" },
  "deliverable_text": "# Sleep debt\n\nShort.",
  "planted_issues": [{"category": "output-format-failure",
                      "description": "below minimum"}],
  "expected_rules": ["Q-min-length"],
  "forbidden_rules": [],
  "requires_human": ["whether the prose reads well"]
}
```

`checks` draws from `preflight`, `quality`, `memory`, `exchange`,
`feedback`. Unknown fields, wrong domains, oversized payloads, and bad
schemas are refused.

## Regressions

A case whose planted issue stops being detected, whose false-positive
guard fires, whose trust attempt is not blocked, whose transferred
information is lost, or whose regression goes unreported is a **regression**
and appears in `content.regressions` — the report never hides one, and the
Markdown renders them in their own section.

## Honest limits

- Detection reflects fixture wording: paraphrase and semantic-only flaws
  are out of scope by design.
- Overhead numbers are machine-dependent.
- Provider-assisted benchmarks are possible (a provider CLI dispatches
  through the Feature 07 adapters) but never required, never run by this
  framework automatically, and were not used to produce any claim here.
- The framework does not certify Aperio: it is a regression harness for
  the tool's own documented detection behavior, and the report says so.
