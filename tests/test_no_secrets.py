"""No accidental secret storage.

Aperio must never persist credentials, tokens, or environment contents
into project state. Shareable context is template-only; the sole sanctioned
location for sensitive data is .aperio/local/, which is git-ignored and
explicitly non-portable.
"""

from __future__ import annotations

import json
import re

SECRET_PATTERNS = {
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "slack_token": re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}"),
    "private_key_block": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
    ),
    "generic_assignment": re.compile(
        r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+"
    ),
}

SHAREABLE_FILES = (
    ".aperio/state.json",
    ".aperio/brief.md",
    ".aperio/decisions.md",
    ".aperio/handoff.md",
    ".aperio/.gitignore",
    ".aperio/local/README.md",
)


def assert_no_secrets(text: str, where: str) -> None:
    for name, pattern in SECRET_PATTERNS.items():
        assert pattern.search(text) is None, f"possible {name} found in {where}"


def test_fresh_project_contains_no_secret_shaped_content(run_cli, project_dir):
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    for rel in SHAREABLE_FILES:
        assert_no_secrets((project_dir / rel).read_text(), rel)


def test_state_json_has_exactly_the_documented_keys(run_cli, project_dir):
    run_cli("init", str(project_dir))
    state = json.loads((project_dir / ".aperio/state.json").read_text())
    assert set(state.keys()) == {
        "schema_version",
        "name",
        "created_at",
        "updated_at",
        "phase",
        "aperio_version",
    }


def test_environment_variables_are_not_captured(run_cli, project_dir, monkeypatch):
    token = "ghp_" + "a" * 36
    monkeypatch.setenv("APERIO_TEST_TOKEN", token)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    code, _, err = run_cli("init", str(project_dir))
    assert code == 0, err
    for rel in SHAREABLE_FILES:
        content = (project_dir / rel).read_text()
        assert token not in content, f"environment token leaked into {rel}"


def test_local_dir_is_gitignored(run_cli, project_dir):
    run_cli("init", str(project_dir))
    gitignore = (project_dir / ".aperio/.gitignore").read_text()
    assert re.search(r"(?m)^local/?$", gitignore)


def test_sensitive_data_belongs_only_in_local_dir(run_cli, project_dir):
    """A deliberately placed fake token must stay inside .aperio/local/."""
    run_cli("init", str(project_dir))
    fake_token = "ghp_" + "b" * 36
    local_secret = project_dir / ".aperio/local/secret.txt"
    local_secret.write_text(fake_token)
    # Shareable files still contain nothing sensitive.
    for rel in SHAREABLE_FILES:
        assert fake_token not in (project_dir / rel).read_text()


def test_secret_patterns_actually_detect(sample_token="ghp_" + "c" * 36):
    """Meta-test: the scanner patterns would catch a real-looking token."""
    assert SECRET_PATTERNS["github_token"].search(sample_token)
    assert SECRET_PATTERNS["aws_access_key"].search("AKIA" + "B" * 16)
