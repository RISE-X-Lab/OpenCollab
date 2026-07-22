"""Integrity checks for public brand assets."""

from pathlib import Path
from xml.etree import ElementTree

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ASSET_DIR = _REPO_ROOT / "assets"
_PATH_WORDMARKS = {
    "banner-dark.svg",
    "lockup-horizontal-tagline.svg",
    "lockup-horizontal.svg",
    "lockup-stacked.svg",
}


def _assert_self_contained_svg(path: Path) -> None:
    assert path.stat().st_size < 500_000, path
    root = ElementTree.parse(path).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg", path
    for element in root.iter():
        for name, value in element.attrib.items():
            if name.endswith("href"):
                assert value.startswith("#"), (path, value)
            assert "url(http" not in value.lower(), (path, value)
            assert "file:" not in value.lower(), (path, value)


def test_svg_assets_are_self_contained_and_within_hygiene_limit() -> None:
    svg_paths = [*sorted(_ASSET_DIR.glob("*.svg")), _ASSET_DIR / "brand-guidelines.svg.in"]
    for path in svg_paths:
        _assert_self_contained_svg(path)


def test_wordmarks_are_font_independent_paths() -> None:
    for filename in _PATH_WORDMARKS:
        root = ElementTree.parse(_ASSET_DIR / filename).getroot()
        assert not list(root.iter("{http://www.w3.org/2000/svg}text")), filename

    board = ElementTree.parse(_ASSET_DIR / "brand-guidelines.svg").getroot()
    assert not list(board.iter("{http://www.w3.org/2000/svg}text"))
    ids = {element.attrib.get("id") for element in board.iter()}
    assert "wordmark-path" in ids
    wordmark_uses = [
        element
        for element in board.iter("{http://www.w3.org/2000/svg}use")
        if element.attrib.get("href") == "#wordmark-path"
    ]
    assert len(wordmark_uses) == 4

    template = ElementTree.parse(_ASSET_DIR / "brand-guidelines.svg.in").getroot()
    assert list(template.iter("{http://www.w3.org/2000/svg}text"))


def test_brand_assets_record_open_font_provenance() -> None:
    tracked_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (*sorted(_ASSET_DIR.rglob("*")), _REPO_ROOT / "scripts" / "generate_brand_assets.py")
        if path.is_file() and path.suffix in {".in", ".md", ".svg", ".txt"}
    )
    for proprietary_font in ("Aven" + "ir", "SF" + " Pro"):
        assert proprietary_font not in tracked_text

    readme = (_ASSET_DIR / "README.md").read_text(encoding="utf-8")
    assert "Liberation Sans 2.1.5" in readme
    assert "SIL Open Font License 1.1" in readme
    assert (_ASSET_DIR / "LICENSES" / "LiberationSans-OFL-1.1.txt").is_file()
