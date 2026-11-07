"""CLI usage errors, exit codes, and cross-file consistency."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import aperio
from aperio.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_subcommand_is_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_command_is_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2


def test_init_without_argument_is_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["init"])
    assert excinfo.value.code == 2


def test_status_without_argument_is_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["status"])
    assert excinfo.value.code == 2


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "init" in out
    assert "status" in out


def test_python_dash_m_help():
    result = subprocess.run(
        [sys.executable, "-m", "aperio", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "init" in result.stdout
    assert "status" in result.stdout


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == aperio.__version__


def test_package_requires_python_311_plus():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"] == ">=3.11"


def test_no_runtime_dependencies():
    """Feature 01 runs on the standard library only."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == []
