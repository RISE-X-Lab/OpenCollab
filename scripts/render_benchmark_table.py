"""Render README benchmark data as self-contained light and dark SVG tables."""

from html import escape
from itertools import groupby
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PALETTES = {
    "light": {
        "background": "#ffffff",
        "text": "#1f2328",
        "muted": "#656d76",
        "rule": "#d8dee4",
        "accent": "#6d28d9",
        "highlight": "#f4f0ff",
    },
    "dark": {
        "background": "#0d1117",
        "text": "#e6edf3",
        "muted": "#9da7b3",
        "rule": "#30363d",
        "accent": "#c4b5fd",
        "highlight": "#211a35",
    },
}


def render(rows: list[list[str]], colors: dict[str, str]) -> str:
    groups = [(name, list(group)) for name, group in groupby(rows, key=lambda row: row[0])]
    height = 82 + sum(len(group) * 34 + 26 for _, group in groups) + 18
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="980" height="{height}" '
        f'viewBox="0 0 980 {height}" role="img" aria-labelledby="title description">',
        '  <title id="title">Cross-harness benchmark results</title>',
        '  <desc id="description">Five harnesses on SWE-bench Pro, Terminal-Bench 2.1, '
        "and DeepSWE. OC Duo has the highest reported Pass@1 in each group. "
        "All values are also available in the README data table.</desc>",
        f'  <rect width="980" height="{height}" rx="12" fill="{colors["background"]}"/>',
        '  <g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" '
        f'font-size="17" fill="{colors["text"]}">',
    ]

    def text(x: int, y: int, value: str, *, anchor: str = "start", extra: str = "") -> None:
        svg.append(f'    <text x="{x}" y="{y}" text-anchor="{anchor}" {extra}>{escape(value)}</text>')

    text(24, 43, "Dataset", extra='font-size="15" font-weight="600"')
    text(250, 43, "Harness", extra='font-size="15" font-weight="600"')
    columns = [
        (552, "Pass@1", "(%) ↑"),
        (684, "Tokens", "Avg. (M) ↓"),
        (814, "Cost", "Avg. ($) ↓"),
        (948, "Cache hit", "(%) ↑"),
    ]
    for x, label, unit in columns:
        text(x, 32, label, anchor="end", extra='font-size="15" font-weight="600"')
        text(x, 55, unit, anchor="end", extra=f'font-size="13" fill="{colors["muted"]}"')
    svg.append(f'    <path d="M24 76H956" stroke="{colors["rule"]}" stroke-width="1.5"/>')
    top = 91
    for index, (dataset, group) in enumerate(groups):
        if index:
            svg.append(f'    <path d="M24 {top - 13}H956" stroke="{colors["rule"]}"/>')
        text(24, top + (len(group) - 1) * 17 + 23, dataset, extra='font-weight="600"')
        for offset, row in enumerate(group):
            y = top + offset * 34
            duo = row[1] == "OC (Duo)"
            if duo:
                svg.append(
                    f'    <rect x="234" y="{y - 1}" width="728" height="32" rx="6" fill="{colors["highlight"]}"/>'
                )
            label_style = f'font-weight="650" fill="{colors["accent"]}"' if duo else ""
            text(250, y + 22, row[1], extra=label_style)
            for column, value in zip(columns, row[2:]):
                style = 'style="font-variant-numeric:tabular-nums"'
                if duo and column[1] == "Pass@1":
                    style += f' font-weight="700" fill="{colors["accent"]}"'
                text(column[0], y + 22, value, anchor="end", extra=style)
        top += len(group) * 34 + 26
    svg.extend(["  </g>", "</svg>", ""])
    return "\n".join(svg)


def main() -> None:
    section = (ROOT / "README.md").read_text(encoding="utf-8").split("## Benchmark results\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    rows = [
        [cell.strip().replace("**", "") for cell in line.strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith(("| SWE-bench Pro |", "| Terminal-Bench 2.1 |", "| DeepSWE |"))
    ]
    if not rows or any(len(row) != 6 for row in rows):
        raise ValueError("Expected six-column benchmark rows in README.md")
    for theme, colors in PALETTES.items():
        path = ROOT / "assets" / f"benchmark-results-{theme}.svg"
        path.write_text(render(rows, colors), encoding="utf-8")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
