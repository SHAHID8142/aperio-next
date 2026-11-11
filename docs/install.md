# Installing, upgrading, and removing Aperio

Aperio is a **stdlib-only Python package** (no runtime dependencies). A fresh
install needs only Node 18+ and Python 3.11+ (for the one-line installer) or
just Python 3.11+ with a package manager that keeps it isolated.
No services are contacted, and no global configuration is changed.

The recommended method is the **npm launcher**: one command installs the
engine AND registers the `aperio-next` skill in every supported coding agent
it finds on your machine.

## 1. Prerequisites

- Node **18 or newer** (`node --version`) — only for the npm launcher.
- Python **3.11 or newer**. Check with `python3 --version` (macOS/Linux) or
  `py --version` (Windows). The full test suite passes on 3.11, 3.12, 3.13 and
  3.14 (macOS arm64 in this release).
- For the Python-only alternative: one of
  [`uv`](https://docs.astral.sh/uv/getting-started/installation/) or
  [`pipx`](https://pipx.pypa.io/latest/installation/).

## 2. Install (recommended: one command)

> The npm package is **not published yet** — this is the verified local
> distribution path. When published, `npx aperio-next install` will be the
> whole story.

```sh
git clone https://github.com/SHAHID8142/aperio-next.git
cd aperio-next
./scripts/build-npm-package.sh          # builds the wheel + packs the tgz
npx ./dist/aperio-next-<version>.tgz install
```

The installer:

1. Creates an isolated engine environment in `~/.aperio-next/` (no network
   needed at this step: the wheel is bundled and installed with
   `pip --no-index --no-deps`).
2. Installs and verifies the `aperio` CLI, and puts a shim in
   `~/.aperio-next/bin`.
3. Registers managed skill assets **only** in the documented skills/commands
   locations of detected hosts (see
   [integrations.md](integrations.md) for the exact paths and rules).
4. Prints only verified facts; undetected hosts are listed as pending.

```sh
aperio-next doctor        # engine, runtime, and per-host integration health
aperio-next update        # refresh engine + skill assets
aperio-next uninstall     # remove skills + engine (project data is kept)
```

If `aperio` is not found in a new terminal, add the shim directory to PATH:
`export PATH="$HOME/.aperio-next/bin:$PATH"`.

## 3. Install (alternative: uv)

Pin to a commit or tag so the install is reproducible. Replace `<REF>` with a
commit SHA or release tag:

```sh
uv tool install "git+https://github.com/SHAHID8142/aperio-next.git@<REF>"
aperio --version
```

If `aperio` is not found afterwards, run `uv tool update-shell` or open a new
terminal. To install a locally built wheel instead:

```sh
uv tool install ./dist/aperio_next-<version>-py3-none-any.whl
```

## 4. Install (alternative: pipx)

```sh
pipx install "git+https://github.com/SHAHID8142/aperio-next.git@<REF>"
aperio --version
```

With uv/pipx installs, register agent skills afterwards with the engine
directly: `aperio integrations install`.

## 5. Windows notes

Both `uv` and `pipx` support Windows. The executable lands in
`%USERPROFILE%\.local\bin` (uv) or the pipx apps directory; both tools offer a
command to add that directory to `PATH` (`uv tool update-shell`, or
`pipx ensurepath`). Reopen the terminal afterwards. The npm launcher has
Windows-aware paths (`venv\Scripts`, a `.cmd` engine shim, `py -3`
discovery), but they were **not executed** in this release's verification.

> **Honesty:** this release was verified on **macOS arm64 only**. The code
> avoids POSIX-only calls on default paths and process termination is
> platform-guarded, so Windows and Linux are *expected* to work — but they were
> **not executed** in this audit. Treat them as UNVERIFIED.

## 6. First checks on a new machine

```sh
aperio --version          # prints the version
aperio --help             # lists every command
aperio host-doctor        # honest per-host compatibility matrix
aperio integrations       # per-host skill registration status
aperio next .             # read-only: the one next action for this folder
```

`host-doctor` never installs an agent and never scans beyond the optional
passive probe of documented provider CLIs. See
[docs/host-integration.md](host-integration.md) for what each level means.

## 7. Upgrade

```sh
aperio-next update        # npm installs: engine + skill assets
uv tool upgrade aperio-next
# or reinstall a specific ref:
uv tool install --force "git+https://github.com/SHAHID8142/aperio-next.git@<NEW-REF>"
```

Project state is never touched by an upgrade. Inside a project,
`aperio upgrade <project>` previews and applies only explicitly registered
schema migrations, with backups.

## 8. Uninstall

```sh
aperio-next uninstall            # npm installs
uv tool uninstall aperio-next    # or: pipx uninstall aperio-next
```

Managed skill assets are removed from every host; foreign files are never
touched. Your projects' `.aperio/` folders are **not** removed. Delete one
yourself only if you truly want to discard that project's briefs, decisions,
plans, and history.

## Troubleshooting a broken install

- **`aperio: command not found`** — for npm installs add
  `~/.aperio-next/bin` to PATH; for uv/pipx run `uv tool update-shell` (or
  `pipx ensurepath`) and reopen the terminal. `uv tool list` shows what is
  installed.
- **A different `aperio` runs** — an older script earlier on `PATH` is
  shadowing the new one. `which -a aperio` (macOS/Linux) or `where aperio`
  (Windows) shows the order; remove the stale entry.
- **`conflict: foreign file` from the doctor** — a file aperio did not
  create occupies the `aperio-next` skill slot; aperio never overwrites it.
  Rename or remove it yourself, then re-run the install.
- **Install fails while building** — confirm Python ≥ 3.11 and that Git is
  available (the `git+https://` form needs Git). The npm launcher path never
  builds from source: it installs a bundled wheel offline.

