# Aperio Next

**A local-first command-line companion that keeps AI-assisted projects organized, reviewable, and portable — even when you switch AI coding agents.**

> **Release status:** `1.2.0rc2` on branch `feat/universal-agent-integration`, a
> release candidate. The Python CLI, onboarding flow, installation, and the
> new agent-skill registration were tested on macOS arm64 (Python 3.14;
> **1769 Python tests and 34 Node tests passed**). Fresh **Codex CLI** sessions
> verified native activation, consented initialization, a governed coding
> task, independent review, and durable recovery. **OpenCode** verified
> native activation and read-only cross-agent recovery. The current Claude
> attempt was blocked by a configured-model diagnostic and timeout.
> VS Code fresh-session automation, other hosts, Windows and Linux remain
> unverified. See the [current release evidence](docs/release-candidate-evidence.md).

## What is Aperio?

Aperio is a tool that sits *next to* an AI coding agent (such as Codex or
Claude Code). It helps you decide what to build, records your decisions, keeps
a plan and task list, and hands that context to whatever agent you use — so you
do not restart from zero when you change tools or open a new chat.

Aperio is **not** an AI model and it does not write code for you. It does not
download an AI, and it does not need an account. It is a plain command-line
program that stores its notes in a `.aperio/` folder inside your project.

It works without any agent too: you can use it purely to organize a project.

## Who is it for?

- People starting a project who want structure without a huge setup.
- People using AI agents who lose context when they switch tools or sessions.
- Teams that need a written record of decisions and what "done" means.

You do not need to know Python or Git to use Aperio.

## What can it do today?

- **Install once, activate in your agent** — a one-command installer registers
  a native `aperio-next` skill in the coding agents it finds (see below).
- **Understand a workspace** — tell an empty folder from an existing project
  without changing anything.
- **Guide discovery** — ask one question at a time and save the answers so you
  can pause and resume later.
- **Organize work** — keep a project brief, decisions, an approved plan, and
  task states in one place.
- **Protect work** — record checkpoints, review decisions, bounded retry
  budgets, and verification evidence.
- **Move between agents** — export a portable brief or context pack and import
  it elsewhere.
- **Assess tools** — find some installed tools and suggest what fits; any
  installation requires your explicit approval.

## What it does NOT do today

- It does not build or deploy your website or app for you.
- It does not intercept or control any agent's chat. The skill is
  *cooperative*: it instructs the agent to run the real engine. Runtime
  guarantees come from the CLI, not from the skill text.
- It does not guarantee a task's output is correct; it requires recorded
  evidence and a separate review, which is not the same as proof.

## Installation

**Requires:** Node 18+ and Python 3.11+ (macOS, Linux, or Windows).

> **Status: pre-release.** `npx aperio-next install` is the *target* one-line
> experience but the npm package is **not on the public registry yet**. Do
> not run it expecting a download — until it is published, use the
> build-from-source path below. These instructions are verified against the
> `feat/universal-agent-integration` branch; `main` does not yet contain the
> npm launcher under `packaging/` or `scripts/`.

### From source (verified today)

The npm launcher, packaging script, and engine all live on the feature
branch, so clone that branch explicitly:

```sh
# 1. Clone the branch that contains the installer (not main yet).
git clone --branch feat/universal-agent-integration \
  https://github.com/SHAHID8142/aperio-next.git
cd aperio-next

# 2. Build the self-contained npm package (bundles the Python engine wheel).
./scripts/build-npm-package.sh          # needs `uv` (or pip) + npm

# 3. Install the freshly packed tarball. Do NOT use a bare `npx aperio-next`:
#    there is no such published package yet.
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next install
```

The installer creates an isolated engine environment in `~/.aperio-next/`,
installs and verifies the `aperio` CLI, then registers skill assets **only**
in the documented skills/commands locations of the agents it detects. Files
it did not create are never touched, undetected hosts are reported as
pending (never as installed), and the summary prints only verified facts.

> **Only one true engine.** The installer embeds the absolute path of the
> engine it just verified (`~/.aperio-next/bin/aperio`) into every skill, so
> a different `aperio` earlier on `PATH` is never invoked. If a legacy
> Aperio is also installed, that isolation is what keeps them apart.

Use the same explicit npx package invocation with `doctor`, `update`, or
`uninstall` in place of `install` for health, refresh, or removal. npx does
not leave a permanent launcher command. Uninstall retains every project's
`.aperio/` data.

### Direct Python install (alternative)

If you prefer uv or pipx directly (no agent-skill registration):

```sh
uv tool install "git+https://github.com/SHAHID8142/aperio-next.git@feat/universal-agent-integration"
aperio --version
```

Operating-system notes, upgrades, and removal:
[docs/install.md](docs/install.md). Integration details:
[docs/integrations.md](docs/integrations.md).

## Activate Aperio Next in your agent

Open your project in any supported agent and use its native mechanism:

| Host | Activation |
| --- | --- |
| ZCode | `/aperio-next` or mention the skill |
| Claude Code | `/aperio-next` |
| OpenCode | `/aperio-next` or `@aperio-next` |
| Codex CLI | `$aperio-next` |
| Gemini CLI | `/aperio-next` |
| Cursor | `/aperio-next` |

The skill runs the real engine read-only first (`aperio next <project>
--json`), walks you through consent-gated onboarding for new projects, and
resumes from durable state in managed ones. Project data, approvals, and
verification stay under your control at every step.

## Quick start — a new project

Open a terminal and run these commands. Each step is safe to repeat.

```sh
mkdir my-project
cd my-project

# 1. Ask Aperio what to do. This only looks; it changes nothing.
aperio next .

# 2. Start the guided, one-question-at-a-time wizard.
aperio start .
```

