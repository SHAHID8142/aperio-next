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


def _builder_available() -> bool:
    if shutil.which("uv"):
        return True
    try:
        import build  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _build_wheel(out_dir: Path) -> Path:
    """Build a wheel; FAIL (not skip) if a builder exists but the build fails.

    Skipping on a build failure would let a broken package silently pass CI.
    Only a genuinely absent builder is a skip.
    """
    if shutil.which("uv"):
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            "uv wheel build failed (packaging is a release blocker):\n"
            f"{proc.stderr[-2000:]}"
        )
    else:
        try:
            import build  # noqa: F401
        except ModuleNotFoundError:
            pytest.skip("no wheel builder (uv / python-build) available")
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir),
             str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            "wheel build failed (packaging is a release blocker):\n"
            f"{proc.stderr[-2000:]}"
        )
    wheels = sorted(out_dir.glob("*.whl"))
    assert wheels, "builder reported success but produced no wheel"
    return wheels[0]


def test_wheel_contains_all_integration_assets():
    """The built wheel must contain every asset file, including the .toml."""
    with tempfile.TemporaryDirectory() as tmp:
        wheel = _build_wheel(Path(tmp))
        names = set(zipfile.ZipFile(wheel).namelist())
        expected = {
            f"aperio/integration_assets/{p.name}"
            for p in ASSETS_DIR.iterdir()
            if p.is_file()
        }
        missing = sorted(expected - names)
        assert not missing, f"wheel is missing integration assets: {missing}"


def test_built_wheel_resources_render_outside_checkout(tmp_path):
    wheel = _build_wheel(tmp_path / "dist")
    target = tmp_path / "installed"
    installer = ["uv", "pip", "install", "--python", sys.executable] if shutil.which("uv") else [sys.executable, "-m", "pip", "install"]
    installed = subprocess.run(
        installer + ["--no-index", "--no-deps", "--target", str(target), str(wheel)],
        capture_output=True, text=True, timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import aperio; from aperio.integrations import host_by_id, render_command_body; "
        "assert aperio.__file__.startswith(sys.argv[1]); "
        "body = render_command_body(host_by_id('gemini-cli'), engine_path='/fixture/engine', version='fixture'); "
        "import tomllib; tomllib.loads(body); print('installed Gemini TOML rendered')"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script, str(target)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "installed Gemini TOML rendered" in result.stdout


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
