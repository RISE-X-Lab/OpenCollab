"""The `.importlinter` contracts must keep tracking the project they guard.

``domain-stdlib-only`` is spelled as a blacklist: it names every third-party
package the domain may not import. That is equivalent to "the standard library
only" exactly as long as the list stays complete, and nothing keeps it complete
on its own — a ninth entry in ``[project.dependencies]`` would simply not be
covered, silently. This test is what makes the two move together.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Distributions whose import name is not just the distribution name with dashes
# turned into underscores.
_IMPORT_NAME = {"pyyaml": "yaml"}


def _import_name(requirement: str) -> str:
    distribution = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0].lower()
    return _IMPORT_NAME.get(distribution, distribution.replace("-", "_"))


def _runtime_dependencies() -> set[str]:
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.MULTILINE | re.DOTALL)
    assert block is not None, "pyproject.toml has no runtime dependencies block"
    return {_import_name(m) for m in re.findall(r'"([^"]+)"', block.group(1))}


def _contract(name: str) -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    parser.read(_REPO_ROOT / ".importlinter", encoding="utf-8")
    section = f"importlinter:contract:{name}"
    assert parser.has_section(section), f"{section} is missing from .importlinter"
    return parser[section]


def _values(section: configparser.SectionProxy, key: str) -> set[str]:
    return {line.strip() for line in section[key].splitlines() if line.strip()}


def test_domain_blacklist_covers_every_runtime_dependency():
    forbidden = _values(_contract("domain-stdlib-only"), "forbidden_modules")
    missing = _runtime_dependencies() - forbidden
    assert not missing, (
        "these runtime dependencies are not forbidden to the domain layer: "
        f"{sorted(missing)}. Add them to `forbidden_modules` in `.importlinter`, "
        "or the 'stdlib only' contract silently stops meaning that."
    )


def test_domain_blacklist_names_nothing_that_is_not_a_dependency():
    forbidden = _values(_contract("domain-stdlib-only"), "forbidden_modules")
    stale = forbidden - _runtime_dependencies()
    assert not stale, (
        f"`forbidden_modules` still lists {sorted(stale)}, which is no longer a "
        "runtime dependency. Drop it so the contract stays readable."
    )