The wizard explains that it will create the `.aperio/` folder and asks before
doing it. Answer in your own words; type `/pause` to stop safely or `/quit` to
cancel. Run `aperio start .` again later to resume.

Prefer scripts or an agent driving the CLI? Use the non-interactive form:

```sh
aperio onboarding-confirm-init . --approve
aperio onboarding-answer . project.category --answer "Website"
aperio onboarding-readiness .
```

When the required questions are answered, Aperio reports readiness and the next
step. Onboarding gathers information — it does not build anything.

## Existing projects

Run the same read-only check inside any folder:

```sh
cd /path/to/your-project
aperio next .
```

Aperio tells you whether the folder is a new, existing, or ambiguous project,
and the next onboarding action. With consent, initialization creates
`.aperio/` scaffolding and adds a managed section to `AGENTS.md`, preserving
existing instructions and project files.
Use `aperio onboarding-reconstruct .` to inventory what already exists.

## AI agent compatibility

`aperio integrations status` reports registration per host;
`aperio host-doctor` reports engine/provider compatibility. The levels are
kept separate on purpose — "registered" (file written to the host's
documented location) is not "activated" (a live session actually invoked
it):

| Host | Skill registered at | Activation | Live-activated tested |
| --- | --- | --- | --- |
| ZCode | `~/.zcode/skills/`, `~/.zcode/commands/` | `/aperio-next` or skill mention | no launchable binary in this run |
| Claude Code | `~/.claude/skills/`, `~/.claude/commands/` | `/aperio-next` | current attempt blocked by model configuration/timeout |
| OpenCode | `~/.config/opencode/skills/`, `commands/` | `/aperio-next`, `@aperio-next` | yes; read-only cross-agent recovery |
| Codex CLI | `~/.agents/skills/`, repo `.agents/skills/` | `$aperio-next` | yes; coding, review and fresh recovery |
| Gemini CLI | `~/.gemini/skills/`, `commands/` (TOML) | `/aperio-next` | registration only |
| Cursor | `~/.cursor/skills/` | `/aperio-next` | registration only |

Every path above is that host's **officially documented** mechanism
(sources in [docs/integrations.md](docs/integrations.md)). Codex, Gemini,
Cursor, and ZCode also scan the cross-tool `~/.agents/skills/` path, so a
Codex install makes the skill visible there too. Any agent can always use
the cooperative fallback: run `aperio` commands in a terminal.
[docs/host-integration.md](docs/host-integration.md) explains the level
vocabulary.

## Security and limitations

- Everything is local by default. Aperio calls no cloud service.
- Project text and files from other tools are treated as untrusted data, never
  as commands.
- Installing tools, running a write-capable agent, and other impactful actions
  require explicit, recorded approval.
- Archives are **not encrypted**. Never put passwords, API keys, or secrets in
  project documents.
- A passing check proves only that the check passed. See
  [docs/limitations.md](docs/limitations.md) before relying on it for real users.

## Troubleshooting

- **`aperio: command not found`** — the npm installer's engine lives in
  `~/.aperio-next/bin`; add it to PATH (`export PATH="$HOME/.aperio-next/bin:$PATH"`)
  or use the full path. For uv installs, run `uv tool update-shell`.
- **`aperio-next doctor` shows "conflict: foreign file"** — a file aperio did
  not create occupies the skill slot. Aperio never overwrites foreign files:
  rename or remove it yourself, then re-run `aperio-next install`.
- **`aperio next` says the project state is ambiguous** — Aperio will not guess
  or overwrite. Inspect the reported reasons; see
  [docs/troubleshooting.md](docs/troubleshooting.md).
- **The skill does not trigger in your agent** — registration is verified by
  `aperio-next doctor`; activation depends on the host (restart the agent
  session so it re-scans its skills, then invoke `/aperio-next` explicitly).
- **An agent shows in `host-doctor` but won't connect** — a probe does not log
  in or make a model request. See [docs/providers.md](docs/providers.md).
- **You want to start over on a project** — Aperio never deletes your files.
  Remove the `.aperio/` folder yourself if you truly want a clean slate.

## Upgrading and uninstalling

```sh
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next update
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next uninstall

# uv/pipx installs:
uv tool upgrade aperio-next
uv tool uninstall aperio-next
```

Your project data lives in `.aperio/` and is never touched by upgrade or
uninstall.

## Documentation

| I want to… | Read |
| --- | --- |
| Install, upgrade, or remove on my OS | [docs/install.md](docs/install.md) |
| Follow a first project step by step | [docs/quickstart.md](docs/quickstart.md) |
| Understand the interview | [docs/onboarding.md](docs/onboarding.md) |
| Plan and approve tasks | [docs/planning.md](docs/planning.md) |
| Use or switch AI agents | [docs/agent-workflows.md](docs/agent-workflows.md) |
| See exact host support | [docs/host-integration.md](docs/host-integration.md) |
| Understand privacy and permissions | [docs/security-model.md](docs/security-model.md) |
| Look up every command | [docs/cli-reference.md](docs/cli-reference.md) |
| See what was verified for this release | [docs/final-handoff.md](docs/final-handoff.md) |

## Development and contributing

For working on Aperio itself (Python 3.11+):

```sh
git clone https://github.com/SHAHID8142/aperio-next.git
cd aperio-next
uv run --with pytest pytest      # run the full test suite
```

Issues, reproducible bug reports, and documentation corrections are welcome.
Please never attach credentials or private data to a public issue.

## License

MIT — see [LICENSE](LICENSE).
