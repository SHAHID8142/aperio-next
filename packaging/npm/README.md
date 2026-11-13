# aperio-next

**Native agent skills and durable project orchestration.**

Aperio Next keeps a project's brief, decisions, plan, tasks, checkpoints,
and handoffs inside the project's `.aperio/` folder so cooperating agent
sessions can recover context. Host-specific verification is documented in
the repository's release-candidate evidence.

This npm package is the installer: it bootstraps the local Python engine
(stdlib-only, no accounts, no services) and registers a native
`aperio-next` skill in every supported coding agent it finds on your
machine.

## Install

This release candidate is not published to npm. From a local packed artifact:

```sh
npx --yes --package ./aperio-next-1.2.0-rc.2.tgz aperio-next install
```

Requires Node 18+ and Python 3.11+ (no other dependencies). The installer:

1. Creates an isolated engine environment in `~/.aperio-next/`.
2. Installs the bundled `aperio` CLI engine and verifies it runs.
3. Registers skill assets **only** in the documented skills/commands
   locations of detected hosts (ZCode, Claude Code, OpenCode, Codex,
   Gemini CLI, Cursor). Existing files it did not create are never
   touched.
4. Prints exactly what was verified — hosts it could not detect are
   reported as pending, never as installed.

## Activate

| Host | Command |
| --- | --- |
| ZCode | `/aperio-next` or mention the skill |
| Claude Code | `/aperio-next` |
| OpenCode | `/aperio-next` or `@aperio-next` |
| Codex CLI | `$aperio-next` |
| Gemini CLI | `/aperio-next` |
| Cursor | `/aperio-next` |

The skill runs the real engine: `aperio next <project> --json` gives the
agent one machine-readable next action (read-only), the onboarding flow is
consent-gated, and plan approval, execution authorization, verification,
and recovery stay human-controlled and engine-enforced.

## Use in a project

Open your project in any supported agent and activate. In a new project
the agent walks you through onboarding (nothing is written until you
approve); in an Aperio-managed project it resumes from durable state.

## Health, updates, removal

```sh
npx --yes --package ./aperio-next-1.2.0-rc.2.tgz aperio-next doctor
npx --yes --package ./aperio-next-1.2.0-rc.2.tgz aperio-next update
npx --yes --package ./aperio-next-1.2.0-rc.2.tgz aperio-next uninstall
```

npx does not leave a persistent launcher command. Each invocation above
selects the same local artifact. Uninstall retains project `.aperio/` data.

## Honesty notes

- Installing a skill file is *registration*. Whether a given agent session
  activates it depends on the host — `aperio-next doctor` separates
  registered from activated, and the engine never claims runtime
  enforcement inside a third-party host.
- Public `npx aperio-next install` is a future interface, requiring
  publication and registry verification. It is not the verified command
  for this release candidate.
