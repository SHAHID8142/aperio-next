# Provider integrations: support, safety, and honest limits (M7)

Aperio's core is provider-neutral: everything in the universal layer
(context packs, goal contracts, memory, quality, feedback, exchange,
briefs) works by copy/paste with no provider installed. Provider
**adapters** are an optional convenience for launching installed
non-interactive agent CLIs under Aperio's execution records. This page
states what each adapter supports and what has — and has not — been
verified. The adapter mechanics and safety model are specified in
[feature-07.md](feature-07.md).

## Adapter support matrix

| Adapter | Launches | Officially documented interface | Read-only by default | Write mode |
| --- | --- | --- | --- | --- |
| `manual` | nothing — writes a portable run-handoff for any agent | n/a (portable text) | always | n/a |
| `local-process` | a configured local executable (argv array, no shell) | the executable's own CLI | yes (no exec without `--authorize-exec`) | by configuration |
| `codex-cli` | `codex exec --json` (OpenAI Codex CLI) | documented non-interactive mode | yes (`sandbox read-only`) | `workspace-write` only with `--authorize-write` |
| `claude-cli` | `claude -p` (Claude Code CLI) | documented non-interactive mode | yes (`plan` mode, restrictive `--tools`/`--allowedTools`) | `acceptEdits` (+ Bash only with explicit `allow_shell` and write consent) |

No other providers are claimed. GUI applications (ZCode, Cursor, and
similar) are explicitly out of scope.

## Capability detection and safe failure

- `aperio agent-list` shows adapters; `aperio agent-doctor --agent ID`
  passively checks the installed CLI (version/flag probe) and the
  configured mode without any model request and without creating records.
- A missing executable or an unsupported CLI version is refused **before**
  any run record, task claim, or process exists
  (`test_missing_provider_executable_is_rejected_before_mutation`).
- `--authorize-exec` and `--authorize-write` are two independent
  consents; a write request without its consent fails before any claim.
- Structured provider output is reduced to bounded neutral metadata; raw
  model text stays in machine-local logs and never enters shareable
  records. Exit 0 is never treated as verification.

## What has actually been verified

- **Locally verified (this repository's test suite):** adapter contract
  behavior with test doubles — prompt-on-stdin, argument allowlists,
  capability probes, consent separation, timeouts, pipe-draining bounds,
  malformed/oversized structured output handling, cancellation, privacy
  boundaries, governance interaction, and preview-vs-execution semantics.
- **Not verified (stated plainly):** end-to-end execution against the
  real Codex CLI or Claude CLI services. No provider credentials are used
  in this project's verification, and no claim of live-provider testing
  is made. The adapters validate the documented interfaces (probe the
  flags they rely on before launch) and fail closed when they are absent.

If you cannot or do not want to install a provider CLI, the `manual`
adapter and the copy/paste workflows in
[agent-workflows.md](agent-workflows.md) are complete alternatives —
the universal layer never requires a provider.

## Rules that do not change with providers

- Operator instructions stay authoritative; a provider's output is an
  attested-at-most claim that evaluation and review still gate.
- Governance (Feature 06) applies to provider dispatch like any other
  execution; a policy approval never substitutes for `--authorize-exec`.
- No implicit network requests: Aperia launches a configured executable
  and nothing else; the provider CLI owns its own authentication and
  network activity, and Aperia never handles credentials.
- No lock-in: the adapter contract is provider-neutral plain classes;
  adding a provider means one documented adapter, not core changes.
