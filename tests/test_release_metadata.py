"""Static release gates for distribution metadata and package data."""

from __future__ import annotations

from pathlib import Path

import opencollab

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "opencollab"
_COPYRIGHT = "Copyright (c) 2026 Yihong Dong, Zhenhua Xu, Kai Gong, and OpenCollab contributors"


def test_release_metadata_keeps_versions_aligned_and_includes_license() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    license_text = (_REPO_ROOT / "LICENSE").read_text(encoding="utf-8")

    assert opencollab.__version__
    assert f'version = "{opencollab.__version__}"' in pyproject
    assert 'license = "MulanPSL-2.0"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert license_text.startswith(f"{_COPYRIGHT}\n\n木兰宽松许可证， 第2版\n")
    assert "END OF THE TERMS AND CONDITIONS" in license_text
    assert not (_PACKAGE_ROOT / "LICENSE").exists()


def test_readme_advertises_the_distribution_license() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "License-MulanPSL--2.0-blue.svg" in readme
    assert "License: MulanPSL-2.0" in readme
    assert "[Mulan Permissive Software License v2](LICENSE)" in readme


def test_distribution_is_marked_as_typed() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"Typing :: Typed"' in pyproject
    assert (_PACKAGE_ROOT / "py.typed").read_text(encoding="utf-8").strip() == ""
