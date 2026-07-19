"""Static release gates for distribution metadata and package data."""

from __future__ import annotations

from pathlib import Path

import opencollab

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_ROOT / "opencollab"
_PACKAGE_ROOT = _PROJECT_ROOT / "opencollab"


def test_release_metadata_keeps_versions_aligned_and_includes_license() -> None:
    pyproject = (_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert opencollab.__version__
    assert f'version = "{opencollab.__version__}"' in pyproject
    assert 'license = "MulanPSL-2.0"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert (_PROJECT_ROOT / "LICENSE").read_bytes() == (_REPO_ROOT / "LICENSE").read_bytes()


def test_distribution_is_marked_as_typed() -> None:
    pyproject = (_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"Typing :: Typed"' in pyproject
    assert (_PACKAGE_ROOT / "py.typed").read_text(encoding="utf-8").strip() == ""
