#!/usr/bin/env python3
"""Assemble the Analyst cards of the handoff experiment from one shared body.

Every cell of that experiment is a claim that two cards differ in one named
place and nowhere else. While each card carried its own copy of the shared
body, that property was re-checked after the fact rather than guaranteed, and
it had already broken once -- the body split into two generations. Here there
is one copy, in ``configs/handoff-experiment/shared.md``, with two slots:

* ``{{CAPABILITIES}}`` -- the paragraph pair under ``## What you can do``,
  which moves only when the Analyst's tool bundle moves;
* ``{{BLOCK}}`` -- the closing section, which is what most cells vary.

Run this module to render every registered card. It refuses to write a card
whose content would change unless ``--write`` is given, so a drifting card is
reported rather than silently overwritten.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CARDS_DIR = Path(__file__).resolve().parents[1] / "configs" / "handoff-experiment"
CAPABILITIES_SLOT = "{{CAPABILITIES}}\n"
BLOCK_SLOT = "{{BLOCK}}\n"
#: Every card ends on this. It is an outcome-side rule -- what counts as a
#: verified change -- so no cell may vary it, which is why it lives in the
#: shared body rather than in any block.
CLOSING_LINE = "Do not report a change as verified unless you have the evidence for it.\n"
#: The Analyst bundle a cell gets when it declares no ``tools`` override: the
#: single agent's working tools plus the collaboration channel.
DEFAULT_TOOLS = [
    "apply_patch", "bash", "file_read", "file_write", "grep",
    "message_agent", "run_tests", "submit", "team_status",
]


@dataclass(frozen=True)
class Variant:
    """One cell: which block it carries, and what bundle it is seated with."""

    name: str
    team_file: str
    block: str
    capabilities: str
    note: str
    tools: tuple[str, ...]

    @property
    def card_name(self) -> str:
        return f"analyst.{self.name}.md"

    @property
    def card_path(self) -> Path:
        return CARDS_DIR / self.card_name


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shared_body() -> str:
    """The one copy of everything no cell may vary."""
    body = _read(CARDS_DIR / "shared.md")
    if body.count(CAPABILITIES_SLOT) != 1 or body.count(BLOCK_SLOT) != 1:
        raise ValueError("shared.md must contain each slot exactly once")
    return body


def load_registry() -> tuple[dict[str, Variant], dict[str, tuple[str, ...]]]:
    """The registered cells and the ladders declared over them."""
    raw = yaml.safe_load(_read(CARDS_DIR / "variants.yaml"))
    variants: dict[str, Variant] = {}
    for name, entry in raw["variants"].items():
        variants[name] = Variant(
            name=name,
            team_file=entry["team_file"],
            block=entry["block"],
            capabilities=entry.get("capabilities", "full"),
            note=entry["note"],
            tools=tuple(entry.get("tools", DEFAULT_TOOLS)),
        )
    ladders = {name: tuple(rungs) for name, rungs in raw.get("ladders", {}).items()}
    return variants, ladders


def render(variant: Variant) -> str:
    """The card this cell's declaration assembles to."""
    capabilities = _read(CARDS_DIR / "capabilities" / f"{variant.capabilities}.md")
    block = _read(CARDS_DIR / "blocks" / f"{variant.block}.md")
    return shared_body().replace(CAPABILITIES_SLOT, capabilities).replace(BLOCK_SLOT, block)


def main(argv: list[str] | None = None) -> int:
    write = "--write" in (argv if argv is not None else sys.argv[1:])
    variants, _ = load_registry()
    drifted = 0
    for name in sorted(variants):
        variant = variants[name]
        built = render(variant)
        current = _read(variant.card_path) if variant.card_path.exists() else None
        if current == built:
            continue
        drifted += 1
        verb = "rewrote" if write else "DRIFTED"
        print(f"{verb}: {variant.card_name}")
        if write:
            variant.card_path.write_text(built, encoding="utf-8")
    if drifted and not write:
        print(f"\n{drifted} card(s) differ from their assembly. Re-run with --write to update.")
        return 1
    print(f"{len(variants)} card(s) checked, {drifted} rewritten." if write
          else f"{len(variants)} card(s) match their assembly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
