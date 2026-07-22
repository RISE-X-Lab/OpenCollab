"""Static release gates for distribution metadata and package data."""

from __future__ import annotations

import hashlib
from pathlib import Path

import opencollab

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "opencollab"
# SPDX license-list-data v3.28.0, text/MulanPSL-2.0.txt.
_MULAN_PSL_2_SHA256 = "eb7a1d713eb919b146787629e22e4c975cb701f529a65d4d7e0fcd417558bf1c"


def test_release_metadata_keeps_versions_aligned_and_includes_license() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    license_bytes = (_REPO_ROOT / "LICENSE").read_bytes()

    assert opencollab.__version__
    assert f'version = "{opencollab.__version__}"' in pyproject
    assert 'license = "MulanPSL-2.0"' in pyproject
    assert 'license-files = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]' in pyproject
    assert hashlib.sha256(license_bytes).hexdigest() == _MULAN_PSL_2_SHA256
    assert not (_PACKAGE_ROOT / "LICENSE").exists()


def test_release_notices_preserve_historical_and_third_party_terms() -> None:
    notice = (_REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    normalized_notice = " ".join(notice.split())
    third_party = (_REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    js_yaml_license = (
        _REPO_ROOT / "skills/team-config/vendor/js-yaml.LICENSE"
    ).read_text(encoding="utf-8")

    assert "OpenCollab v0.1.0 tag contains a root LICENSE under the MIT License" in normalized_notice
    assert "Rights already granted for earlier revisions are not withdrawn" in normalized_notice
    assert "They do not determine or transfer ownership" in normalized_notice
    assert "js-yaml 4.1.1" in third_party
    assert js_yaml_license in third_party


def test_readme_advertises_the_distribution_license() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "License-MulanPSL--2.0-blue.svg" in readme
    assert "License: MulanPSL-2.0" in readme
    assert (
        "[Mulan Permissive Software License v2]"
        "(https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE)"
    ) in readme


def test_distribution_does_not_claim_package_wide_typing() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"Typing :: Typed"' not in pyproject
    assert not (_PACKAGE_ROOT / "py.typed").exists()
