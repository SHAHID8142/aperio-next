# Feature 07 — Real AI agent integrations & provider-neutral CLI bridge

Status: implemented and hardened in `aperio 0.7.1`. Features 01–06 remain
compatible. This is a supervised local-process bridge, not a cloud
integration, model SDK, autonomous loop, or OS sandbox.

## Scope

Aperio can dispatch one approved task to an installed, non-interactive AI
coding CLI and supervise it exactly like the other subprocess adapters.

| Adapter | Installation | Non-interactive interface |
|---|---|---|
| `codex-cli` | OpenAI Codex CLI (`codex`) | `codex exec`, JSONL events, prompt on stdin |
| `claude-cli` | Anthropic Claude Code CLI (`claude`) | `claude -p`, JSON result, prompt on stdin |

`manual` and `local-process` are unchanged. ZCode Desktop, Cursor Desktop,
and other GUI applications are **not** integrated: they have no officially
supported, independently verified non-interactive automation interface used
here.

## Two separate consents

Launching a provider and allowing it to modify the workspace are different
decisions:

| Consent | Meaning |
|---|---|
| `--authorize-exec` | Launch the provider process at all |
| `--authorize-write` (alias `--authorize-workspace-write`) | Allow the workspace-write/file-edit mode |

Both providers default to read-only. A write-capable configuration without
the second consent fails in the Python runtime **before** a run record is
created or a task is claimed. Plan approval and role authorization remain
separate governance decisions; neither substitutes for process consent.
Without `--authorize-exec`, dispatch is still an entirely read-only preview:
no record, no claim, no process.

## Provider-neutral boundary

`CliAgentAdapter` in `src/aperio/adapters.py` owns the shared contract:

- strict known-key JSON config validation before any persistent mutation;
- **no generic `args` prefix**: arbitrary provider flags are refused because
  provider precedence is not a security boundary;
- working directory resolved inside the project boundary (a path boundary,
  not an OS sandbox);
- deterministic prompt rendered from the approved task and delivered only
  over stdin, never argv;
- no shell and no string command interpolation;
- minimal environment by default, with `env_policy: "inherit"` as an
  explicit operator risk;
- bounded structured-output capture (1 MiB), a 256 KiB event bound, and a
  full machine-local mirror under `.aperio/local/runs/<run-id>.log`;
- hard timeout, SIGTERM → 5 s grace → SIGKILL on the owned POSIX process
  group, Windows TerminateProcess;
- bounded pipe draining: a descendant that inherits stdout/stderr cannot
  hang the dispatcher after the provider exits;
- child reaped; exit, timeout, interruption, cancellation, byte counts, and
  truncation recorded as neutral metadata;
- provider capability probe (`--version`, `--help`) before an authorized
  launch; missing required controls fail closed before any run or claim;
- raw output, prompt, executable path, config, and environment never enter
  `executions.json` or portable memory.

Exit 0 means only that the provider process finished. It is never
verification, task completion, or independent review.

## Configuration

Store machine-specific config under `.aperio/local/`; it is git-ignored and
never exported. Both adapters accept only:

| Key | Meaning |
|---|---|
| `executable` | CLI name or absolute wrapper path; defaults to `codex` / `claude` |
| `model` | optional model alias/name passed to the provider |
| `working_dir` | optional directory inside the project (default project root) |
| `timeout_seconds` | 1–3600; default 1800 for provider adapters |
| `env_policy` | `minimal` (default) or `inherit` |

`args`, `permission_mode`, and `allowed_tools` are deliberately **not**
configurable. Aperio derives the complete privileged portion of argv.

### `codex-cli`

Additional keys:

| Key | Default | Allowed / meaning |
|---|---|---|
| `sandbox` | `read-only` | `read-only` or `workspace-write`; `danger-full-access` is refused |
| `skip_git_repo_check` | `true` | pass `--skip-git-repo-check` |
| `ephemeral` | `true` | pass `--ephemeral` (no persisted Codex session files) |

Read-only effective shape:

```text
codex exec --json --sandbox read-only -C <project> \
  -c approval_policy='never' --skip-git-repo-check --ephemeral \
  [-m <model>] -
```

With `sandbox: "workspace-write"` **and** `--authorize-write`, the only
difference is `--sandbox workspace-write`. The final `-` reads the prompt
from stdin. Codex commands run inside its own sandbox; the read-only mode
blocks filesystem writes.

### `claude-cli`

Additional keys:

