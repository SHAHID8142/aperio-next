# Universal onboarding (Step 01)

Aperio Step 01 turns `/aperio-next` — the intended universal onboarding
experience — into working, portable software: open any folder, invoke
onboarding, and receive a safe, structured, one-question-at-a-time
conversation that classifies the workspace, initializes only with
explicit approval, understands existing projects without modifying them,
and ends with an honest readiness evaluation.

**Status: implemented and tested locally.** Every claim below is backed
by automated tests (`tests/test_onboarding_*.py`, including a security
regression suite and seven automated end-to-end scenarios). Live
verification inside commercial AI hosts is **not** claimed — see
[Host compatibility](#host-compatibility).

---

## Supported user experience

The canonical universal entry point is the CLI (the only runtime-enforced
path in Aperio's [host-integration model](host-integration.md)):

```bash
aperio next [DIR]                    # classify/resume, ask ONE thing
```

`aperio next` is a read-only route through the same onboarding facade as
`aperio onboarding-start`; the longer command remains available for explicit
workflows and backward compatibility.

### For people who prefer answering to typing commands

`aperio start [DIR]` is an **opt-in interactive wizard** that drives the same
facade one step at a time. It is for humans at a terminal, not for scripts:

- it refuses to run when stdin/stdout is not a TTY (agents must use `aperio next`);
- it explains exactly what the first mutation creates and asks before doing it;
- it supports `/pause`, `/quit`, and `/back` at the prompts;
- it bounds answer length and never invents an approval.

`aperio next` remains strictly read-only, so nothing about the scriptable
protocol changes when the wizard is available.

Every onboarding command answers with **exactly one next action**. With
`--json`, the answer is a machine-readable *protocol document*
(`aperio-onboarding-protocol` v1) carrying `action_required`, one
`question` (or `choices`), and the session identity — that document **is**
the host-integration contract for AI hosts that cannot embed Aperio
natively: the host renders the question, collects the operator's answer,
and invokes the next command.

```bash
aperio onboarding-confirm-init DIR --approve     # only after explicit consent
aperio onboarding-decline DIR                    # exit; nothing modified
aperio onboarding-answer DIR QUESTION_ID --answer "…"
aperio onboarding-revise DIR QUESTION_ID --answer "…"
aperio onboarding-intent DIR --choice continue   # existing projects
aperio onboarding-status DIR
aperio onboarding-pause DIR
aperio onboarding-cancel DIR --confirm
aperio onboarding-reconstruct DIR [--output F]
aperio onboarding-propose-standardization DIR [--output F]
aperio onboarding-apply-standardization DIR --proposal ID --approve
aperio onboarding-discover-tools DIR [--user-config] [--probe TOOL]
aperio onboarding-recommend-tools DIR [--category "Web application"]
aperio onboarding-tool-plan DIR TOOL_ID
aperio onboarding-tool-install DIR TOOL_ID --approve --authorize-install
aperio onboarding-readiness DIR
aperio onboarding-report DIR [--output F]
```

`Not sure yet` and `Decide later` are always accepted answers; they are
recorded as `unsure`/`deferred` with a severity, never guessed around.

## New project onboarding

1. `aperio onboarding-start DIR` classifies the workspace. An empty
   directory → classification `empty_new`; the one question is
   *"Would you like to initialize a new project here?"*
2. **No** → `onboarding-decline`: nothing is initialized, installed, or
   modified. **Yes** → `onboarding-confirm-init --approve` runs the
   existing safe init mechanism (`aperio init`: create-missing-only,
   symlink-refusing, never overwrites) and opens the interview.
3. The first interview question is the category driver
   (*"Which best describes this project?"*), which selects the domain
   profile; the next question is always
   *"What would you like to build or accomplish?"*.
4. Step 01 **prepares** the project; it does not implement the requested
   application. The readiness report states this boundary explicitly.

## Existing project onboarding

1. Discovery classifies content-bearing workspaces as
   `existing_non_aperio`, `legacy_orchestrated` (previous orchestration
   framework evidence: `AGENTS.md`, `CLAUDE.md`, `.cursor`, `.mcp.json`,
   …), or `aperio_project`.
2. The one question is *"What would you like to do with this project?"*
   with the seven suggested responses (continue work, start a new task,
   review health, organize with Aperio, recover incomplete work, review
   documentation, something else) plus a free-form answer.
3. `onboarding-reconstruct` reads relevant documents **read-only** and
   reports pending-work claims, claim conflicts, and untrusted-content
   risk findings (see [Reconstruction](#reconstruction)).
4. Gap-filling reuses the same interview engine: topics with no
   documented coverage become the next interview questions, with
   provenance for every documented fact.

## The adaptive interview contract

- **One primary question per turn.** `next_question()` returns at most
  one question; the CLI renders one; the protocol carries one.
- Questions have stable ids (`foundation.goal`, `technical.platforms`,
  …), a coverage category, the reason they are asked, and concise
  suggestions. The full registry is inspectable:
  `from aperio.onboarding_interview import write_registry_markdown`.
- **Domain profiles** (`web_app`, `desktop_app`, `website`, `mobile_app`,
  `saas`, `api`, `automation`, `ai_app`, `design`, `research`, `writing`,
  `business_workflow`, `general`) select *required* vs *recommended*
  questions per category. Irrelevant questions are never asked (for
  example, SEO is never asked for research; browser tooling is not
  recommended for desktop apps).
- Declarative `when` conditions adapt follow-ups to prior answers; a
  **deferred** answer never satisfies a condition that needs a concrete
  value.
- Revising an answer keeps the full history in the session; a confirmed
  revision clears the pending unresolved item the deferral created.
- Profiles are extensible without touching the universal core:
  `register_profile(InterviewProfile(...))` or
  `load_profile_extension("profile.json")` (validated; unknown question
  ids refused).

## State and recovery behavior

Session state lives in `.aperio/onboarding/session.json`
(`aperio-onboarding-session` v1): workspace identity, session id,
classification, stage, every question answered with provenance
(`confirmed`/`unsure`/`deferred`), decisions, unresolved items with
severity (`critical_blocker`, `required_before_action`,
`pending_decision`, `optional`), scoped approval records, tool records,
blockers, and a bounded audit history.

- Writes are atomic (exclusive temp + link/replace publish) and mutations
  run under an advisory lock with byte compare-and-swap — concurrent
  writers cannot silently clobber each other.
- An operator who answered thirty questions does **not** answer them again
  after a chat ends: `onboarding-start` resumes, never repeats answered
  questions, and never repeats completed side effects.
- Corrupt, non-UTF-8, symlinked, newer-schema, or workspace-mismatched
  sessions are **refused with an actionable error, never rewritten**.
- A leftover `session.json.tmp` from a crashed write blocks the next
  write with explicit guidance; reading is unaffected. Nothing is ever
  deleted automatically.

## Reconstruction

`onboarding-reconstruct` (read-only, bounded: 24 documents, 64 KiB each)
inventories README/PRD/SPEC/AGENTS/CLAUDE/TODO/ROADMAP/PLAN-style
documents plus `docs/`. For each extracted item it records the source
path, line number, and the document's SHA-256 at read time.

Honesty rules enforced in code:

- Checkboxes become `documented_pending` / `documented_complete` **claims
  with attribution — never approved tasks, never verified completion**.
- `key: value` claims that disagree across documents are reported as
  candidate conflicts for operator resolution.
- Imperative patterns ("delete files", "ignore all previous
  instructions", "install …", "reveal the secret …") and secret-shaped
  strings become **risk findings** (location + bounded excerpt; secrets
  redacted entirely). They authorize nothing.
- Existing Aperio artifacts (decision log, lifecycle plan tasks) are read
  through their own modules and reported separately from documented
  claims.

## Safe standardization

For existing projects, Aperio can organize knowledge into its
architecture — under full operator control:

1. `onboarding-propose-standardization` produces a proposal
   (`aperio-onboarding-standardization-proposal` v1): inventoried
   documents, current source-of-truth locations, **proposed additions
   only** (existing files are never modified — the modification list is
   empty by design), conflicts needing operator decisions, explicitly
   untouched files, systems that must remain operational, backup/rollback
   plans, and compatibility risks.
2. Applying requires an approval record in the session scoped to the
   **exact proposal id** (`standardization.apply`). A prompt or a
   document saying "yes" is not approval — the check runs in the Python
   boundary.
3. Apply = init repair (create-missing-only) + an exclusive-created
   mapping record + a pre-apply checkpoint refresh. On failure, only
   files the attempt created are removed; pre-existing content is never
   touched.
4. Post-apply verification reports document drift honestly instead of
   hiding it.

## Tool discovery and recommendations

`onboarding-discover-tools` reports hosts, executables, and MCP servers
with **distinct, non-interchangeable status values**: `detected`,
`installed`, `configured`, `verified_by_probe`, `available_unconfigured`,
`missing`, `unsupported`, `unknown`. Installed does not imply
authenticated or working; configured does not imply authenticated; a
successful probe proves only that a binary runs.

- Read-only by default: project-local markers and bounded JSON parsing
  of `.mcp.json` / `mcp.json` / `.vscode/mcp.json` (contents are data,
  never instructions). A small documented set of user-level config paths
  is scanned only with explicit `--user-config`; nothing else on the
  machine is ever read.
- Executables are located with `which` and never executed without an
  authorized `--probe TOOL` (one bounded version command, hard timeout).
- `onboarding-recommend-tools` selects from a static, honest registry
  (Playwright MCP for browser automation, Chrome DevTools MCP for
  Chromium diagnostics, pytest for testing) by project category and
  missing coverage topics. Sources are *declared* ("verify before
  trusting"); maintenance status is `unknown (no network lookup
  performed)`; recommendations are category-gated so a desktop project
  gets testing guidance rather than browser tooling.

## Permission and installation model

`onboarding-tool-plan` produces the ten mandated plan items (name,
purpose, source, candidate version, compatibility, required permissions,
network access, exact command, security considerations, removal) plus a
manual fallback. Installing requires **two independent gates**:

1. a recorded session approval scoped to the tool id
   (`aperio onboarding-tool-install` records it from `--approve`), and
2. the explicit `--authorize-install` flag on the executing invocation.

Execution runs the plan's fixed argv — no shell, stdin closed, bounded
output capture, hard timeout, process-group kill on overrun — then
re-records status honestly (`install_command_completed` is explicitly
*not* verification). Tools without a safe portable install path (for
example pytest, which depends on the environment) are **manual-only:
aperio never pretends a tool was installed**. Declined installs change
nothing and do not block onboarding when the tool is optional.

## Host compatibility

| Host | Support | Invocation |
| --- | --- | --- |
| Terminal / CLI (any OS) | runtime-enforced | `aperio next` — canonical supported path |
| Any text-chat agent | cooperative | run the commands, paste the JSON protocol |
| Claude Code, Codex CLI, OpenCode | cooperative | the host runs the same CLI commands; **no native slash-command is claimed** |
| ZCode Desktop, Cursor, GUI hosts | not integrated | fallback: run the portable CLI in a terminal |

No native `/aperio-next` slash command is claimed for any host: Aperio
has no verified, documented mechanism to register slash commands inside
those hosts, and it does not invent one. The `--json` protocol is the
portable integration; a cooperating host renders the question and calls
back with `onboarding-answer`.

## Quick-start examples

```bash
# New project in an empty folder
cd ~/new-idea
aperio onboarding-start
aperio onboarding-confirm-init . --approve          # after you say yes
aperio onboarding-answer . project.category --answer "Desktop application"
aperio onboarding-answer . foundation.goal --answer "A menu-bar pomodoro timer"
# … one answer at a time; 'Not sure yet' is a valid answer
aperio onboarding-readiness .
aperio onboarding-report . --output readiness.md

# Machine-readable drive from a host agent
aperio onboarding-start . --json    # -> action_required + one question
aperio onboarding-answer . "$QID" --answer "$ANSWER" --json
```

## Security limitations

Stated plainly: discovery and reconstruction are read-only but **not a
sandbox** — they read file names and bounded text inside the workspace.
Hashes are integrity evidence, not authentication. Status detection is
name-based heuristic evidence, not proof that any tool works. Approval
records are declarations stored in the project; they enforce Aperio's
own boundaries (apply/install refuse without them) but are not an
operating-system security mechanism. Prompt-injection resistance is
structural (instructions become findings, never actions) — no scanner
proves the absence of manipulation. Secrets found in documents are
redacted from Aperio outputs but remain in the original files.

## Testing instructions

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/test_onboarding_discovery.py \
                 tests/test_onboarding_session.py \
                 tests/test_onboarding_interview.py \
                 tests/test_onboarding_reconstruct.py \
                 tests/test_onboarding_standardize.py \
                 tests/test_onboarding_tools.py \
                 tests/test_onboarding_readiness.py \
                 tests/test_onboarding_cli.py \
                 tests/test_onboarding_security.py \
                 tests/test_onboarding_e2e.py -v
```

The E2E file automates the seven acceptance scenarios (fresh folder,
existing website, complex/conflicting project, interruption recovery,
security adversary, declined tool, host honesty) against the real CLI.
Live-host verification remains out of scope and is **not** claimed.

## Known limitations

- No automatic background monitoring: tool changes are found when
  discovery is re-run, not continuously.
- No network access anywhere in Step 01: recommendation metadata is a
  static registry; version/maintenance status is not fetched.
- MCP/skill *configuration* detection covers documented project-local and
  user-level config paths only; it never scans the whole machine.
- The interview engine is deterministic; free-form answers are stored
  verbatim and interpreted only as text. A cooperating host may propose
  interpretations, but nothing becomes an operator decision without the
  operator's recorded answer.
- Interruption-safety is best-effort at the process level (atomic
  publication + CAS), matching Aperio's documented durability model; no
  fsync/power-loss guarantee is claimed.
- Live AI-host integration is unverified; the terminal CLI is the only
  runtime-enforced path.
