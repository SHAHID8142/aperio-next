# Feature 07 — Real AI agent integrations & provider-neutral CLI bridge

Status: implemented in `aperio 0.7.0`. Feature 01–06 behavior is preserved.

## Scope

Aperio can dispatch one approved task to an installed, non-interactive AI
coding CLI and supervise it exactly like other subprocess adapters. This is
not a model SDK integration, an autonomous loop, or a sandbox.

Built-in adapter ids:

| Adapter | Installation | Non-interactive interface |
|---|---|---|
| `codex-cli` | OpenAI Codex CLI (`codex`) | `codex exec` with prompt on stdin |
| `claude-cli` | Anthropic Claude Code CLI (`claude`) | `claude -p` print mode with prompt on stdin |

`manual` and `local-process` are unchanged. ZCode Desktop, Cursor Desktop,
and other GUI applications are **not** integrated: they have no officially
supported, independently verified non-interactive automation interface used
here.

## Provider-neutral boundary

`CliAgentAdapter` in `src/aperio/adapters.py` owns the shared contract:

- strict JSON config validation before any persistent mutation;
- working directory resolved inside the project boundary;
- deterministic prompt rendered from the approved task;
- prompt delivered only over stdin, never argv;
- no shell and no string command interpolation;
- minimal environment by default, with `env_policy: "inherit"` as an
  explicit operator risk;
- bounded stdout/stderr capture (64 KiB retained tail per stream) and a full
  machine-local mirror under `.aperio/local/runs/<run-id>.log`;
- hard timeout, SIGTERM → 5 s grace → SIGKILL on POSIX (direct process and
  its own process group), Windows TerminateProcess;
- child reaped, exit code and neutral byte/truncation metadata recorded;
- runtime authorization `--authorize-exec` required before `start()`;
- without authorization, dispatch is a read-only preview (no run record, no
  claim, no process);
- raw output, prompt, executable path, config, and environment never enter
  `executions.json` or portable memory.

Exit 0 means only that the provider process finished. It is never
verification, task completion, or independent review.

## Configuration

Store machine-specific config under `.aperio/local/`; it is git-ignored and
never exported. Both adapters accept:

| Key | Meaning |
|---|---|
| `executable` | CLI name or absolute wrapper path; defaults to `codex` / `claude` |
| `args` | optional prefix argv (operator-trusted; no shell) |
| `model` | optional model alias/name passed to the provider |
| `working_dir` | optional directory inside the project (default project root) |
| `timeout_seconds` | 1–3600; default 1800 for provider adapters |
| `env_policy` | `minimal` (default) or `inherit` |

### `codex-cli`

Additional keys:

| Key | Default | Allowed / meaning |
|---|---|---|
| `sandbox` | `workspace-write` | `read-only` or `workspace-write`; `danger-full-access` is refused |
| `skip_git_repo_check` | `true` | pass `--skip-git-repo-check` |
| `ephemeral` | `true` | pass `--ephemeral` (no persisted Codex session files) |

Effective shape:

```text
codex exec --sandbox workspace-write -C <project> \
  -c approval_policy='never' --skip-git-repo-check --ephemeral \
  [-m <model>] -
```

The final `-` reads the prompt from stdin. Additional prefix arguments from
`args` are inserted before `exec`.

### `claude-cli`

Additional keys:

| Key | Default | Meaning |
|---|---|---|
| `permission_mode` | `acceptEdits` | one of `default`, `acceptEdits`, `plan`, `dontAsk`, `auto`, `manual`; `bypassPermissions` is refused |
| `allowed_tools` | `["Read","Edit","Write","Bash"]` | comma-joined into `--allowedTools`; no commas/control characters |
| `max_turns` | `25` | 1–100 |
| `max_budget_usd` | unset | optional 0–1000 cost cap |
| `no_session_persistence` | `true` | pass `--no-session-persistence` |

Effective shape:

```text
claude -p --permission-mode acceptEdits --permission-prompts none \
  --allowedTools Read,Edit,Write,Bash --max-turns 25 \
  [--no-session-persistence] [--max-budget-usd N] [--model <model>]
```

