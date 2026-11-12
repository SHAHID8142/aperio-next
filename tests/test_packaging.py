"""Distribution-artifact tests: the wheel must ship every integration asset.

These tests guard the packaging contract that a source checkout can hide:
``pyproject.toml`` package-data globs decide what a wheel actually contains.
A file that exists on disk but is excluded by the globs (e.g. Gemini CLI's
``.toml`` command template) works in-tree and breaks in an installed wheel.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "src" / "aperio" / "integration_assets"


def _declared_patterns() -> list[str]:
    """Read the ``[tool.setuptools.package-data] aperio`` list from pyproject."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.11+ always has tomllib
        pytest.skip("tomllib unavailable")
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    return data["tool"]["setuptools"]["package-data"]["aperio"]


def _pattern_matches(name: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(name, pat.split("/", 1)[-1]) for pat in _declared_patterns())


def test_every_integration_asset_is_covered_by_package_data():
    """Every file on disk in integration_assets/ must match a declared glob.

    This is the cheap, always-run guard: it fails the moment someone adds an
    asset with an extension the package-data list does not cover.
    """
    files = sorted(p.name for p in ASSETS_DIR.iterdir() if p.is_file())
    assert files, "integration_assets/ is empty"
    uncovered = [f for f in files if not _pattern_matches(f)]
    assert not uncovered, (
        "integration_assets files are not packaged by pyproject.toml "
        f"[tool.setuptools.package-data]: {uncovered}. Add a matching glob."
    )


def test_gemini_toml_template_is_present_on_disk():
    assert (ASSETS_DIR / "aperio-next-command.toml").is_file()


def _build_wheel(out_dir: Path) -> Path | None:
    """Build a wheel with uv or `python -m build`; None if no builder exists."""
    if shutil.which("uv"):
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"uv build failed: {proc.stderr[-400:]}")
    else:
        try:
            import build  # noqa: F401
        except ModuleNotFoundError:
            pytest.skip("no wheel builder (uv / build) available")
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir),
             str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"build failed: {proc.stderr[-400:]}")
    wheels = sorted(out_dir.glob("*.whl"))
    return wheels[0] if wheels else None


def test_wheel_contains_all_integration_assets():
    """The built wheel must contain every asset file, including the .toml."""
    with tempfile.TemporaryDirectory() as tmp:
        wheel = _build_wheel(Path(tmp))
        if wheel is None:
            pytest.skip("no wheel produced")
        names = set(zipfile.ZipFile(wheel).namelist())
        expected = {
            f"aperio/integration_assets/{p.name}"
            for p in ASSETS_DIR.iterdir()
            if p.is_file()
        }
        missing = sorted(expected - names)
        assert not missing, f"wheel is missing integration assets: {missing}"


def test_installed_asset_loads_toml_template():
    """The Gemini TOML template must render from whatever is importable now.

    Under a source checkout this verifies resources.files() finds it; under an
    installed wheel (see the clean-room lifecycle test) it verifies packaging.
    """
    from importlib import resources

    text = (
        resources.files("aperio")
        .joinpath("integration_assets", "aperio-next-command.toml")
        .read_text(encoding="utf-8")
    )
    assert "description" in text