| Key | Default | Meaning |
|---|---|---|
| `allow_shell` | `false` | request the restricted `Bash` tool; requires write consent |
| `max_turns` | unset | optional 1–100 provider turn limit (help does not always list this documented flag) |
| `max_budget_usd` | unset | optional 0–1000 cost cap |
| `no_session_persistence` | `true` | pass `--no-session-persistence` |

Aperio distinguishes Claude's two documented controls:

- `--tools`: which tools exist for the model;
- `--allowedTools`: which of those may execute without prompting.

Read-only mode derives **both** from `Read,Grep,Glob` and uses
`--permission-mode plan`. It does not grant `Bash`, `Edit`, or `Write`.

```text
claude -p --permission-mode plan --permission-prompts none \
  --tools Read,Grep,Glob --allowedTools Read,Grep,Glob \
  --output-format json --no-session-persistence \
  [--max-turns N] [--max-budget-usd N] [--model <model>]
```

With `--authorize-write`, the mode becomes `acceptEdits` and the tool set is
`Read,Grep,Glob,Edit,Write`. `Bash` is added only when `allow_shell: true`
and write consent is present. `bypassPermissions`, `--permission-mode`
overrides, and operator-supplied tool lists are refused. `--permission-prompts
none` means prompted tools are denied; it is not treated as a sandbox.

Example write-capable config:

```json
{
  "executable": "claude",
  "working_dir": "/absolute/path/to/project",
  "timeout_seconds": 1800,
  "env_policy": "minimal",
  "allow_shell": true,
  "max_turns": 30,
  "max_budget_usd": 5
}
```

## Usage

```sh
aperio agent-list

# Read-only preview: command, cwd, effective permissions, prompt digest.
aperio run-preview ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json

# Read-only execution.
aperio run-start ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json --authorize-exec

# Codex workspace-write requires both consents.
aperio run-start ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json \
  --authorize-exec --authorize-write

# Passive compatibility report: version and flags only, no login/model call.
aperio agent-doctor ./project --agent claude-cli \
  --config .aperio/local/claude.json --json
```

Under governance, `run-start` requires an actor with `execution.dispatch`;
the resulting task owner is `<adapter>:RUN-NNNN`, while `implementer` remains
the declared actor. Evidence and independent review are still required for
completion.

## Structured provider results

- Codex runs with `--json`; each bounded JSONL event is validated. The
  record keeps only neutral metadata: event counts, item-type counts, turn
  completion/failure, model-reported status, and bounded token usage.
- Claude runs with `--output-format json`; the single result object is
  validated for `type`, `subtype`, `is_error`, turns, cost, token usage, and
  denial count. Raw result text is never persisted.
- Malformed, empty, oversized, incomplete, and model-failed output is
  reported separately as `provider_result.parse_status` /
  `model_reported`. Process exit status remains a separate field, and both
  remain separate from task verification.
- Model events cannot perform Aperio mutations. They are inert metadata.

## Bounded cleanup

After the provider leader exits, reader threads are joined for a strict
2-second grace. If a descendant still holds the inherited pipes, Aperio
SIGKILLs the process group it created, then closes local pipe handles as the
final unblock. No wait is unbounded. Keyboard interrupt, timeout,
cancellation, startup failure, and normal exit all reap the child and close
the machine-local log. On Windows the direct process is terminated; a full
descendant tree cannot be guaranteed and is documented rather than claimed.

## Provider compatibility and doctor

An authorized launch first runs `--version` and the provider's `--help`
(Codex: `exec --help`) with a bounded timeout and no model request. Every
safety-relevant flag Aperio will rely on must be advertised, otherwise the
launch is refused before a run/claim. This detects unsupported CLI versions
instead of guessing precedence. Claude's help does not list every supported
flag, so the optional documented `--max-turns` is not required from help;
the safety controls (`--permission-mode`, `--permission-prompts`, `--tools`,
`--allowedTools`, `--output-format`) and the required `plan`/`acceptEdits`/
`json` values are.

`aperio agent-doctor <project> --agent ... [--config ...] [--json]` reports,
separately: installation, resolved executable, version, required/missing
flags, config validity, authentication status (only safely confirmable:
currently reported as unknown because passive diagnostics never read
credentials), read-only support, workspace-write support, and live-test
status (unverified unless the operator performs a separate authorized test).
It never logs in, installs, or makes a paid model request.

## Security acceptance matrix

