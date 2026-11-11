"""Shared fixtures for the Aperio test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Some tests import sibling modules as ``tests.<name>`` (shared fixtures such
# as the fake provider CLI). That only resolves when the repository root is on
# sys.path — which is not guaranteed for every pytest invocation (for example
# ``uv run --python 3.12 pytest`` from a fresh environment). Add it here so the
# suite collects identically regardless of how it is started.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aperio.cli import main  # noqa: E402


@pytest.fixture
def project_dir(tmp_path):
    """A fresh, non-existent project directory path."""
    return tmp_path / "proj"


@pytest.fixture
def run_cli(capsys):
    """Invoke the CLI in-process; return (exit_code, stdout, stderr)."""

    def _run(*argv: str):
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run
