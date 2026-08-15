"""Phase 0 checks: the repository foundation is present and coherent.

These are deliberately structural. Phase 0 ships no product behavior, but the promises
made in the foundation documents are worth holding to mechanically — especially the
scope boundaries, which are the thing most likely to erode quietly over time.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import calon

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "README.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    ".gitignore",
    ".editorconfig",
    ".env.example",
    "pyproject.toml",
    "Makefile",
    "config/calon.example.toml",
]


def test_required_foundation_files_exist():
    missing = [name for name in REQUIRED_FILES if not (REPO_ROOT / name).is_file()]
    assert not missing, f"missing foundation files: {missing}"


def test_version_is_pep440_and_exported():
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+|[ab]\d+|rc\d+)?", calon.__version__)


def test_license_is_agpl_and_unmodified():
    """The FSF text must be shipped verbatim; it forbids altering the license document."""
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    # Section 13 is the whole reason this license was chosen.
    assert "Remote Network Interaction" in text

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["license"] == "AGPL-3.0-or-later"


def test_changelog_follows_keep_a_changelog():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    for section in ("### Added", "### Changed", "### Fixed", "### Security"):
        assert section in text, f"changelog is missing the {section!r} section"


def test_claude_md_records_every_scope_boundary():
    """CLAUDE.md is the durable guard against scope creep; keep the boundaries in it."""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").lower()
    for boundary in ("crm", "workflow", "multi-tenancy", "openflow", "sync engine"):
        assert boundary in text, f"CLAUDE.md no longer mentions the {boundary!r} boundary"
    assert "standalone first" in text


def test_operator_config_example_is_valid_toml():
    tomllib.loads((REPO_ROOT / "config" / "calon.example.toml").read_text(encoding="utf-8"))


def test_no_real_config_or_database_is_tracked():
    """config/calon.toml may hold per-source secrets; the database is instance state."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("config/calon.toml", "calon.db", ".env"):
        assert pattern in gitignore
