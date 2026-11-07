"""Shared fixtures for the Aperio test suite."""

from __future__ import annotations

import pytest

from aperio.cli import main


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
