"""Experiment 3: the reference cell with the budget stated, at three budgets.

Each ``s2dual-judgeinfo-<X>`` cell is ``s2dual-judge`` with two sentences added
to the end of the capabilities pair: the run's budget, the same for every seat,
and what a single agent working alone used on these tasks. Two claims make the
cells comparable, and both are checked on the text that is seated: a cell
differs from ``s2dual-judge`` in those two sentences and nowhere else, and the
cells differ from each other in the budget number and nowhere else -- the
single-agent figure is one measurement, stated identically in all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.analyst_cards import S2DUAL_CARDS, load_registry

PROMPTS = Path(__file__).resolve().parents[1] / "configs" / "s2dual"
VARIANTS, _ = load_registry(S2DUAL_CARDS)
INFORMED = sorted(n for n in VARIANTS if n.startswith("s2dual-judgeinfo-"))
TOKENS = {"05m": 500_000, "1m": 1_000_000, "2m": 2_000_000, "4m": 4_000_000}
ADDED = re.compile(
    r"Your budget for this run is (?P<x>[\d,]+) tokens, and each Coder's is the same\. "
    r"On these tasks, a single agent working alone used (?P<mean>[\d,]+) tokens on "
    r"average \(median (?P<median>[\d,]+)\)\.$"
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _capabilities(name: str) -> str:
    return (PROMPTS / "capabilities" / f"{VARIANTS[name].capabilities}.md").read_text(encoding="utf-8")


def test_the_three_budgets_are_registered() -> None:
    assert INFORMED == ["s2dual-judgeinfo-1m", "s2dual-judgeinfo-2m", "s2dual-judgeinfo-4m"]


@pytest.mark.parametrize("name", INFORMED)
def test_a_budget_cell_is_the_reference_cell_plus_two_sentences(name: str) -> None:
    variant = VARIANTS[name]
    assert variant.block == "judge"
    assert variant.tools == VARIANTS["s2dual-judge"].tools
    full = _flat((PROMPTS / "capabilities" / "full.md").read_text(encoding="utf-8"))
    ours = _flat(_capabilities(name))
    assert ours.startswith(full + " ")
    added = ADDED.match(ours[len(full) + 1:])
    assert added is not None
    assert int(added["x"].replace(",", "")) == TOKENS[name.rsplit("-", 1)[1]]


def test_the_budget_cells_differ_from_each_other_only_in_the_budget() -> None:
    masked = {
        _flat(re.sub(r"this run is [\d,]+ tokens", "this run is X tokens", _capabilities(n)))
        for n in INFORMED
    }
    assert len(masked) == 1
