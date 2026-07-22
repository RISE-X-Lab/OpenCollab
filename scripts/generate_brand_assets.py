#!/usr/bin/env python3
"""Generate the OpenCollab wordmark assets from pinned Liberation Sans fonts."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

try:
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont
except ModuleNotFoundError as exc:  # pragma: no cover - exercised before argument parsing
    raise SystemExit(
        'fontTools is required; run with `uv run --no-project --with "fonttools==4.59.2" python ...`'
    ) from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "assets"
DEFAULT_BOLD_FONT = Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
DEFAULT_REGULAR_FONT = Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")

FONT_VERSION = "Version 2.1.5"
WORDMARK = "OpenCollab"
TAGLINE = "An Operating Theory of Organized Intelligence."
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SVG_PATH = f"{{{SVG_NAMESPACE}}}path"
SVG_STYLE = f"{{{SVG_NAMESPACE}}}style"
SVG_TEXT = f"{{{SVG_NAMESPACE}}}text"
ElementTree.register_namespace("", SVG_NAMESPACE)

MARK_DEFS = """  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#7C3AED"/>
      <stop offset="1" stop-color="#2563EB"/>
    </linearGradient>
    <mask id="cut" maskUnits="userSpaceOnUse" x="0" y="0" width="512" height="512">
      <rect x="0" y="0" width="512" height="512" fill="#fff"/>
      <circle cx="256" cy="96" r="62" fill="#000"/>
      <circle cx="408.17" cy="206.56" r="62" fill="#000"/>
      <circle cx="350.05" cy="385.44" r="62" fill="#000"/>
      <circle cx="161.95" cy="385.44" r="62" fill="#000"/>
      <circle cx="103.83" cy="206.56" r="62" fill="#000"/>
    </mask>
  </defs>"""

MARK = """    <circle cx="256" cy="256" r="160" fill="none" stroke="url(#g)" stroke-width="31" mask="url(#cut)"/>
    <circle cx="256" cy="96" r="45" fill="#713AED"/>
    <circle cx="408.17" cy="206.56" r="45" fill="#2A53EB"/>
    <circle cx="350.05" cy="385.44" r="45" fill="#2563EB"/>
    <circle cx="161.95" cy="385.44" r="45" fill="#5556EC"/>
    <circle cx="103.83" cy="206.56" r="45" fill="#7C3AED"/>"""


@dataclass(frozen=True)
class FontSpec:
    path: Path
    style: str
    sha256: str


@dataclass(frozen=True)
class TextOutline:
    path: str
    advance: float


EXPECTED_FONTS = {
    "Bold": "3973aa5054fb467dd5627245d3dc82e37bf16fe075756156a570455871351582",
    "Regular": "4659bc0c58c5028dd488ec928d41d9265db43d9b669fc14ca8b0832daca7b144",
}

GUIDELINE_STYLES = {
    "label": (13.5, 700, -0.35, "#000000"),
    "body": (12.8, 400, -0.08, "#253047"),
    "small": (11.25, 400, -0.05, "#38445B"),
    "micro": (9.75, 400, 0.0, "#536077"),
}

TEXT_STYLE_PREFIXES = ("text {", ".label {", ".body {", ".small {", ".micro {")


def _font_name(font: TTFont, name_id: int) -> str:
    candidates = [record for record in font["name"].names if record.nameID == name_id]
    preferred = sorted(candidates, key=lambda record: (record.platformID != 3, record.langID != 0x409))
    for record in preferred:
        try:
            return record.toUnicode()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"font is missing name table entry {name_id}")


def _load_font(spec: FontSpec) -> TTFont:
    if not spec.path.is_file():
        raise FileNotFoundError(f"font file not found: {spec.path}")

    digest = hashlib.sha256(spec.path.read_bytes()).hexdigest()
    if digest != spec.sha256:
        raise ValueError(f"unexpected SHA-256 for {spec.path}: {digest}")

    font = TTFont(spec.path, recalcBBoxes=False, recalcTimestamp=False)
    metadata = {
        "family": _font_name(font, 1),
        "style": _font_name(font, 2),
        "version": _font_name(font, 5),
    }
    expected = {"family": "Liberation Sans", "style": spec.style, "version": FONT_VERSION}
    if metadata != expected:
        raise ValueError(f"unexpected font metadata for {spec.path}: {metadata!r}")
    return font


def _kerning(font: TTFont) -> dict[tuple[str, str], int]:
    values: dict[tuple[str, str], int] = {}
    if "kern" not in font:
        return values
    for table in font["kern"].kernTables:
        values.update(table.kernTable)
    return values


def _outline(font: TTFont, text: str, *, letter_spacing: float = 0.0) -> TextOutline:
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()
    metrics = font["hmtx"].metrics
    kerning = _kerning(font)
    pen = SVGPathPen(glyph_set)
    x: float = 0
    previous: str | None = None

    for index, character in enumerate(text):
        glyph_name = cmap.get(ord(character))
        if glyph_name is None:
            raise ValueError(f"font does not contain {character!r}")
        if previous is not None:
            x += kerning.get((previous, glyph_name), 0)
        glyph_set[glyph_name].draw(TransformPen(pen, (1, 0, 0, 1, x, 0)))
        x += metrics[glyph_name][0]
        if index < len(text) - 1:
            x += letter_spacing
        previous = glyph_name

    return TextOutline(path=pen.getCommands(), advance=x)


def _path(outline: TextOutline, *, x: str, y: str, scale: str, fill: str) -> str:
    return f'  <path d="{outline.path}" transform="translate({x} {y}) scale({scale} -{scale})" fill="{fill}"/>'


def _svg_start(view_box: str, title: str, description: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" role="img" '
        f'aria-labelledby="title desc">\n'
        f'  <title id="title">{title}</title>\n'
        f'  <desc id="desc">{description}</desc>'
    )


def _mark_group(transform: str) -> str:
    return f'  <g transform="{transform}">\n{MARK}\n  </g>'


def _render_assets(wordmark: TextOutline, tagline: TextOutline) -> dict[Path, str]:
    horizontal = "\n".join(
        (
            _svg_start("0 0 2146.82 512", "OpenCollab", "OpenCollab horizontal brand lockup."),
            MARK_DEFS,
            _mark_group("translate(40 0)"),
            _path(wordmark, x="593", y="358.5", scale="0.1318359375", fill="#0F172A"),
            "</svg>\n",
        )
    )
    horizontal_tagline = "\n".join(
        (
            _svg_start(
                "0 0 1904.61 512",
                "OpenCollab",
                "OpenCollab horizontal brand lockup with its operating-theory tagline.",
            ),
            MARK_DEFS,
            _mark_group("translate(40 0)"),
            _path(wordmark, x="593", y="269.78", scale="0.11083984375", fill="#0F172A"),
            _path(tagline, x="593", y="419.1", scale="0.02666015625", fill="#253047"),
            "</svg>\n",
        )
    )
    stacked = "\n".join(
        (
            _svg_start("0 0 859.19 662.71", "OpenCollab", "OpenCollab stacked brand lockup."),
            MARK_DEFS,
            _mark_group("translate(173.59 -15)"),
            _path(wordmark, x="36", y="592.6", scale="0.068603515625", fill="#0F172A"),
            "</svg>\n",
        )
    )
    banner = "\n".join(
        (
            _svg_start("0 0 1435.05 420", "OpenCollab", "OpenCollab brand lockup on a dark panel."),
            MARK_DEFS,
            '  <rect width="1435.05" height="420" rx="56" fill="#0F172A"/>',
            _mark_group("translate(66.34 41.04) scale(0.66)"),
            _path(wordmark, x="404.6", y="273", scale="0.08115234375", fill="#FFFFFF"),
            "</svg>\n",
        )
    )
    return {
        ASSET_DIR / "lockup-horizontal.svg": horizontal,
        ASSET_DIR / "lockup-horizontal-tagline.svg": horizontal_tagline,
        ASSET_DIR / "lockup-stacked.svg": stacked,
        ASSET_DIR / "banner-dark.svg": banner,
    }


def _format_number(value: float) -> str:
    rendered = f"{value:.10f}".rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def _guideline_text_path(element: ElementTree.Element, *, bold: TTFont, regular: TTFont) -> ElementTree.Element:
    attributes = dict(element.attrib)
    allowed = {"class", "fill", "font-size", "font-weight", "letter-spacing", "text-anchor", "x", "y"}
    unexpected = set(attributes) - allowed
    if unexpected:
        raise ValueError(f"unsupported brand-board text attributes: {sorted(unexpected)}")
    if "x" not in attributes or "y" not in attributes:
        raise ValueError("brand-board text requires x and y coordinates")

    class_name = attributes.get("class")
    style = GUIDELINE_STYLES.get(class_name or "", (None, 400, 0.0, "#000000"))
    font_size = float(attributes.get("font-size", style[0] or 0))
    if font_size <= 0:
        raise ValueError(
            f"brand-board text has no valid font size: {ElementTree.tostring(element, encoding='unicode')}"
        )
    weight = int(attributes.get("font-weight", style[1]))
    letter_spacing = float(attributes.get("letter-spacing", style[2]))
    fill = attributes.get("fill", style[3])
    font = bold if weight >= 600 else regular
    scale = font_size / font["head"].unitsPerEm
    outline = _outline(font, element.text or "", letter_spacing=letter_spacing / scale)

    x = float(attributes["x"])
    anchor = attributes.get("text-anchor", "start")
    if anchor == "middle":
        x -= outline.advance * scale / 2
    elif anchor == "end":
        x -= outline.advance * scale
    elif anchor != "start":
        raise ValueError(f"unsupported text-anchor: {anchor}")

    scale_text = _format_number(scale)
    path = ElementTree.Element(
        SVG_PATH,
        {
            "d": outline.path,
            "transform": f"translate({_format_number(x)} {attributes['y']}) scale({scale_text} -{scale_text})",
            "fill": fill,
        },
    )
    path.tail = element.tail
    return path


def _render_brand_board(wordmark: TextOutline, *, bold: TTFont, regular: TTFont) -> tuple[Path, str]:
    path = ASSET_DIR / "brand-guidelines.svg"
    template_path = ASSET_DIR / "brand-guidelines.svg.in"
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True))
    tree = ElementTree.parse(template_path, parser=parser)
    root = tree.getroot()

    wordmark_paths = [element for element in root.iter(SVG_PATH) if element.attrib.get("id") == "wordmark-path"]
    if len(wordmark_paths) != 1:
        raise ValueError(f"expected one wordmark-path in {template_path}, found {len(wordmark_paths)}")
    wordmark_paths[0].set("d", wordmark.path)

    for parent in list(root.iter()):
        for index, element in enumerate(list(parent)):
            if element.tag == SVG_TEXT:
                parent.remove(element)
                parent.insert(index, _guideline_text_path(element, bold=bold, regular=regular))
    if next(root.iter(SVG_TEXT), None) is not None:
        raise ValueError(f"unconverted text element remains in {template_path}")

    style_elements = list(root.iter(SVG_STYLE))
    if len(style_elements) != 1 or style_elements[0].text is None:
        raise ValueError(f"expected one style block in {template_path}")
    style_elements[0].text = "\n".join(
        line for line in style_elements[0].text.splitlines() if not line.strip().startswith(TEXT_STYLE_PREFIXES)
    )
    ElementTree.indent(tree, space="  ")
    return path, ElementTree.tostring(root, encoding="unicode", short_empty_elements=True) + "\n"


def _write_or_check(outputs: dict[Path, str], *, check: bool) -> int:
    stale: list[Path] = []
    for path, rendered in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == rendered:
            continue
        stale.append(path)
        if not check:
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(path)

    if check and stale:
        for path in stale:
            print(f"out of date: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    for path in stale:
        print(f"generated: {path.relative_to(REPO_ROOT)}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bold-font", type=Path, default=DEFAULT_BOLD_FONT)
    parser.add_argument("--regular-font", type=Path, default=DEFAULT_REGULAR_FONT)
    parser.add_argument("--check", action="store_true", help="fail when generated assets differ from tracked files")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    bold = _load_font(FontSpec(args.bold_font, "Bold", EXPECTED_FONTS["Bold"]))
    regular = _load_font(FontSpec(args.regular_font, "Regular", EXPECTED_FONTS["Regular"]))
    wordmark = _outline(bold, WORDMARK)
    tagline = _outline(regular, TAGLINE)
    outputs = _render_assets(wordmark, tagline)
    board_path, board = _render_brand_board(wordmark, bold=bold, regular=regular)
    outputs[board_path] = board
    return _write_or_check(outputs, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
