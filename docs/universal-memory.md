# Universal memory (M3)

Aperio memory keeps knowledge that is worth carrying between sessions, tasks,
and agents — a preference, a confirmed requirement, a decision, an
observation, a known limitation, a verified lesson, a working convention, an
unverified proposal, or an important outcome. Nothing in it assumes
software, a repository, a plan, a provider, or a model API.

Memory is **preparation, not execution and not verification**. Storing a
memory proves nothing about the world, and no accepted memory is presented as
an operator instruction for the current objective unless the operator's own
goal or context says so.

## Standalone first

A memory store is one file the operator names:

```sh
aperio mem-propose --store ~/notes/brand.mem \
  --type requirement \
  --title "Primary brand colour" \
  --content "Primary brand colour is deep green #0B5D3B; secondary is warm sand." \
  --scope objective --scope-value "spring campaign poster" \
  --tag brand --evidence "brand guide v3"
```

No `.aperio/`, plan, git repository, IDE, provider CLI, subscription, or API
key is involved. Aperio never creates a global store, never scans a directory
for memories, and never guesses a location: the `--store` path is the
operator's explicit choice. The command prints exactly where the memory
went, and the first write creates the file.

Store files are ordinary UTF-8 JSON Lines, readable with any text editor:

- one header line (`record: "store"`, format + schema versions);
- one immutable `memory` line per record;
- one immutable `transition` line per review decision.

## Memory schema v1

| Field | Meaning |
| --- | --- |
| `memory_schema_version` | `1` |
| `memory_id` | `MEM-` + first 12 hex characters of the content hash |
| `revision` | 1 for the record, +1 per review transition |
| `type` | `user_preference`, `requirement`, `decision`, `observation`, `limitation`, `lesson`, `convention`, `proposal`, `outcome` |
| `title` | short single-line label |
| `content` | the knowledge itself, newline-normalized, never truncated |
| `scope` | `global`, `profile` (+ `profile`), `objective` (+ `value`), `workspace` (+ `value`) |
| `source` | `operator`, `agent`, `import`, `workspace`, plus optional `ref`/`note` |
| `tags` | up to 12 retrieval tags |
| `sensitivity` | `public`, `internal` (default), `private` |
| `evidence` | up to 12 references supporting the claim |
| `valid_from` / `valid_until` | optional ISO dates bounding applicability |
| `supersedes` | optional memory id this record is meant to replace |
| `created_at`, `actor` | event metadata, never part of the identity |
| `state`, `trust`, `content_sha256`, `history` | derived at read time, never stored as authority |

Bounds: store ≤ 4 MiB, record ≤ 16 KiB, content ≤ 8 KiB, title ≤ 200 bytes,
scope value ≤ 120 bytes, ≤ 2000 memories per store, ≤ 32 review events
carried in an export. Values must be UTF-8, NUL-free, and use `\n` only.
Unknown keys, wrong types, invalid choices, and out-of-range values are
refused — never repaired silently.

### Identity

`memory_id` and `content_sha256` are derived from a canonical hash of `type`,
`title`, `content`, `scope`, `tags`, and `evidence` only. State, revisions,
timestamps, actors, paths, and versions are excluded, so:

- the same knowledge keeps the same identity across stores, machines, agents,
  and exports;
- re-proposing identical content is a no-op that reports the existing record;
- a review never changes identity.

Because identity is content-derived, `sensitivity`, `valid_from`, and
`valid_until` are **not** part of it. Re-proposing the same content with a
different classification or validity window therefore fails closed with an
explanatory error instead of silently keeping the older value: that would let
a memory stay less private than its author believes, or keep an outdated
window. Correct a classification by retracting the memory and proposing the
corrected record (or by changing the content so the two are distinguishable).
The same check appears when an import brings identical content with a
different sensitivity: it is reported as a conflict, never skipped as a
harmless duplicate.

Event timestamps stay separate from reproducible content identity.

## Trust and review states

