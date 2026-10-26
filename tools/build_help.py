from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "help-src" / "zh-CN"
OUTPUT = ROOT / "help" / "zh-CN"
SITE_URL = "https://rise-x-lab.github.io/OpenCollab/"
HELP_ASSET_VERSION = "4"
MD = MarkdownIt("commonmark", {"html": False}).enable("table")


@dataclass
class Page:
    source: Path
    destination: Path
    metadata: dict
    body: str
    headings: list[tuple[str, str]]
    tokens: list


def relative(target: Path, source: Path) -> str:
    return Path(os.path.relpath(target, source.parent)).as_posix()


def read_markdown(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        metadata, body = text[4:].split("\n---\n", 1)
        return json.loads(metadata), body.lstrip()
    return {}, text


def heading_ids(tokens: list) -> list[tuple[str, str]]:
    seen: dict[str, int] = {}
    headings = []
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        title = tokens[i + 1].content.replace("`", "")
        base = re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", title.lower())) or "section"
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = f"{base}-{count}" if count else base
        token.attrSet("id", anchor)
        if token.tag in {"h2", "h3"}:
            headings.append((anchor, title))
    return headings


def rewrite_links(tokens: list, page: Page, pages_by_source: dict[Path, Page]) -> None:
    for token in tokens:
        if token.children:
            rewrite_links(token.children, page, pages_by_source)
        if token.type != "link_open":
            continue
        href = token.attrGet("href") or ""
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or not parts.path:
            continue
        target = (page.source.parent / unquote(parts.path)).resolve()
        if target not in pages_by_source:
            raise ValueError(f"Unknown local document link in {page.source}: {href}")
        destination = pages_by_source[target].destination
        result = relative(destination, page.destination)
        if parts.fragment:
            result += "#" + parts.fragment
        token.attrSet("href", result)


def navigation(page: Page, pages: list[Page], categories: list[dict]) -> str:
    parts = []
    for category in categories:
        entries = [p for p in pages if p.metadata.get("category") == category["id"]]
        opened = " open" if page.metadata.get("category") == category["id"] else ""
        links = []
        for entry in entries:
            current = ' aria-current="page"' if entry is page else ""
            links.append(
                f'<a href="{relative(entry.destination, page.destination)}"{current}>'
                f'{html.escape(entry.metadata["title"])}</a>'
            )
        parts.append(f'<details{opened}><summary>{html.escape(category["title"])}'
                     f' <small>{len(entries)}</small></summary>{"".join(links)}</details>')
    return "".join(parts)


def language_switch(destination: Path, language: str) -> str:
    links = []
    for code, label in (("zh-CN", "中文"), ("en", "English")):
        target = relative(ROOT / "help" / code / "index.html", destination)
        current = ' aria-current="true"' if code == language else ""
        links.append(f'<a href="{target}" lang="{code}" hreflang="{code}"{current}>{label}</a>')
    return '<nav class="language-switch" aria-label="Language">' + "".join(links) + '</nav>'


def head_assets(destination: Path) -> str:
    icon = relative(ROOT / "fig" / "mark.svg", destination)
    css = relative(ROOT / "help" / "assets" / "help.css", destination)
    script = relative(ROOT / "help" / "assets" / "help.mjs", destination)
    fonts = "https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&amp;family=Noto+Sans:wght@400;500;600;700&amp;family=Google+Sans+Code:wght@400;500&amp;display=swap"
    return f'''<link rel="icon" href="{icon}" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{fonts}" media="print" onload="this.media='all'">
<link rel="stylesheet" href="{css}?v={HELP_ASSET_VERSION}">
<script type="module" src="{script}?v={HELP_ASSET_VERSION}"></script>'''


def render_header(destination: Path, language: str) -> str:
    english = language == "en"
    home = relative(ROOT / "help" / language / "index.html", destination)
    project = relative(ROOT / "index.html", destination)
    mark = relative(ROOT / "fig" / "mark.svg", destination)
    label = "Search help" if english else "搜索帮助文档"
    placeholder = "Coming soon" if english else "搜索文档、功能或问题"
    input_options = ' disabled' if english else ' autocomplete="off" aria-controls="search-results" aria-expanded="false"'
    results = '' if english else '''<kbd class="search-shortcut" aria-hidden="true">Ctrl K</kbd>
<div id="search-results" class="results" hidden></div>
<div id="search-status" class="sr-only" role="status" aria-live="polite"></div>'''
    return f'''<header class="topbar"><div class="topbar-inner">
<a class="brand" href="{home}"><img src="{mark}" alt="" width="32" height="32"><span>OpenCollab<small>{'Wiki' if english else 'Wiki'}</small></span></a>
<div class="searchbox">
<svg class="search-icon" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg>
<label class="sr-only" for="help-search">{label}</label>
<input id="help-search" type="search" placeholder="{placeholder}"{input_options}>
{results}
</div>
<div class="header-actions">
{language_switch(destination, language)}
<a class="project-link" href="{project}" aria-label="{'Project home' if english else '项目主页'}"><span>{'Project home' if english else '项目主页'}</span><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg></a>
<button id="menu-toggle" type="button" aria-expanded="false" aria-controls="help-navigation">{'Menu' if english else '目录'}</button>
</div>
</div></header>'''


def render_page(page: Page, pages: list[Page], config: dict) -> str:
    title = page.metadata["title"]
    home = relative(OUTPUT / "index.html", page.destination)
    project = relative(ROOT / "index.html", page.destination)
    search_index = relative(OUTPUT / "search-index.json", page.destination) + f"?v={HELP_ASSET_VERSION}"
    paths = relative(OUTPUT / "reading-paths" / "index.html", page.destination)
    questions = relative(OUTPUT / "questions" / "index.html", page.destination)
    source_path = relative(page.source, page.destination)
    category = next((c["title"] for c in config["categories"]
                     if c["id"] == page.metadata.get("category")), "阅读导航")
    toc = "".join(f'<a href="#{quote(anchor)}">{html.escape(label)}</a>'
                  for anchor, label in page.headings)
    body = MD.renderer.render(page.tokens, MD.options, {})
    canonical = SITE_URL + page.destination.relative_to(ROOT).as_posix()
    description = page.metadata.get("summary", "OpenCollab 中文使用与开发指南")
    entry = ' data-help-entry="true"' if page.destination == ROOT / "help" / "index.html" else ""
    home_page = page.metadata["id"] == "home"
    nav_entries = [(home, "home", "Wiki 首页"), (paths, "reading-paths", "阅读路径"),
                   (questions, "questions", "问题导航")]
    primary_nav = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if page.metadata["id"] == ident else '')
        + f'>{label}</a>' for href, ident, label in nav_entries
    )
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · OpenCollab Wiki</title>
<meta name="description" content="{html.escape(description, quote=True)}">
<link rel="canonical" href="{canonical}">
{head_assets(page.destination)}
</head>
<body class="{'help-home' if home_page else 'help-article'}" data-search-index="{search_index}"{entry}>
<a class="skip" href="#content">跳到正文</a>
{render_header(page.destination, 'zh-CN')}
<div class="layout">
<nav id="help-navigation" aria-label="帮助分类">
<div class="nav-start">{primary_nav}</div>
<p class="nav-label">文档目录</p>
{navigation(page, pages, config["categories"])}
</nav>
<main id="content" tabindex="-1">
<p class="category">{'WIKI' if home_page else html.escape(category)}</p>
{body}
<footer class="article-footer"><a href="{source_path}">Markdown 源文档</a><a href="{config['source_commit_url']}">OpenCollab {config['documented_version']}</a><a href="{project}">返回项目主页</a></footer>
</main>
<aside class="toc" aria-label="本页内容"><strong>本页内容</strong>{toc}</aside>
</div>
</body>
</html>
'''


def render_english_placeholder() -> str:
    destination = ROOT / "help" / "en" / "index.html"
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wiki · OpenCollab</title>
<meta name="description" content="OpenCollab Wiki. English guides are coming soon.">
<link rel="canonical" href="{SITE_URL}help/en/index.html">
{head_assets(destination)}
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
{render_header(destination, 'en')}
<div class="layout">
<nav id="help-navigation" aria-label="Help navigation">
<div class="nav-start"><a href="index.html" aria-current="page">Wiki home</a></div>
</nav>
<main id="content" class="placeholder" tabindex="-1">
<img class="placeholder-mark" src="../../fig/mark.svg" alt="" width="52" height="52">
<h1>Wiki</h1>
<p>Coming soon.</p>
</main>
</div>
</body>
</html>
'''