The prompt is piped on stdin. `--permission-prompts none` prevents an
unattended run from waiting for a human.

Example:

```json
{
  "executable": "claude",
  "working_dir": "/absolute/path/to/project",
  "timeout_seconds": 1800,
  "env_policy": "minimal",
  "permission_mode": "acceptEdits",
  "allowed_tools": ["Read", "Edit", "Write", "Bash"],
  "max_turns": 30,
  "max_budget_usd": 5
}
```

## Usage

```sh
aperio agent-list

# Read-only preview: prints command, cwd, timeout, prompt digest.
aperio run-preview ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json

# Refuses to start without explicit authorization.
aperio run-start ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json

# Real execution.
aperio run-start ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json --authorize-exec

aperio run-status ./project RUN-0001 --json
```

Under governance, `run-start` requires an actor with `execution.dispatch`;
the resulting task owner is `<adapter>:RUN-NNNN`, while `implementer` remains
the declared actor. Evidence and independent review are still required for
completion.

## Security acceptance matrix

| Threat / requirement | Control | Verified by |
|---|---|---|
| Command injection | argv array, `shell=False`, no interpolation | provider invocation tests |
| Prompt leakage through process list | prompt on stdin only | argv assertions |
| Arbitrary filesystem scope | working directory resolved inside project | config validation tests |
| Unauthorized paid/model execution | `--authorize-exec`; preview creates nothing | preview tests |
| Unsafe provider modes | Codex `danger-full-access` and Claude `bypassPermissions` refused | validation tests |
| Interactive hang | Codex approval policy never; Claude print mode + permission prompts none | invocation tests |
| Secret/config leakage into shareable records | only adapter id, exit and neutral metadata persisted | serialization assertions |
| Runaway process | hard timeout; process-group termination on POSIX | timeout tests |
| Unbounded output | 64 KiB tail capture; full mirror stays machine-local | existing + provider tests |
| Unattended cost (Claude) | optional `max_budget_usd` | config/argv validation |
| Claim corruption on startup failure | preflight executable check; existing recovery boundary | missing-executable test |
| Governance bypass | existing policy gate runs before config/start | governed provider test |
| Treating agent success as completion | task remains in-progress after exit 0 | provider tests |

## Provider compatibility

- Claude Code flags were checked against the locally installed
  `claude 2.1.319 --help` (print mode, permission mode/prompts, allowed
  tools, max turns, budget, no-session-persistence).
- Codex was not installed on the build machine; the `codex exec` shape is
  based on OpenAI's documented interface (`--sandbox`, `--cd`/`-C`,
  `--skip-git-repo-check`, `--ephemeral`, model, and stdin prompt).
- No live model request was sent during the build: tests use executable
  fake CLIs so they exercise the process contract without credentials,
  network use, or account charges.

## Honest limitations

- These are real local processes with operator privileges; they are **not**
  sandboxed by Aperio. Codex applies its own workspace sandbox when configured;
  Claude permission modes are provider-enforced, not an OS jail.
- Prompt instructions tell the agent not to alter `.aperio/` control state or
  invoke completion/review commands, but a shell-capable agent can attempt to
  do so. Governance state mutations still require declared actors and pass
  all policy checks; agent output is never trusted as verification.
- Task/repository text can contain prompt injection. The plan approval and
  human review remain the trust boundary; Aperio does not sanitize semantic
  instructions inside approved task content.
- A configured executable is trusted; Aperio checks installation/path but
  does not authenticate that a binary is genuinely the named vendor CLI.
- Provider CLIs may add their own session/auth telemetry. Aperio does not
  configure credentials, network access, or vendor accounts.
- Windows termination kills the direct process, not a full descendant tree.
- No network isolation, token accounting, streaming UI, provider SDK, or
  automatic retry is provided.
- GUI applications (ZCode, Cursor) are not advertised as integrations.

## Verification

- Existing Feature 01–06 suites remain green.
- New tests use executable fake provider CLIs to exercise real argv, stdin,
  cwd, environment filtering, logs, preview, authorization, nonzero exit,
  timeout, missing executable, governance, and CLI discovery.
- One integrated CLI smoke run is executed against the built package.
