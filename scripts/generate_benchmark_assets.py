#!/usr/bin/env python3
"""Render the public benchmark SVGs from the accepted summary data."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, Liberation Sans, sans-serif"
HERO_ORDER = ("duo", "base", "claude", "codex", "mini", "openhands")
HERO_LABELS = {"duo": "OpenCollab (Duo)", "base": "OpenCollab (Base)"}
PALETTES = {
    "light": {
        "background": "#ffffff",
        "text": "#1f2328",
        "muted": "#656d76",
        "grid": "#d8dee4",
        "highlight": "#f4f0ff",
        "duo": "#8b5cf6",
        "base": "#b6a2e8",
        "bar": "#c7cdd5",
        "accent": "#6d28d9",
    },
    "dark": {
        "background": "#0d1117",
        "text": "#e6edf3",
        "muted": "#9da7b3",
        "grid": "#30363d",
        "highlight": "#211a35",
        "duo": "#a78bfa",
        "base": "#7963bb",
        "bar": "#4b515a",
        "accent": "#c4b5fd",
    },
}


def _text(x: float, y: float, value: str, **attrs: str | int) -> str:
    attributes = " ".join(f'{key.replace("_", "-")}="{escape(str(v), quote=True)}"' for key, v in attrs.items())
    return f'<text x="{x:g}" y="{y:g}" {attributes}>{escape(value)}</text>'


def _start(width: int, height: int, title: str, description: str, colors: dict) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="description">{escape(description)}</desc>',
        f'<rect width="{width}" height="{height}" rx="12" fill="{colors["background"]}"/>',
        f'<g font-family="{FONT}" fill="{colors["text"]}" style="font-variant-numeric:tabular-nums">',
    ]


def _rate(result: dict, dataset: dict) -> float:
    return 100 * result["passed"] / dataset["tasks"]


def _description(data: dict) -> str:
    return " ".join(
        f"{dataset['name']}. " + "; ".join(f"{r['label']} {_rate(r, dataset):.2f}%" for r in dataset["results"]) + "."
        for dataset in data["datasets"]
    )


def render_hero(data: dict, theme: str) -> str:
    """Render the README's four aligned panels with the existing 40–100% scale."""
    c = PALETTES[theme]
    width, height = 1180, 478
    start_x, plot_width, panel_width = 214, 164, 236
    first_y, row_height = 164, 44
    bottom = first_y + len(HERO_ORDER) * row_height
    svg = _start(
        width,
        height,
        "Pass@1 on four agentic coding benchmarks",
        "Six harnesses use GPT-5.6-Luna at max reasoning effort. Each axis spans 40% to 100%. " + _description(data),
        c,
    )
    svg += [
        _text(28, 44, "Pass@1 on four agentic coding benchmarks", font_size=22, font_weight=600),
        _text(
            28,
            72,
            "GPT-5.6-Luna at max reasoning effort across all six harnesses. Higher is better. Axes span 40–100%.",
            font_size=15,
            fill=c["muted"],
        ),
        f'<rect x="16" y="{first_y}" width="{width - 32}" height="{row_height}" rx="8" fill="{c["highlight"]}"/>',
    ]
    first = {r["harness"]: r for r in data["datasets"][0]["results"]}
    for row, harness in enumerate(HERO_ORDER):
        label = HERO_LABELS.get(harness, first[harness]["label"])
        svg.append(
            _text(
                28, first_y + row * row_height + 29, label, font_size=16, font_weight=600 if harness == "duo" else 400
            )
        )
    for index, dataset in enumerate(data["datasets"]):
        x = start_x + index * panel_width
        if dataset["id"] == "hard":
            svg += [
                _text(x, 116, "SWE-bench Pro v2", font_size=17, font_weight=600),
                _text(x, 140, "HARD-51", font_size=14, fill=c["muted"]),
            ]
        else:
            svg.append(_text(x, 128, dataset["name"], font_size=17, font_weight=600))
        for tick in (40, 60, 80, 100):
            axis_x = x + (tick - 40) / 60 * plot_width
            svg += [
                f'<path d="M{axis_x:g} 158V{bottom}" stroke="{c["grid"]}" stroke-width="1"/>',
                _text(axis_x, bottom + 23, str(tick), font_size=12, text_anchor="middle", fill=c["muted"]),
            ]
        records = {r["harness"]: r for r in dataset["results"]}
        for row, harness in enumerate(HERO_ORDER):
            rate = _rate(records[harness], dataset)
            bar_width = (rate - 40) / 60 * plot_width
            y = first_y + row * row_height
            fill = c[harness] if harness in ("duo", "base") else c["bar"]
            svg.append(
                f'<rect x="{x}" y="{y + 11}" width="{bar_width:.3f}" height="23" '
                f'rx="4" fill="{fill}" data-dataset="{dataset["id"]}" '
                f'data-harness="{harness}" data-pass-rate="{rate:.8f}"/>'
            )
            svg.append(
                _text(
                    x + bar_width + 8,
                    y + 29,
                    f"{rate:.2f}",
                    font_size=15,
                    font_weight=600 if harness == "duo" else 400,
                    stroke=c["highlight"] if harness == "duo" else c["background"],
                    stroke_width=4,
                    stroke_linejoin="round",
                    paint_order="stroke",
                )
            )
    return "\n".join([*svg, "</g>", "</svg>", ""])