def render_redirect(old_id: str, target: str, by_id: dict[str, Page]) -> str:
    """Keep a merged guide's published URL working by forwarding to its new section."""
    target_id, _, fragment = target.partition("#")
    page = by_id[target_id]
    destination = OUTPUT / old_id / "index.html"
    href = relative(page.destination, destination) + (f"#{quote(fragment)}" if fragment else "")
    home = relative(OUTPUT / "index.html", destination)
    canonical = SITE_URL + page.destination.relative_to(ROOT).as_posix()
    title = html.escape(page.metadata["title"])
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · OpenCollab Wiki</title>
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={html.escape(href, quote=True)}">
<link rel="canonical" href="{canonical}">
{head_assets(destination)}
</head>
<body class="help-article" data-redirect="{html.escape(target, quote=True)}">
<a class="skip" href="#content">跳到正文</a>
{render_header(destination, 'zh-CN')}
<div class="layout">
<nav id="help-navigation" aria-label="帮助分类">
<div class="nav-start"><a href="{home}">Wiki 首页</a></div>
</nav>
<main id="content" tabindex="-1">
<p class="category">已合并</p>
<h1>这篇指南已合并</h1>
<p>内容现在位于 <a href="{href}">{title}</a>。浏览器没有自动跳转时，请点击这个链接。</p>
</main>
</div>
</body>
</html>
'''


def main() -> None:
    config = json.loads((ROOT / "help-src" / "catalog.json").read_text())
    pages = []
    for source in sorted(SOURCE.rglob("*.md")):
        metadata, body = read_markdown(source)
        if source.relative_to(SOURCE).as_posix() == "index.md":
            metadata.update(id="home", title="OpenCollab Wiki", category="navigation")
            destination = OUTPUT / "index.html"
        else:
            metadata.setdefault("id", source.stem)
            metadata.setdefault("title", body.splitlines()[0].lstrip("# "))
            metadata.setdefault("category", "navigation")
            destination = OUTPUT / metadata["id"] / "index.html"
        tokens = MD.parse(body)
        pages.append(Page(source.resolve(), destination, metadata, body, heading_ids(tokens), tokens))
    by_id = {p.metadata["id"]: p for p in pages}
    if len(by_id) != len(pages):
        raise ValueError("Document IDs must be unique")
    order = {key: i for i, key in enumerate(config["topic_order"])}
    pages.sort(key=lambda p: order.get(p.metadata["id"], len(order)))
    by_source = {p.source: p for p in pages}
    for page in pages:
        rewrite_links(page.tokens, page, by_source)
        page.destination.parent.mkdir(parents=True, exist_ok=True)
        page.destination.write_text(render_page(page, pages, config), encoding="utf-8")
    home = by_id["home"]
    landing = Page(home.source, ROOT / "help" / "index.html", home.metadata,
                   home.body, [], MD.parse(home.body))
    landing.headings = heading_ids(landing.tokens)
    rewrite_links(landing.tokens, landing, by_source)
    landing.destination.write_text(render_page(landing, pages, config), encoding="utf-8")
    english = ROOT / "help" / "en" / "index.html"
    english.parent.mkdir(parents=True, exist_ok=True)
    english.write_text(render_english_placeholder(), encoding="utf-8")
    redirects = config.get("redirects", {})
    for old_id, target in redirects.items():
        if old_id in by_id:
            raise ValueError(f"Redirect source is still a guide: {old_id}")
        if target.partition("#")[0] not in by_id:
            raise ValueError(f"Unknown redirect destination: {old_id} -> {target}")
        destination = OUTPUT / old_id / "index.html"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_redirect(old_id, target, by_id), encoding="utf-8")
    documents = []
    for page in pages:
        meta = page.metadata
        visible = " ".join(
            child.content for token in page.tokens for child in token.children or []
            if child.type in {"text", "code_inline"}
        )
        documents.append({
            "id": meta["id"], "title": meta["title"], "summary": meta.get("summary", ""),
            "url": page.destination.relative_to(OUTPUT).as_posix(),
            "keywords": meta.get("keywords", []) + meta.get("aliases", []) + meta.get("questions", []),
            "headings": [label for _, label in page.headings], "text": visible,
        })
    routes = json.loads((ROOT / "help-src" / "search-routes.json").read_text())["routes"]
    questions = SOURCE / "questions.md"
    for line in questions.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\| (.+?) \| \[.*?\]\(([^)]+)\) \|", line)
        if match:
            question, href = match.groups()
            target = by_source[(questions.parent / unquote(urlsplit(href).path)).resolve()]
            routes.append({"queries": [question], "target_id": target.metadata["id"],
                           "mode": "direct", "also_show": []})
    for route in routes:
        for ident in [route["target_id"], *route.get("also_show", [])]:
            if ident not in by_id:
                raise ValueError(f"Unknown search destination: {ident}")
    (OUTPUT / "search-index.json").write_text(
        json.dumps({"documents": documents, "routes": routes}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Built {len(pages) + 2} help pages from {len(order)} guide topics "
          f"and {len(redirects)} redirects")


if __name__ == "__main__":
    main()