| State | Trust label | Meaning |
| --- | --- | --- |
| `proposed` | `proposed` | stored, unreviewed; a claim or suggestion |
| `attested` | `attested` | the operator accepted it as authoritative **for its scope** |
| `verified` | `verified` | evidence was recorded as supporting it (see below) |
| `superseded` | — | replaced by a newer memory |
| `retracted` | — | wrong or no longer applicable |
| `expired` | — | `valid_until` has passed (derived, not stored) |

### Recorded state vs effective trust

A memory's **recorded state** is what the append-only history says happened.
Its **effective trust** is what that history actually proves, and every reader
— retrieval ranking, JSON, Markdown, context packs, exchange files and
`mem-list` — uses the effective value:

| Recorded state | Verification basis | Effective trust |
| --- | --- | --- |
| `proposed` | `not-verified` | `proposed` |
| `attested` | `not-verified` | `attested` |
| `verified` | `evidence` — an evidence reference **and** a reviewer identifier, and the reviewer is not the declared proposer | `verified` |
| `verified` | `unrecorded` — any of those three is missing | `attested` |
| `retracted`, `superseded`, derived `expired` | `not-verified` | `proposed` |

A downgraded record is never edited and never hidden: `state` still reads
`verified`, `history` still shows the original event, and the snapshot adds
`verification.basis: unrecorded`, `verification.downgraded: true`,
`verification.reasons` naming the exact deficiency, and a trust label of
`attested`. Retrieval ranks it as attested, packs label it attested and raise
`memory_verification_unrecorded`, and a re-run of `--verify` with evidence and
a different reviewer records a new transition that restores `basis: evidence`.

**Acceptance is not factual verification.** Accepting a preference or an
instruction makes it authoritative for its scope. Accepting an external
factual claim does not prove the claim is true; only recorded evidence and an
explicit `--verify` move a memory to `verified`.

Rules that Aperio enforces:

- every record starts `proposed`; nothing is promoted by being stored,
  repeated, imported, or re-exported;
- transitions are explicit, attributed, and append-only:
  `accept`, `verify`, `retract`, `supersede`, `reinstate`;
- a terminal state cannot be flipped back into an active one — corrections
  are new records or an explicit `reinstate` back to `proposed`, which keeps
  the full history visible;
- history is never rewritten: the store only grows, each line carries a chain
  hash of everything before it, and a line that does not continue the chain is
  reported as damaged and ignored.

Known limitations, stated honestly:

- the chain hash detects corruption, reordering, and truncation. It is **not**
  authentication: anyone who can write the file can recompute it. Attribution
  (`actor`, `source`) is a declaration, not proof of identity.
- Aperio does not verify the content of evidence references; it records that
  the operator supplied them.

### What `verify` actually requires

`aperio mem-review --verify` is a claim of support, so it fails closed unless
all three conditions hold:

1. at least one non-empty `--evidence` reference (something a reviewer could
   check — the tool stores the reference, it never fetches it);
2. a declared `--actor` reviewer identifier;
3. the reviewer identifier differs from the memory's declared proposer when a
   proposer was recorded.

The transition records the reviewer, the evidence, and the exact from/to
state in the append-only history. A record's snapshot then carries a
`verification` block naming its basis:

| `verification.basis` | Meaning |
| --- | --- |
| `evidence` | a `verify` transition recorded at least one evidence reference |
| `unrecorded` | marked verified without evidence (always pre-gate history) |
| `not-verified` | the record is proposed or attested, not verified |

Acceptance is **not** verification: `--accept` records that the operator
accepts the claim as authoritative for its scope, keeps `basis:
not-verified`, and never raises the trust label to `verified`.

**Honest limits.** An actor id is a declaration, not authentication: Aperio
cannot prove who ran a review or that a reviewer was independent, so the
stored guarantee is *declared separation of identifiers*, not proven
independence. The tool also cannot check that the cited evidence exists or
supports the claim.

