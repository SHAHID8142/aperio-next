# Installing Aperio on a fresh machine

Aperio is a stdlib-only Python package (no runtime dependencies), so a
fresh-machine install is: Python 3.11+, a virtual environment, and the
wheel. Nothing else is downloaded, no services are contacted, and no
global configuration is changed.

**Verified path** (evidence: clean-venv install of the built wheel,
Python 3.14, macOS arm64): wheel install → `aperio --version` →
`aperio host-doctor` → `aperio init` → `aperio status` all work, and
the wheel ships the builtin skill packs and benchmark cases.

## 1. Prerequisites

- Python **3.11 or newer** (classifiers cover 3.11–3.14). Check:
  - macOS/Linux: `python3 --version`
  - Windows: `py --version`
- `pip` (bundled with Python). No compiler, no database, no network
  account.

## 2. Create an isolated environment (recommended)

macOS / Linux:

```sh
python3 -m venv ~/.aperio-venv
~/.aperio-venv/bin/python -m pip install aperio_next-<version>-py3-none-any.whl
~/.aperio-venv/bin/aperio --version
```

Windows (PowerShell):

```powershell
py -m venv $env:USERPROFILE\.aperio-venv
& $env:USERPROFILE\.aperio-venv\Scripts\python -m pip install aperio_next-<version>-py3-none-any.whl
& $env:USERPROFILE\.aperio-venv\Scripts\aperio --version
```

A venv keeps the `aperio` command out of your system Python and avoids
collisions with unrelated tools on PATH.

## 3. Getting the wheel

- **From a release archive**: download `aperio_next-<version>-py3-none-any.whl`
  and install it as above. (Not on PyPI yet — see `docs/limitations.md`;
  there is no public release artifact at the time of this document.)
- **From a source checkout**:

  ```sh
  python -m pip wheel . --no-deps -w dist/
  ```

  then install `dist/aperio_next-<version>-py3-none-any.whl`. For
  development instead: `python -m pip install -e ".[dev]"` (adds pytest).

## 4. First checks on a new machine

```sh
aperio --version        # prints the version
aperio host-doctor      # which agent hosts are documented + their honest level
aperio host-doctor --probe   # passive executable/flags check for provider CLIs
```

`host-doctor` detects nothing on its own beyond what the documented
catalog says — it never installs an agent host and never scans your
machine beyond the optional passive probe of the two documented
provider CLIs. See `docs/host-integration.md` for the level meanings.

## 5. Upgrading

Install the newer wheel over the old one in the same environment:

```sh
~/.aperio-venv/bin/python -m pip install aperio_next-<newer>-py3-none-any.whl
```

Project state (`.aperio/`) is not touched by an upgrade; `aperio
upgrade <project>` previews and applies only explicitly registered
schema migrations, with backups. Older plan-schema documents remain
readable without migration.

## 6. Uninstall / cleanup

```sh
~/.aperio-venv/bin/python -m pip uninstall aperio-next
rm -rf ~/.aperio-venv        # if you no longer want the environment
```

Project directories keep their `.aperio/` state — removing it is your
decision (it contains your briefs, decisions, plans, and history).

## Platform honesty

- **Tested**: macOS arm64, Python 3.14 (clean venv, wheel) and the
  repository's full test suite on Python 3.14. The package is
  stdlib-only, so other platforms are *expected* to work — expected,
  not verified.
- **Not verified**: Windows, Linux, Python 3.11/3.13 runtimes. Do not
  treat one platform's verification as another's.
- A stale unrelated `aperio` script earlier on PATH can shadow the real
  CLI; inside a venv the venv's `bin` (or `Scripts`) wins. Check
  `aperio --version` if in doubt.
