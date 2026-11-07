"""CLI version reporting."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import aperio
from aperio.cli import main

EXPECTED = f"aperio {aperio.__version__}"


def test_version_via_main_prints_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == EXPECTED


def test_version_via_python_dash_m():
    result = subprocess.run(
        [sys.executable, "-m", "aperio", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == EXPECTED


def test_version_via_console_script():
    """Run the installed console script if it exists in this environment."""
    if os.name == "nt":
        script = Path(sys.prefix) / "Scripts" / "aperio.exe"
    else:
        script = Path(sys.prefix) / "bin" / "aperio"
    if not script.exists():
        pytest.skip("console script not installed in this environment")
    result = subprocess.run(
        [str(script), "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == EXPECTED