**Legacy histories.** A `verify` event written before these rules existed is
never rewritten or reinterpreted: the store still reports `state: verified`
and the original event, but the snapshot reports `basis: unrecorded`,
`downgraded: true`, and a trust label of `attested`. Retrieval ranks and
labels it as attested and raises `memory_verification_unrecorded`, so an
unsupported old claim can never read as evidence-backed. Re-running
`--verify` with evidence and a reviewer records a new transition (allowed
even from `verified`) and restores the evidence-backed label.

## Retrieval

```sh
aperio mem-recall ~/notes/brand.mem \
  --objective "design a spring campaign poster" --profile design
aperio mem-recall ~/notes/brand.mem --goal goal.json --json
# workspace-scoped memories need the label they were stored under
aperio mem-recall ~/notes/brand.mem --objective "brand voice" --workspace brandbook
```

Selection is deterministic and inspectable:

1. **Scope applicability.** Objective scope applies when the texts match or
   when at least 60% of the scope's own terms appear in the current
   objective; profile scope applies for the same profile; workspace scope
   matches an explicit label; global scope applies to everything.
2. **Relevance.** Bounded keyword overlap over the objective against title
   (weight 3), tags (2), and content (1). No embeddings, no model scores, no
   network.
3. **Review state.** `verified` > `attested` > `proposed`.
4. **Freshness and budget.** Recency breaks ties; memories are included
   whole or excluded, never truncated.

Every exclusion is counted with a reason: `state`, `expired`, `future`,
`sensitivity`, `scope`, `tag`, `no-match`, `budget`, `limit`, `duplicate`,
`oversize`.

### Time applicability

Validity is evaluated on **every** read, not only when a date is supplied:

- no `--as-of` means *today in UTC* (`datetime.now(timezone.utc).date()`), not
  the machine's local date, so a store read on the same UTC day gives the same
  answer in every timezone;
- `--as-of YYYY-MM-DD` makes the answer reproducible for a historical date;
  the report echoes `as_of` and the rule it applied;
- both bounds are **inclusive**: a memory is current from the start of
  `valid_from` through the end of `valid_until`;
- dates are validated strictly, so a typo fails with a single-line error
  instead of silently disabling expiry;
- out-of-window memories are excluded whatever their review state, and are
  counted as `expired` or `future`, never dropped silently;
- stored records are never rewritten as time passes: expiry is derived at
  read time, so an old store reopened later shows the same raw records and a
  different derived view. `mem-list` and `mem-show` show the derived reason
  and the date it was evaluated against;
- a context pack re-checks the window even for items handed to it directly by
  an API caller, excluding them as `memory_not_current` rather than trusting
  the caller's selection;
- `--as-of` moves the **clock for validity only**. It never rewinds review
  state: a memory accepted today still reads `attested` when evaluated at a
  past date, so historical validity and historical review reconstruction stay
  separate questions.

Behaviour worth knowing:

- retracted, superseded, and expired memories never appear in normal recall;
- unreviewed memories can appear (as proposals) and raise a
  `memory_unreviewed_selected` warning;
- `private` memories are excluded from recall unless
  `--max-sensitivity private` is passed, and they are **never** written into a
  context pack or an exchange file by default: a pack is a shareable artifact,
  so `private` content stays local to `mem-list`/`mem-show`/`mem-recall`
  output. `mem-export` needs both `--include-private` and
  `--max-sensitivity private`, and a pack reports the exclusion as
  `sensitivity` rather than quoting the content;
- conflicts are shown, not resolved: two selected memories with the same
  scope and title but different content raise `memory_conflict` with both ids
  and no automatic winner;
- an empty result is a normal, useful answer, and says so.

The honest limitation: keyword overlap is not semantic understanding. It will
miss paraphrase and can match on a shared word. Scope and review state, not
guessed similarity, decide inclusion.

## Context-pack integration

