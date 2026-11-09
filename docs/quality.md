# Universal mistake prevention and quality evaluation (M4)

Aperio's quality layer answers two questions deterministically, before and
after an agent works:

1. **`aperio preflight`** — what could go wrong with this assignment *before*
   work begins?
2. **`aperio quality-check`** — does a candidate deliverable satisfy what the
   goal *explicitly, objectively* requires?

Both commands are read-only, standalone-first (no project, provider, or
model API), and honest about their limits: a finding is labelled
**confirmed** (a deterministic rule proved it), **warning** (a strong
signal needing judgment), **unknown** (the tool cannot decide and says so),
or **recommendation** (subjective advice). The tool never judges truth,
taste, or completeness that is not objectively checkable, never executes
untrusted content, never writes to a memory store, never promotes memory,
and never alters the goal or the deliverable. It is not a governance gate:
nothing in the Feature 03/06 lifecycle reads a quality report.

## Reports (quality-report v1)

Every run produces one canonical payload; Markdown and JSON render from it.

```json
{
  "format": "aperio-quality-report",
  "format_version": 1,
  "taxonomy_version": 1,
  "rules_version": 1,
  "generated_at": "2026-11-09T12:00:00Z",
  "aperio_version": "0.14.0",
  "content_sha256": "…",
  "content": {
    "kind": "preflight | deliverable",
    "goal": { "goal_identity", "goal_sha256", "profile", "authority",
               "provenance_source" },
    "deliverable": { "source", "utf8_bytes", "words", "sha256",
                      "sections_found" },
    "request": { },
    "memory": { },
    "issues": [ … ],
    "counts": { "total", "by_status", "by_severity", "by_category" },
    "checks_run": ["P-…", "Q-…"],
    "limitations": [ … ]
  }
}
```

`content_sha256` covers `content` only; `generated_at` and the package
version are envelope metadata, so identical inputs give identical issue ids
and the same content hash. Issue ids (`QI-<12 hex>`) are derived from rule,
requirement reference, and evidence — the same finding has the same id in
two runs.

### Issues

| Field | Meaning |
| --- | --- |
| `issue_id` | stable, content-derived (`QI-…`) |
| `rule_id` / `rule_version` | which declarative rule fired (quality-rules v1) |
| `category` | taxonomy v1 (below) |
| `severity` | `high`, `medium`, `low`, `info` |
| `status` | `confirmed`, `warning`, `unknown`, `recommendation` |
| `requirement_ref` | where the requirement came from (`goal.constraints[2]`, `request.required_sections[0]`, …) |
| `evidence` | the matched or missing text (bounded) |
| `explanation` | why this is a finding |
| `corrective_action` | what to do next |

### Taxonomy (v1)

`requirement-violation`, `missing-deliverable`, `unsupported-claim`,
`conflicting-information`, `context-misuse`, `outdated-information`,
`provenance-failure`, `output-format-failure`, `insufficient-evidence`,
`incomplete-task`, `privacy-or-safety-violation`. Names are stable; a later
taxonomy version may add categories, never rename them.

### Rules (quality-rules v1)

Rules are plain data in `aperio/quality.py` (`RULES`) — one deterministic
engine, no plugin loading, no executable rule definitions, no third-party
dependency. Most rules apply to every profile; `Q-missing-verification-
evidence` is scoped to the `software` profile. `aperio` never invents a
rule at runtime.

## Preflight

```sh
aperio preflight goal.json
aperio preflight goal.json --memory-store ~/notes/brand.mem --json
aperio preflight goal.json --output preflight.md
```

Checks (each reported only when it has something to say):

| Rule | Category | Status |
| --- | --- | --- |
| `P-untrusted-instructions` (goal provenance is agent/import) | provenance-failure | confirmed |
| `P-objective-underspecified` (< 4 words) | incomplete-task | warning |
| `P-missing-acceptance-criteria` / `P-missing-deliverables` | insufficient-evidence / missing-deliverable | warning |
| `P-missing-quality-criteria` | insufficient-evidence | recommendation |
| `P-duplicate-instructions` (identical text in two instruction slots) | conflicting-information | warning |
| `P-conflicting-length-expectations` (two distinct stated bounds) | conflicting-information | warning |
| `P-unvalidated-assumptions` (one finding per assumption) | unsupported-claim | recommendation |
| `P-unaddressed-risks` (risks named, no acceptance criteria) | incomplete-task | recommendation |
| `P-oversized-instruction-list` (≥ 40 entries of 50) | context-misuse | warning |
| `P-expired-memory` (with `--memory-store`) | outdated-information | warning |
| `P-unreviewed-memory` (selected memories still `proposed`) | unsupported-claim | warning |
| `P-memory-conflict` | conflicting-information | warning |

Memory inspection reuses the M3 recall machinery read-only; the store file
is byte-identical after the run. Nothing is promoted, retracted, or written.

## Deliverable quality check

```sh
aperio quality-check goal.json --deliverable draft.md
aperio quality-check goal.json --deliverable draft.md \
  --require-section "Results" --forbid-text "INTERNAL-TOKEN" \
  --min-words 300 --json
aperio quality-check goal.json --deliverable draft.md --request request.json
```

The deliverable must be UTF-8 text, at most 1 MiB, supplied by the operator.
Checks:

| Rule | Category | Status |
| --- | --- | --- |
| `Q-empty-deliverable` | incomplete-task | confirmed |
| `Q-required-section` (operator-named heading/label absent) | output-format-failure | confirmed |
| `Q-required-text` (verbatim text absent) | requirement-violation | confirmed |
| `Q-forbidden-text` (forbidden text present) | privacy-or-safety-violation | confirmed |
| `Q-constraint-required-inclusion` ("must include/mention/contain/reference/state/name X") | requirement-violation | confirmed |
| `Q-constraint-prohibition` ("must not / must never … X"; sentences that negate the phrase themselves do not fire) | requirement-violation | confirmed |
| `Q-min-length` / `Q-max-length` (stated word bounds) | output-format-failure | confirmed |
| `Q-target-length` (approximate length missed by > 10%) | output-format-failure | warning |
| `Q-deliverable-coverage` (named deliverable never mentioned) | missing-deliverable | warning |
| `Q-missing-attribution` (evidence required, no citation found) | insufficient-evidence | warning |
| `Q-unsupported-absolute-claim` (absolute phrasing under an evidence requirement) | unsupported-claim | warning |
| `Q-source-support-unknown` (attribution found; sources not readable by the tool) | insufficient-evidence | **unknown** |
| `Q-format-mismatch` (JSON that does not parse: confirmed; markdown/table expectations: warning) | output-format-failure | confirmed/warning |
| `Q-missing-limitations` (risks/uncertainties named, no limitation language) | incomplete-task | warning |
| `Q-missing-verification-evidence` (software profile: acceptance criteria, no check language) | insufficient-evidence | recommendation |

Modal constraint extraction is deterministic and auditable: every finding
quotes the constraint it came from. Bounds expressed in pages, slides, or
characters are reported as expectations and never converted into word
counts.

### The quality request (request-schema v1)

`--request FILE` or the repeatable flags fill a small versioned contract:

```json
{
  "request_schema_version": 1,
  "required_sections": ["Results", "Limitations"],
  "required_texts": ["data collected between 2024 and 2025"],
  "forbidden_texts": ["INTERNAL-TOKEN"],
  "min_words": 300,
  "max_words": 1500,
  "use_output_contract": true
}
```

Unknown fields are rejected; lists are capped at 100 entries, texts at
8 KiB; `min_words <= max_words` is enforced. `use_output_contract: false`
ignores the goal's own length/format statements.

## Honesty limits

- Presence/absence of text is not understanding. `Q-deliverable-coverage`
  and `Q-missing-limitations` are signals (warnings), never proof.
- `Q-source-support-unknown` is deliberate: when sources are cited, whether
  they support the claims is **unknown** to the tool and must be judged by
  a human.
- The engine cannot detect sarcasm, omission by paraphrase, or a wrong
  number stated confidently. Nothing here replaces reading the work.
- A clean report means "nothing explicitly required was found missing" —
  it is not a certificate of correctness.
- Reports are derived artifacts: regenerate rather than edit them; a
  hand-edited report no longer matches its own hash.
- No memory writes, no goal/deliverable rewrites, no provider calls, and
  no governance authority: quality gates stay outside the Feature 03/06
  completion contract.
