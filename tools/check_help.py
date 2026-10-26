from __future__ import annotations

import ast
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build_help import OUTPUT, ROOT, SOURCE, read_markdown


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        for field in ("href", "src"):
            if values.get(field):
                self.links.append(values[field])


def main() -> None:
    parsed: dict[Path, PageLinks] = {}
    for page in [ROOT / "index.html", *(ROOT / "help").rglob("*.html")]:
        parser = PageLinks()
        parser.feed(page.read_text(encoding="utf-8"))
        parsed[page.resolve()] = parser
    errors = []
    links_checked = 0
    for page, parser in parsed.items():
        for link in parser.links:
            parts = urlsplit(link)
            if parts.scheme or parts.netloc:
                continue
            target = (page.parent / unquote(parts.path)).resolve() if parts.path else page
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                errors.append(f"Missing target: {page.relative_to(ROOT)} -> {link}")
            elif parts.fragment and target in parsed and unquote(parts.fragment) not in parsed[target].ids:
                errors.append(f"Missing anchor: {page.relative_to(ROOT)} -> {link}")
            links_checked += 1
    config = json.loads((ROOT / "help-src" / "catalog.json").read_text())
    topics = {}
    blocks = 0
    for source in (SOURCE / "topics").rglob("*.md"):
        metadata, body = read_markdown(source)
        ident = metadata["id"]
        if ident in topics:
            errors.append(f"Duplicate document ID: {ident}")
        topics[ident] = metadata
        if metadata["verified_ref"] != config["verified_ref"]:
            errors.append(f"Unreviewed source revision: {source}")
        for code in re.findall(r"^```(?:python|py)\s*\n(.*?)^```", body, re.M | re.S):
            compile(code, str(source), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            blocks += 1
    if set(topics) != set(config["topic_order"]):
        errors.append("Navigation and guide sources disagree")
    for ident, metadata in topics.items():
        for other in metadata["related"]:
            if other not in topics:
                errors.append(f"Unknown related document: {ident} -> {other}")
        if not (OUTPUT / ident / "index.html").is_file():
            errors.append(f"Missing generated page: {ident}")
    redirects = config.get("redirects", {})
    for old_id, target in redirects.items():
        if old_id in topics:
            errors.append(f"Redirect source is still a guide: {old_id}")
        if target.partition("#")[0] not in topics:
            errors.append(f"Unknown redirect destination: {old_id} -> {target}")
        stub = OUTPUT / old_id / "index.html"
        if not stub.is_file() or f'data-redirect="{target}"' not in stub.read_text():
            errors.append(f"Missing redirect page: {old_id}")
    navigation_pages = {path.stem for path in SOURCE.glob("*.md")} - {"index"}
    for generated in OUTPUT.glob("*/index.html"):
        ident = generated.parent.name
        if ident not in topics and ident not in redirects and ident not in navigation_pages:
            errors.append(f"Stale generated page: {generated.relative_to(ROOT)}")
    homepage = (ROOT / "index.html").read_text()
    if homepage.count('href="help/">Wiki</a>') != 3:
        errors.append("Homepage help links are missing")
    for page in parsed:
        if page.parent == ROOT:
            continue
        text = page.read_text()
        if 'class="project-link"' not in text:
            errors.append(f"Missing project return link: {page}")
        if 'class="language-switch"' not in text:
            errors.append(f"Missing language switch: {page}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Checked {len(topics)} guides, {len(redirects)} redirects, {len(parsed)} HTML pages, "
          f"{links_checked} local links and {blocks} Python code blocks")


if __name__ == "__main__":
    main()
