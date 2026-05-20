"""Boundary tests for the application and domain layers.

Locks the Clean Architecture dependency rule: neither layer may import
from ``opencollab.core``, ``opencollab.tools``, ``opencollab.bootstrap``,
``opencollab.cli``, ``opencollab.adapters``, or ``opencollab.team``.

A docstring or comment that mentions one of those names is fine — only
``import`` and ``from ... import`` statements are forbidden.
"""

from __future__ import annotations

import pathlib
import re


FORBIDDEN = re.compile(
    r"^\s*(?:from|import)\s+opencollab\.(?:core|tools|bootstrap|cli|adapters|team)\b",
    re.MULTILINE,
)

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "opencollab"


def _offenders(layer: str) -> list[str]:
    root = _PACKAGE_ROOT / layer
    return [
        str(p.relative_to(_PACKAGE_ROOT))
        for p in root.rglob("*.py")
        if FORBIDDEN.search(p.read_text(encoding="utf-8"))
    ]


def test_application_does_not_import_outer_layers():
    assert _offenders("application") == []


def test_domain_does_not_import_outer_layers():
    assert _offenders("domain") == []
