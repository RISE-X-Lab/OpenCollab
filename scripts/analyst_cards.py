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

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"
CARDS_DIR = CONFIGS_DIR / "handoff-experiment"
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
    "message_agent", "submit", "team_status",
]


@dataclass(frozen=True)
class CardSet:
    """One instrument's cards: where they live and which seat carries them.

    A card set is not a level of anything -- it is a different team. The
    handoff ladder seats an Analyst, a Coder and a Tester over a complete
    graph; the dual-candidate family seats an Adopter and two Coders over a
    partial one. Two cards from different sets share the closing evidence rule
    and nothing else, so each set has its own body, its own registry and its
    own test module, and no test in one set's module reads the other's files.
    """

    name: str
    directory: Path
    #: The seat the assembled card is written for. Also the card's filename
    #: stem, because a card that says "You are the Adopter" filed as
    #: ``analyst.<cell>.md`` is a card nobody can find by the name it uses.
    entry_role: str
    body: str = "shared.md"
    #: Slots this set's body carries besides ``{{CAPABILITIES}}`` and
    #: ``{{BLOCK}}``, each filled from ``<slot in lower case>/<value>.md``
    #: by the value a cell declares under ``slots:``. Every cell of the set
    #: declares every one, so a card never renders with a slot left in it.
    extra_slots: tuple[str, ...] = ()


HANDOFF_CARDS = CardSet(name="handoff", directory=CARDS_DIR, entry_role="analyst")
DUAL_CARDS = CardSet(
    name="dual-candidate",
    directory=CONFIGS_DIR / "dual-candidate",
    entry_role="adopter",
)
#: The same roster and the same five blocks as ``DUAL_CARDS``, seated as the
#: evaluated single agent: every role declares ``profile: single2``, so the
#: card is appended to that profile's system prompt instead of being the whole
#: of one. Its own body therefore states only what the profile does not -- the
#: three team tools, what ends a run here, and where each seat works.
S2DUAL_CARDS = CardSet(
    name="s2dual",
    directory=CONFIGS_DIR / "s2dual",
    entry_role="adopter",
)
#: The s2dual roster with the Adopter's tools changed, which makes it a
#: different team: a cell here differs from ``s2dual-judge`` in the tools the
#: Adopter holds and in the three places its card says so -- the tools line,
#: the capabilities pair, and how a Coder's commit is brought over. Those are
#: this set's slots; everything else is ``s2dual``'s body byte for byte.
S2TOOLS_CARDS = CardSet(
    name="s2tools",
    directory=CONFIGS_DIR / "s2tools",
    entry_role="adopter",
    extra_slots=("TOOLS", "ADOPTION"),
)
CARD_SETS: dict[str, CardSet] = {
    s.name: s for s in (HANDOFF_CARDS, DUAL_CARDS, S2DUAL_CARDS, S2TOOLS_CARDS)
}


def slot_marker(slot: str) -> str:
    """The line a slot occupies in a body, e.g. ``{{TOOLS}}``."""
    return "{{" + slot + "}}\n"


@dataclass(frozen=True)
class Variant:
    """One cell: which block it carries, and what bundle it is seated with."""

    name: str
    team_file: str
    block: str
    capabilities: str
    note: str
    tools: tuple[str, ...]
    card_set: CardSet = HANDOFF_CARDS
    #: ``(slot, value)`` for each of the card set's ``extra_slots``.
    slots: tuple[tuple[str, str], ...] = ()

    @property
    def card_name(self) -> str:
        return f"{self.card_set.entry_role}.{self.name}.md"

    @property
    def card_path(self) -> Path:
        return self.card_set.directory / self.card_name


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shared_body(card_set: CardSet = HANDOFF_CARDS) -> str:
    """The one copy of everything no cell in this set may vary."""
    body = _read(card_set.directory / card_set.body)
    markers = [CAPABILITIES_SLOT, BLOCK_SLOT, *map(slot_marker, card_set.extra_slots)]
    if any(body.count(marker) != 1 for marker in markers):
        raise ValueError("shared.md must contain each slot exactly once")
    return body


def load_registry(
    card_set: CardSet = HANDOFF_CARDS,
) -> tuple[dict[str, Variant], dict[str, tuple[str, ...]]]:
    """The registered cells of one card set, and the ladders declared over them."""
    raw = yaml.safe_load(_read(card_set.directory / "variants.yaml"))
    variants: dict[str, Variant] = {}
    for name, entry in raw["variants"].items():
        declared = {str(k).upper(): str(v) for k, v in (entry.get("slots") or {}).items()}
        if set(declared) != set(card_set.extra_slots):
            raise ValueError(
                f"{card_set.name}/{name}: slots {sorted(declared)} do not fill "
                f"{sorted(card_set.extra_slots)}"
            )
        variants[name] = Variant(
            name=name,
            team_file=entry["team_file"],
            block=entry["block"],
            capabilities=entry.get("capabilities", "full"),
            note=entry["note"],
            tools=tuple(entry.get("tools", DEFAULT_TOOLS)),
            card_set=card_set,
            slots=tuple((slot, declared[slot]) for slot in card_set.extra_slots),
        )
    ladders = {name: tuple(rungs) for name, rungs in raw.get("ladders", {}).items()}
    return variants, ladders


def render(variant: Variant) -> str:
    """The card this cell's declaration assembles to."""
    directory = variant.card_set.directory
    capabilities = _read(directory / "capabilities" / f"{variant.capabilities}.md")
    block = _read(directory / "blocks" / f"{variant.block}.md")
    card = shared_body(variant.card_set)
    for slot, value in variant.slots:
        card = card.replace(slot_marker(slot), _read(directory / slot.lower() / f"{value}.md"))
    return card.replace(CAPABILITIES_SLOT, capabilities).replace(BLOCK_SLOT, block)


def main(argv: list[str] | None = None) -> int:
    write = "--write" in (argv if argv is not None else sys.argv[1:])
    variants: dict[str, Variant] = {}
    for card_set in CARD_SETS.values():
        registered, _ = load_registry(card_set)
        for name, variant in registered.items():
            key = f"{card_set.name}/{name}"
            if key in variants:
                raise ValueError(f"two card sets register {key!r}")
            variants[key] = variant
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