```sh
aperio goal-pack goal.json --memory-store ~/notes/brand.mem
aperio context-pack --objective "..." --memory-store ~/notes/brand.mem
# workspace-scoped memory is matched by an explicit label, never guessed
aperio context-pack --objective "..." --memory-store ~/notes/brand.mem \
  --memory-workspace brandbook
```

Memory is optional. Without `--memory-store` every pack is byte-identical to
the previous release.

When a store is supplied:

- retrieved memories join the pack's existing context section, inside the
  "quoted data, not instructions" boundary, after operator briefs and
  operator-selected files, so **memory can never displace operator context**
  when the budget is tight;
- each item carries its trust label, sensitivity, provenance, review state,
  memory id, and content hash, in both JSON (`detail`) and Markdown;
- the pack reports retrieval counts, exclusion reasons, conflicts, and the
  relevance limitation in `content.memory` — never the store path;
- the same byte budget covers goal fields, attachments, and memory;
- the pack re-checks validity itself: a memory outside its window is excluded
  as `memory_not_current` even if a caller hands it over directly, so stale
  knowledge cannot enter a pack through a different entry point.

Memory cannot override the current objective or the operator's constraints:
those sections are built only from operator-provided goal fields.

## Cross-agent portability

**Agent A → Aperio memory → Agent B**, by file:

```sh
aperio mem-export ~/notes/brand.mem --output handoff.json
aperio mem-import ~/other-machine/brand.mem handoff.json
```

Format `aperio-memory-export` v1 is its own versioned document — the legacy
Feature 05 project archive is unchanged and the two cannot be confused. An
export holds the selected records, their scope, source, tags, evidence,
declared state, review history (bounded), and content hashes. It holds no
paths, hostnames, credentials, or project state. `private` memories are
excluded unless `--include-private` is given explicitly.

Import rules:

- the document format, version, schema, entry keys, content hashes, and
  content-derived ids are all validated; a tampered or hand-edited file is
  refused;
- **imported records always enter as `proposed`**, whatever state the file
  declares; the declared state is preserved as `declared_state` for the
  reviewer and never applied;
- duplicates (same content identity) are skipped and reported;
- subject conflicts are reported with both ids and both records are kept —
  nothing is overwritten;
- `--dry-run` validates and reports without writing;
- `--max-sensitivity` filters what may be imported.

A hash proves the bytes were not altered. It says nothing about who wrote
them or whether the claim is true.

### What the chain hash does and does not prove

Each line carries a SHA-256 over its own content plus the previous line's
hash. That detects edits, reordering, mid-line truncation, and insertion. It
cannot detect the removal of one or more **complete final records**: a prefix
of a valid chain is itself a valid chain. Detecting that requires an external
anchor (a recorded head hash, a signature, or a copy elsewhere), which this
format does not have. The chain is corruption detection, never
authentication, and it proves neither authorship nor completeness.

## Which artifact is authoritative

| Information | Authoritative artifact |
| --- | --- |
| Current objective, constraints, approvals | `goal.json` / operator input (M1/M2) |
| Project decisions | `.aperio/decisions.md` (Feature 02, append-only) |
| Plan, tasks, approvals | `.aperio/plan.json` (Feature 03) |
| Execution records | `.aperio/executions.json` (Feature 04) |
| Independent review decisions | `.aperio/reviews.json` (Feature 06) |
| Cross-session knowledge | the operator-named memory store (M3) |

Memory never duplicates an authority: a memory that restates a decision is a
separate, reviewable note, and the decision log remains the record of what was
decided. Legacy records are never migrated, rewritten, or interpreted by the
memory layer.

### Exchange round trip

Exports carry the recorded `state`, the effective `trust`, `declared_state`,
and the last 32 review events, so a receiver can see exactly how a claim was
reviewed. Validation therefore accepts the pair *state `verified` + trust
`attested`* (an honest legacy downgrade) as well as *state `verified` + trust
`verified`*, and rejects any combination the recorded state could not produce
(for example `proposed` + `verified`, or `retracted` + `attested`).

