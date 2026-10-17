"""Structural case coverage of the inputs a reconstruction has observed.

Modelled on rea's obligation ledger: an obligation closes only when the
fixtures cover the required case kinds. The kinds here are semantics-free
structural properties of JSON-like inputs (length buckets, zero, negatives,
repeats, and the analogous properties for numbers, pairs, strings and point
sets), so they can be demanded from the agent without hinting at the rule.
"""

from __future__ import annotations

from typing import Any

KINDS = ("list", "two_lists", "int", "pair", "string", "points", "other")

DESCRIPTIONS = {
    "len:0": "an empty list",
    "len:short": "a list of 1-3 elements",
    "len:mid": "a list of 4-7 elements",
    "len:long": "a list of 8 or more elements",
    "val:zero": "a list containing 0",
    "val:neg": "a list containing negative numbers",
    "val:repeat": "a list with repeated values",
    "val:allsame": "a list whose elements are all equal",
    "int:zero": "the number 0",
    "int:small": "a number from 1 to 9",
    "int:mid": "a number from 10 to 99",
    "int:large": "a number of 100 or more",
    "int:neg": "a negative number",
    "pair:equal": "a pair of equal numbers",
    "pair:a<b": "a pair with the first number smaller",
    "pair:a>b": "a pair with the first number larger",
    "pair:large": "a pair with a number of 20 or more",
    "pair:one": "a pair containing 1",
    "str:short": "a string of 1-3 characters",
    "str:mid": "a string of 4-7 characters",
    "str:long": "a string of 8 or more characters",
    "str:repeat": "a string with repeated characters",
    "pts:few": "1-2 points",
    "pts:some": "3-5 points",
    "pts:many": "6 or more points",
    "pts:dup": "a point set with a repeated point",
    "pts:neg": "points with negative coordinates",
    "pts:zero": "points with a zero coordinate",
}


def _list_categories(xs: list[Any]) -> set[str]:
    n = len(xs)
    cats: set[str] = set()
    cats.add("len:0" if n == 0 else "len:short" if n <= 3 else "len:mid" if n <= 7 else "len:long")
    nums = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if any(x == 0 for x in nums):
        cats.add("val:zero")
    if any(x < 0 for x in nums):
        cats.add("val:neg")
    try:
        if n > 1 and len({repr(x) for x in xs}) < n:
            cats.add("val:repeat")
        if n > 1 and len({repr(x) for x in xs}) == 1:
            cats.add("val:allsame")
    except TypeError:
        pass
    return cats


def categories(inp: Any, kind: str) -> set[str]:
    """Structural categories one input exhibits."""
    try:
        if kind == "list" and isinstance(inp, list):
            return _list_categories(inp)
        if kind == "two_lists" and isinstance(inp, list) and len(inp) == 2 and all(isinstance(v, list) for v in inp):
            return _list_categories(inp[0]) | _list_categories(inp[1])
        if kind == "int" and isinstance(inp, (int, float)) and not isinstance(inp, bool):
            if inp == 0:
                return {"int:zero"}
            if inp < 0:
                return {"int:neg"}
            if inp <= 9:
                return {"int:small"}
            return {"int:mid"} if inp <= 99 else {"int:large"}
        if kind == "pair" and isinstance(inp, (list, tuple)) and len(inp) == 2:
            a, b = inp
            cats = {"pair:equal" if a == b else "pair:a<b" if a < b else "pair:a>b"}
            if max(a, b) >= 20:
                cats.add("pair:large")
            if 1 in (a, b):
                cats.add("pair:one")
            return cats
        if kind == "string" and isinstance(inp, str):
            n = len(inp)
            cats = {"str:short" if n <= 3 else "str:mid" if n <= 7 else "str:long"}
            if n > 1 and len(set(inp)) < n:
                cats.add("str:repeat")
            return cats
        if kind == "points" and isinstance(inp, list):
            n = len(inp)
            cats = {"pts:few" if n <= 2 else "pts:some" if n <= 5 else "pts:many"}
            flat = [c for p in inp if isinstance(p, (list, tuple)) for c in p if isinstance(c, (int, float))]
            if n > 1 and len({repr(p) for p in inp}) < n:
                cats.add("pts:dup")
            if any(c < 0 for c in flat):
                cats.add("pts:neg")
            if any(c == 0 for c in flat):
                cats.add("pts:zero")
            return cats
    except Exception:  # noqa: BLE001 - an odd input simply exhibits no category
        pass
    return set()


def required(kind: str, lo: float | None, hi: float | None, is_float: bool = False) -> set[str]:
    """Categories the observed inputs must cover before a promotion counts."""
    lo = -1 if lo is None else lo
    hi = 10 if hi is None else hi
    if kind == "list":
        req = {"len:0", "len:short", "len:mid", "len:long"}
        if not is_float:
            req.add("val:repeat")
            if lo <= 0 <= hi:
                req.add("val:zero")
        if lo < 0:
            req.add("val:neg")
        return req
    if kind == "two_lists":
        req = {"len:short", "len:mid", "len:long", "val:repeat"}
        if lo <= 0 <= hi:
            req.add("val:zero")
        if lo < 0:
            req.add("val:neg")
        return req
    if kind == "int":
        req = {"int:small"}
        if lo <= 0:
            req.add("int:zero")
        if hi >= 10:
            req.add("int:mid")
        if hi >= 100:
            req.add("int:large")
        if lo < 0:
            req.add("int:neg")
        return req
    if kind == "pair":
        req = {"pair:equal", "pair:a<b", "pair:a>b"}
        if hi >= 20:
            req.add("pair:large")
        if lo <= 1:
            req.add("pair:one")
        return req
    if kind == "string":
        return {"str:short", "str:mid", "str:long", "str:repeat"}
    if kind == "points":
        return {"pts:few", "pts:some", "pts:many", "pts:dup"}
    return set()


def missing(inputs: list[Any], kind: str, lo: float | None, hi: float | None, is_float: bool = False) -> set[str]:
    """Required categories no observed input exhibits yet."""
    covered: set[str] = set()
    for inp in inputs:
        covered |= categories(inp, kind)
    return required(kind, lo, hi, is_float) - covered


def describe(cats: set[str]) -> str:
    return "; ".join(DESCRIPTIONS.get(c, c) for c in sorted(cats))
