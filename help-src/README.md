# Wiki

The project site links to the wiki at `help/`, which opens the Chinese
guides on first visit. A global Chinese and English switch selects the wiki
edition. The browser remembers the selected edition when entering through `help/`
again. Explicit article URLs open their specified edition. The English
page at `help/en/` uses the same reader layout and displays "Coming soon."
Chinese article URLs remain under `help/zh-CN/`. The 70 guides are
maintained in `zh-CN/topics/`. Navigation pages link the guides by task and reader questions. Each Markdown document is rendered to an
independent HTML page, so article links work with JavaScript disabled. Browser
search reads a local static index.

## Edit and build

Topic front matter is a JSON object enclosed in YAML delimiters. It stores the
document ID, title, category, summary, search terms, related topics and source
revision. `catalog.json` stores the documented version, category labels and
navigation order. `search-routes.json` maps synonyms and ambiguous queries to guides.
Natural questions in `questions.md` also become search routes during the build.

Preserve document IDs when changing titles. Relative Markdown links are resolved
to generated HTML destinations. The builder reports missing link destinations. Source references identify the framework
revision that the guide describes. Update those references after checking the
corresponding API, CLI, configuration or runtime behavior.

Use an existing environment with `markdown-it-py`, or install the small build
requirements in a disposable environment. From this branch's repository root,
run the following commands.

```bash
python -m pip install -r tools/help-requirements.txt
python tools/build_help.py
python tools/check_help.py
node --test tools/help-search.test.mjs
python -m http.server 8000 --bind 127.0.0.1
```

Preview `/help/` through the local server. Generated HTML and the search
index are committed because GitHub Pages publishes this branch directly. CSS
and JavaScript are maintained in `help/assets/`. Increment `HELP_ASSET_VERSION`
in `tools/build_help.py` when changing shared styles, scripts, or search content,
then rebuild the pages so browsers request the matching asset version. The `.nojekyll` file preserves
the site's existing static publishing behavior.

## Validation and publication

The local checks verify generated links and anchors, guide coverage, source
metadata, code-block syntax, search routes, subpath URL resolution and homepage
entry points. Run changed examples against the documented framework revision with the model,
Docker or service setup specified in each guide.

Keep existing guides aligned when parameters, defaults, or behavior change. Add a
new topic when a feature introduces an independent user task. Include its reader
questions and synonyms in the search metadata and connect it to a reading path.

Review the Markdown sources and generated pages together. Publish by merging the
reviewed change into `gh-pages`. The project homepage navigation, hero action and
quick-start section all link to `help/`; every help page links back to the
project homepage. Application code remains on `main`.