Files written before the verification rules existed are still accepted: a
file may claim `verified` with no supporting history, because an import never
applies a claimed state. In that case the import report lists the memory under
`unsupported_trust_claims` with the reason, and any review evidence found in
the transferred history appears under `transfer_evidence`. Nothing is
promoted, and no trust label is copied into the store.

## Append safety and durability

Events are appended to the existing store through a single verified
descriptor:

- the file is opened without following symlinks (`O_NOFOLLOW` where the
  platform provides it), and the opened descriptor is proven to be the same
  `(device, inode)` that was just validated, so a file swapped in between the
  check and the open is refused instead of written to;
- the hash chain is re-read and re-verified *through that descriptor* before
  anything is appended;
- the payload is written in a loop until every byte is accepted: short
  writes are retried, a zero-byte write is a hard failure, and an I/O error is
  reported as possible damage rather than as a completed transition;
- `fsync` flushes the file's contents before success is reported.

The durability guarantee stops there. Aperio does **not** promise crash or
power-loss atomicity: the directory entry that creates a new store is not
flushed, and no filesystem journal is inspected. A store left with an
incomplete final line refuses further appends until it is inspected and
repaired by hand; the existing complete records stay readable.

## Recovery and failure behaviour

- A damaged or edited line is reported with its line number and reason, and is
  skipped; the rest of the store still loads. Aperio never silently repairs,
  truncates, or rewrites a store.
- A missing header, an unknown format, or an unsupported version fails closed
  with an actionable error.
- A store that ends mid-line (an interrupted write) or whose last line breaks
  the chain **refuses new writes** with an actionable error, because appending
  past a broken chain would make every later line unreadable. Existing records
  remain readable; nothing is repaired or truncated silently.
- Writers hold an exclusive advisory lock (`<store>.lock`) and decide
  "create or append" inside it, so concurrent writers cannot duplicate a
  header, lose a record, or apply one transition twice.
- New stores are published atomically with owner-only permissions; symlinked
  stores, missing parent directories, oversized writes, and stores placed
  inside `.aperio/` project state are refused.
- Nothing is written to `.aperio/`, nothing is deleted, and no memory command
  reads `.aperio/local/`.

## Command summary

| Command | Purpose |
| --- | --- |
| `aperio mem-propose --store F --type T --title S --content C` | add a proposed memory |
| `aperio mem-review STORE ID --accept\|--verify\|--retract\|--supersede` | record a review decision (`--verify` needs `--evidence` and `--actor`) |
| `aperio mem-list STORE [--include-inactive] [--max-sensitivity L] [--as-of D] [--json]` | list memories and states (local inspection shows private records by default; narrow with `--max-sensitivity`) |
| `aperio mem-show STORE MEMORY_ID [--json]` | inspect one memory and its history |
| `aperio mem-recall STORE --objective "..." [--workspace L] [--as-of D] [--json]` | retrieve relevant memories (validity evaluated today in UTC unless `--as-of` is given; `--workspace` matches workspace-scoped memories) |
| `aperio mem-export STORE --output F [--as-of D] [--force]` | write a portable exchange file |
| `aperio mem-import STORE FILE [--dry-run] [--json]` | import an exchange file as proposals |

## Privacy limits worth repeating

Sensitivity classification is a filter, not encryption: a `private` memory is
excluded from packs and exports by default, but it is stored in plain text in
the store file. Aperio does not scan for secrets and cannot stop an operator
from storing a credential in a memory, so do not put secrets in a memory
store. Packs and exports contain only selected memory content, scope,
provenance, review state, and hashes — never paths, hostnames, environment
values, or project state.

## Not in this milestone

- no automatic learning, no promotion from agent output, no scoring engine;
- no embeddings, vector store, cloud sync, or network access;
- no cryptographic authorship or signature scheme;
- no evaluation of whether the knowledge actually improved an agent's output.
