# Framework scripts

This directory contains OpenCollab framework launchers and provider diagnostics.
[OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval) contains the
benchmark generation, evaluation, reporting, and remote execution commands.

Use `uv run opencollab` as the standard development entry point.
`start_opencollab.sh` remains a compatibility launcher for callers that need
its physical-path handling.
`check_dashscope.py` performs an explicit provider connectivity check using the
caller's configuration.

`generate_benchmark_assets.py` renders the README charts and results-table SVGs
from [`docs/benchmark-results.json`](../docs/benchmark-results.json). Run it with
`python3 scripts/generate_benchmark_assets.py` after updating the published
summary. The light and dark assets share this source.

`benchmark_tool_loop.py` compares framework tool-processing overhead between two
explicit source checkouts using mock providers and disposable local workspaces.
See [the tool-loop measurement guide](../docs/development/tool-loop-progress.md)
for scenarios and result fields.

`demo_team_issue.sh` copies a tiny failing Python fixture to a disposable
workspace and runs it through the explicit three-role `analyst`, `coder`, and
`tester` TUI demo. See [the team-issue example](../examples/team-issue/README.md)
for the interaction flow. Its dynamically spawned teammates require the explicit host-shell option.

`run_collab_team.py` runs the reusable three-role team in
`configs/team.collab.yaml` against a workspace. The script passes
`prebuild_team=True` to seat the full roster before the first model call. The
same option is available through `OpenCollab.team()`. The interactive CLI lacks
a prebuild option, and this configuration relies on the prebuilt Coder and
Tester because its roles hold no `spawn_agent` tool.

```bash
uv run scripts/run_collab_team.py --workspace ./repo --prompt "fix the failing test" \
    --allow-unisolated-shell
```

The handoff payload between roles is a commit sha, so the roles need a shell
that can run `git`. Outside a sandboxed environment that requires
`--allow-unisolated-shell`, which lets the roles execute commands on the host:
pass it only for a workspace you trust.

`analyst_cards.py` renders the Analyst cards of the handoff experiment from
`configs/handoff-experiment/` using one shared body (`shared.md`) with two slots,
one file per closing block (`blocks/`), one per statement of the Analyst's own
tool bundle (`capabilities/`), and one registry (`variants.yaml`). The rendered
cards are checked in beside those files and are what the team files load, so
the script writes when given `--write`. Run bare it reports any card that
has drifted from its declaration and exits non-zero.

```bash
uv run scripts/analyst_cards.py            # check
uv run scripts/analyst_cards.py --write    # render after adding a cell
```

Every cell of that experiment claims two cards differ in one named place and
nowhere else, so the shared body exists exactly once rather than once per card.
`configs/handoff-experiment/legacy/` holds the eight first-generation cards,
which retain their original treatment text and the attribution of the batches
measured under them, and porting one of their blocks onto the current body would produce a
different cell rather than the same one.

## Repository maintenance commands

`scripts/check_added_files.py BASE HEAD` measures changed regular files. It
rejects additions or growth past 512,000 bytes and reports an advisory warning
when a Python module crosses 800 lines. CI uses an empty-tree base with
`--require-files` for the complete-tree check on `main`.
`scripts/check_interface_width.py` is a local diagnostic for public top-level
names. Its current command returns a nonzero result for modules above its
configured width limit. It is separate from the Hygiene workflow.

`scripts/check_conventional_title.py` validates a supplied title or the subject
of a Git commit. `scripts/check_secret_history.py` implements the existing
trusted-baseline history scan used by the Security workflow. Contribution and
baseline-update procedures are in [CONTRIBUTING.md](../CONTRIBUTING.md).

`scripts/generate_brand_assets.py` regenerates tracked brand SVG sources using
its declared standalone fonttools environment. Generated assets and font
provenance are documented in [assets/README.md](../assets/README.md).
