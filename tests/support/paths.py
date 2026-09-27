"""Locations shared by source-tree and installed-package tests."""

from pathlib import Path

import opencollab

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(opencollab.__file__).resolve().parent.parent
