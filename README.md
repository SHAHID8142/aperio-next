# Aperio Next

**A local-first command-line companion that keeps AI-assisted projects organized, reviewable, and portable — even when you switch AI coding agents.**

> **Release status:** `1.2.0rc2`, a release candidate. The Python CLI, onboarding
> flow, and installation were tested on macOS arm64 with Python 3.11–3.14.
> Live authenticated integrations with specific AI agents, Windows, and Linux
> were **not** verified in this audit. See [what is verified](docs/final-handoff.md).

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

You do not need to know Python or Git to use Aperio. You do need to be able to
type commands in a terminal.

## What can it do today?

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
- It does not register a native `/aperio` command inside every AI app. Only the
  terminal CLI is fully verified (see [agent compatibility](#ai-agent-compatibility)).
- It does not guarantee a task's output is correct; it requires recorded
  evidence and a separate review, which is not the same as proof.

## Installation

Aperio is a Python package (Python 3.11 or newer). The recommended way keeps it
in its own isolated environment with **uv**:

```sh
# 1. Install uv (official instructions): https://docs.astral.sh/uv/
# 2. Install Aperio from this repository, pinned to a commit or tag:
uv tool install "git+https://github.com/SHAHID8142/aperio-next.git@5b30fba"
aperio --version
```

`uv tool install` puts an `aperio` executable on your `PATH` and does not touch
your project's dependencies. If `aperio` is not found, run
`uv tool update-shell`, or open a new terminal.

**Alternative (pipx).** If you already use pipx:

```sh
pipx install "git+https://github.com/SHAHID8142/aperio-next.git@5b30fba"
```

**No published PyPI package or GitHub Release asset exists yet.** Install from
git as shown above, or from a locally built wheel. Operating-system notes,
upgrades, and removal are in [docs/install.md](docs/install.md).

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
and the single next action. If it is an existing project, initialization only
creates Aperio's own `.aperio/` scaffolding and never overwrites your files.
Use `aperio onboarding-reconstruct .` to inventory what already exists.

## AI agent compatibility

`aperio host-doctor` prints an honest matrix. In short:

| Host | What is verified |
| --- | --- |
| Terminal CLI (any OS) | **Fully verified** — this is Aperio itself |
| Codex CLI, Claude Code | CLI adapter tested against **simulated** processes; no live account run |
| OpenCode, Cline, Cursor, Kilo, ZCode, Goose, Antigravity, Pi | **Cooperative fallback** — the agent runs Aperio's commands and reads JSON; no native integration |

"Executable found" is not the same as "authenticated session verified". The
doctor reports six separate facts per host so this cannot be misread. Details
and fallbacks: [docs/host-integration.md](docs/host-integration.md).

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

- **`aperio: command not found`** — run `uv tool update-shell` or open a new
  terminal, then `uv tool list` to confirm the install.
- **`aperio next` says the project state is ambiguous** — Aperio will not guess
  or overwrite. Inspect the reported reasons; see
  [docs/troubleshooting.md](docs/troubleshooting.md).
- **An agent shows in `host-doctor` but won't connect** — a probe does not log
  in or make a model request. See [docs/providers.md](docs/providers.md).
- **You want to start over on a project** — Aperio never deletes your files.
  Remove the `.aperio/` folder yourself if you truly want a clean slate.

## Upgrading and uninstalling

```sh
uv tool upgrade aperio-next     # upgrade
uv tool uninstall aperio-next   # remove the program (your project files stay)
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