def render_results(data: dict, theme: str) -> str:
    """Render all 24 result rows, keeping OpenHands immediately after Mini."""
    c = PALETTES[theme]
    row_height, group_height = 32, 216
    height = 84 + len(data["datasets"]) * group_height + 42
    svg = _start(
        980,
        height,
        "Cross-harness benchmark results",
        "Six harnesses on four benchmarks. Columns show Pass@1, average tokens, "
        "estimated average cost, and cache hit. " + _description(data),
        c,
    )
    svg += [
        _text(24, 43, "Dataset", font_size=15, font_weight=600),
        _text(250, 43, "Harness", font_size=15, font_weight=600),
    ]
    for x, title, unit in [
        (552, "Pass@1", "(%) ↑"),
        (684, "Tokens", "Avg. (M) ↓"),
        (814, "Cost", "Avg. ($) ↓"),
        (948, "Cache hit", "(%) ↑"),
    ]:
        svg += [
            _text(x, 32, title, text_anchor="end", font_size=15, font_weight=600),
            _text(x, 55, unit, text_anchor="end", font_size=13, fill=c["muted"]),
        ]
    for index, dataset in enumerate(data["datasets"]):
        top = 84 + index * group_height
        svg.append(f'<path d="M24 {top - 8}H956" stroke="{c["grid"]}"/>')
        title_y = top + 107
        if dataset["id"] == "hard":
            svg += [
                _text(24, title_y - 10, "SWE-bench Pro v2", font_size=17, font_weight=600),
                _text(24, title_y + 13, "HARD-51", font_size=16),
            ]
        else:
            svg.append(_text(24, title_y, dataset["name"], font_size=17, font_weight=600))
        best = max(r["passed"] for r in dataset["results"])
        for row, result in enumerate(dataset["results"]):
            y = top + row * row_height + 27
            duo = result["harness"] == "duo"
            if duo:
                svg.append(f'<rect x="234" y="{y - 23}" width="728" height="30" rx="6" fill="{c["highlight"]}"/>')
            svg.append(
                _text(
                    250,
                    y,
                    result["label"],
                    font_size=17,
                    font_weight=600 if duo else 400,
                    fill=c["accent"] if duo else c["text"],
                )
            )
            values = [
                _rate(result, dataset),
                result["avg_tokens_m"],
                result["avg_cost_usd"],
                result["cache_hit_percent"],
            ]
            for column, (x, value) in enumerate(zip((552, 684, 814, 948), values)):
                svg.append(
                    _text(
                        x,
                        y,
                        f"{value:.2f}",
                        text_anchor="end",
                        font_size=17,
                        font_weight=700 if column == 0 and result["passed"] == best else 400,
                    )
                )
    svg += [
        f'<path d="M24 {height - 47}H956" stroke="{c["grid"]}"/>',
        _text(
            24,
            height - 19,
            f"GPT-5.6-Luna · max reasoning effort · Updated {data['updated']} · Costs are estimates",
            font_size=13,
            fill=c["muted"],
        ),
    ]
    return "\n".join([*svg, "</g>", "</svg>", ""])


def main() -> None:
    data = json.loads((ROOT / "docs" / "benchmark-results.json").read_text(encoding="utf-8"))
    for theme in PALETTES:
        for kind, render in [("hero", render_hero), ("results", render_results)]:
            (ROOT / "assets" / f"benchmark-{kind}-{theme}.svg").write_text(render(data, theme), encoding="utf-8")


if __name__ == "__main__":
    main()
