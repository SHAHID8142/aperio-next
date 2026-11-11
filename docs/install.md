# Installing, upgrading, and removing Aperio

Aperio is a **stdlib-only Python package** (no runtime dependencies). A fresh
install needs only Python 3.11+ and a package manager that keeps it isolated.
No services are contacted, and no global configuration is changed.

The recommended method is **`uv tool install`**: it gives Aperio its own
environment, puts a single `aperio` executable on your `PATH`, and never
touches your project's dependencies.

## 1. Prerequisites

- Python **3.11 or newer**. Check with `python3 --version` (macOS/Linux) or
  `py --version` (Windows). The full test suite passes on 3.11, 3.12, 3.13 and
  3.14 (macOS arm64 in this release).
- One of: [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (recommended)
  or [`pipx`](https://pipx.pypa.io/latest/installation/).

## 2. Install (recommended: uv)

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

## 3. Install (alternative: pipx)

```sh
pipx install "git+https://github.com/SHAHID8142/aperio-next.git@<REF>"
aperio --version
```

## 4. Windows notes

Both `uv` and `pipx` support Windows. The executable lands in
`%USERPROFILE%\.local\bin` (uv) or the pipx apps directory; both tools offer a
command to add that directory to `PATH` (`uv tool update-shell`, or
`pipx ensurepath`). Reopen the terminal afterwards.

> **Honesty:** this release was verified on **macOS arm64 only**. The code
> avoids POSIX-only calls on default paths and process termination is
> platform-guarded, so Windows and Linux are *expected* to work — but they were
> **not executed** in this audit. Treat them as UNVERIFIED.

## 5. First checks on a new machine

```sh
aperio --version          # prints the version
aperio --help             # lists every command
aperio host-doctor        # honest per-host compatibility matrix
aperio next .             # read-only: the one next action for this folder
```

`host-doctor` never installs an agent and never scans beyond the optional
passive probe of documented provider CLIs. See
[docs/host-integration.md](host-integration.md) for what each level means.

## 6. Upgrade

```sh
uv tool upgrade aperio-next
# or reinstall a specific ref:
uv tool install --force "git+https://github.com/SHAHID8142/aperio-next.git@<NEW-REF>"
```

Project state is never touched by an upgrade. Inside a project,
`aperio upgrade <project>` previews and applies only explicitly registered
schema migrations, with backups.

## 7. Uninstall

```sh
uv tool uninstall aperio-next     # or: pipx uninstall aperio-next
```

Your project's `.aperio/` folder is **not** removed. Delete it yourself only if
you truly want to discard that project's briefs, decisions, plans, and history.

## Troubleshooting a broken install

- **`aperio: command not found`** — the tool bin directory is not on `PATH`.
  Run `uv tool update-shell` (or `pipx ensurepath`) and reopen the terminal.
  `uv tool list` shows what is installed.
- **A different `aperio` runs** — an older script earlier on `PATH` is
  shadowing the new one. `which -a aperio` (macOS/Linux) or `where aperio`
  (Windows) shows the order; remove the stale entry.
- **Install fails while building** — confirm Python ≥ 3.11 and that Git is
  available (the `git+https://` form needs Git).