| Threat / requirement | Control | Verified by |
|---|---|---|
| Command injection | argv array, `shell=False`, no interpolation | provider invocation tests |
| Unsafe raw overrides | `args` refused, including `--flag value` and `--flag=value` | rejection tests |
| Sandbox/permission override | fixed Codex sandbox enum; Claude mode derived; bypass refused | validation tests |
| Prompt leakage through process list | prompt on stdin only | argv assertions |
| Filesystem scope | working directory resolved inside project | config validation tests |
| Unauthorized paid/model execution | `--authorize-exec`; preview creates nothing | preview tests |
| Silent workspace writes | default read-only; separate `--authorize-write`; runtime fail-closed | write-refusal/CLI tests |
| Claude tool confusion | both `--tools` and `--allowedTools` derived from one mode; no Bash by default | argv tests |
| Unsupported CLI controls | `--version`/`--help` capability probe before run | missing-flag tests |
| Interactive hang | approval policy never; print mode + permission prompts none | invocation tests |
| Secret/model text leakage | only neutral structured metadata persisted | serialization/export tests |
| Runaway process | hard timeout; owned process-group termination | timeout/cancel tests |
| Descendant holds pipes | bounded 2 s drain, group kill, pipe close | real descendant test |
| Unbounded/unsafe events | 1 MiB capture, 256 KiB/event, 4096 events | malformed/oversized tests |
| Claim corruption on startup failure | preflight before record/claim; recovery boundary | missing-executable/write tests |
| Governance bypass | existing policy gate runs before config/start | governed provider test |
| Agent success treated as completion | task remains in-progress after exit 0 | provider tests |

## Provider verification status

- Claude Code `2.1.319` was available locally when the feature was built and
  its documented flags were checked. Tests use executable fake CLIs and do
  not make live model requests.
- Codex was not installed on the build machine; the `codex exec` contract is
  based on OpenAI's documented `--json`, `--sandbox`, `--cd`/`-C`,
  `--config`/`-c`, `--skip-git-repo-check`, `--ephemeral`, and model flags.
  The capability probe refuses an installation that does not advertise them.
- **LIVE PROVIDER TESTING NOT VERIFIED.** No credentials, paid request,
  login, or account operation was used.

## Honest limitations

- These are real local processes with operator privileges; Aperio does not
  provide an OS sandbox. Codex's sandbox and Claude's permission/tool
  contract are provider-enforced controls, not a kernel jail.
- Prompt instructions tell the agent not to alter `.aperio/` control state or
  invoke completion/review commands, but a shell-capable agent can attempt to
  do so. Governance state mutations still require declared actors and pass
  all policy checks; agent output is never trusted as verification.
- Task/repository text can contain prompt injection. Plan approval and human
  review remain the trust boundary; Aperio does not sanitize semantic
  instructions inside approved task content.
- A configured executable is trusted; Aperio checks installation/path and
  advertised flags but does not authenticate that a binary is genuinely the
  named vendor CLI.
- The providers may load their own project/user configuration, plugins,
  hooks, MCP servers, or instruction files (`AGENTS.md`/`CLAUDE.md`).
  Aperio does not disable those facilities because doing so can break
  authentication and normal project context. They are part of the trusted
  provider environment and the approved project content boundary; the
  read-only tool contract still denies prompted tools, and `--tools` is a
  built-in-tool restriction, not an MCP deny-list.
- Provider CLIs may add their own session/auth telemetry. Aperio does not
  configure credentials, network access, or vendor accounts.
- A working directory is a path boundary, not an OS sandbox. Network
  isolation is not provided.
- Windows descendant cleanup is best-effort and not a full tree guarantee.
- No streaming UI, provider SDK, token reconciliation, automatic retry, or
  cloud synchronization is provided.
- GUI applications (ZCode, Cursor) are not advertised as integrations.

## Verification

- Feature 01–06 suites remain green.
- Provider tests use executable fake CLIs to exercise real argv, stdin, cwd,
  environment filtering, logs, previews, capability refusal, two-consent
  enforcement, structured success/incomplete/failure/malformed/oversized
  output, timeout, operator interrupt, cancellation, descendant-pipe
  cleanup, governance, doctor, and export/import privacy.

## Unresolved release blocker (P1)

Provider execution is **not release-ready** until the metadata-bound mismatch
is corrected:

- `adapters._bounded_usage()` accepts numeric values up to `10**12`;
- `runtime._validate_provider_result()` rejects usage values above
  `1_000_000_000` (and applies corresponding bounds to Claude
  `num_turns`/`duration_ms`).

A parser can therefore produce a structurally valid `provider_result` that
fails record validation after the child exits, leaving an abandoned active
run. M1 deliberately does not change this path. The fix must unify one
documented bound and add fail-first boundary tests for every affected field.
