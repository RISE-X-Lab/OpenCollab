---
name: team-config
description: Design an OpenCollab team and visualize it as an interactive HTML blueprint. Use whenever the task asks to create, design, scaffold, write, edit, or lay out a TEAM — its agent roles, per-role prompts, tool allowlists, model/temperature, and the spawn/message topology (who delegates to whom) — producing a valid configs/team.yaml AND a self-contained HTML the user can open, tweak, and hand back for you to refine the topology from their feedback.
---

Author a valid OpenCollab **team.yaml**, then render it to a **self-contained,
interactive HTML blueprint** the user can open in a browser, rewire, and hand
back for you to optimize. You already have `bash` + `file_write` (and likely
`file_read`); this grants no new tools. The shell cwd does NOT persist between
`bash` calls — use ABSOLUTE paths. Rendering is pure `sh` + `cat` (no Python).

## 1. Locate this skill's files
```sh
ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
SKILL="$ROOT/skills/team-config"
[ -d "$SKILL" ] || SKILL=$(find "$ROOT" -type d -name team-config -path '*skills*' 2>/dev/null | head -1)
```
It ships: `template.team.yaml` (commented starting point), `build.sh` (the
renderer glue), and the HTML template parts. Read `template.team.yaml` first.

## 2. Schema — what a team.yaml declares
- **`roles:`** — one block per agent role (the key is its name). Each role:
  - `prompt:` (block scalar) **or** `prompt_file:` — one is REQUIRED. Concrete,
    role-specific instructions.
  - `tools:` — an allowlist from the MENU below. Unknown names fail at startup.
  - `model:` (optional, inherits `OPENCOLLAB_MODEL`), `temperature:` (optional,
    0.0–2.0, inherits 0.2), `thinking:` (optional).
- **`topology:`** — directed graph `src: [dst, …]`. A role may spawn/message
  ONLY the roles listed for it. Coordination is gated by BOTH the tool AND an edge.
- **`entry:`** — which role is agent 0. Omitted → a role named `lead`, else the
  first role. An explicit `entry` naming no declared role fails fast.
- **`tool_limits:`** (optional) — per-tool output caps; NOT for coordination tools.

**Tool MENU (the only valid names):**
- work: `bash` `file_read` `file_write` `apply_patch` `run_tests` `git_diff`
  `grep` `ask_user`
- coordination (pair with topology edges): `spawn_agent` `spawn_with_review`
  `message_agent` `team_status`
- skill: `use_skill`

## 3. Author configs/team.yaml
Seed `configs/team.yaml` from the template ONLY if it doesn't exist yet — **never
clobber a team.yaml you didn't write**. OpenCollab never auto-loads a team file;
the user must select it with `--team-config PATH` or `OPENCOLLAB_TEAM_FILE`. If
the target already exists, read it and edit it in place; or, to preserve the
user's current team, write a fresh name like `configs/team.<name>.yaml`.
```sh
mkdir -p "$ROOT/configs"
[ -e "$ROOT/configs/team.yaml" ] || cp "$SKILL/template.team.yaml" "$ROOT/configs/team.yaml"
```
Then edit the target team file (file_write) — set the roles the task needs, write
each `prompt`, assign `tools`, and wire `topology`. Design rules:
- Give the **entry/lead** the coordination tools (`spawn_agent`, usually
  `message_agent`, `team_status`) and a topology edge to every role it drives.
- A **specialist** gets work tools only — no coordination tools unless the
  topology actually lets it reach someone (an edge without the tool is inert; a
  tool without an edge can't reach anyone).
- Keep prompts tight and role-specific: what this agent owns, how it hands off,
  and (for reviewers) the exact `VERDICT: PASS/FAIL` contract if the loop parses it.
- Match the shape to the job: a simple hub-and-spoke (lead → specialists), or a
  coder↔reviewer feedback loop, or an analyst-orchestrated GAN loop. Don't add
  roles the task doesn't need.

## 4. Render the interactive blueprint
```sh
mkdir -p "$ROOT/.opencollab/blueprints"
sh "$SKILL/build.sh" "$ROOT/configs/team.yaml" \
  "$ROOT/.opencollab/blueprints/$(basename "$ROOT/configs/team.yaml" .yaml).html"
```
(`.opencollab/` is gitignored, so blueprints never pollute the repo. The output is
named after the team file: `configs/team.yaml` → `.opencollab/blueprints/team.html`.)
This splices your YAML into the template and writes ONE self-contained HTML file.
Tell the user the absolute path and to open it in a browser. The page shows:
- a **role card** per agent (tools colored by category, model/temp, prompt);
- the **topology** as a directed graph you can **edit by dragging** — drag a role
  onto another to connect (spawn/message), click an edge to remove it (BFS layers
  from entry; dashed = ad-hoc role; amber = feedback/loop edge) — plus an
  **editable adjacency matrix** (keyboard-accessible fallback);
- a live **validation** banner (missing prompts, unknown tools, unreachable
  roles, tools-without-edges, …) — read it and fix any errors in the YAML;
- a live **team.yaml export** + a **Notes for the LLM** box.

## 5. Round-trip on feedback
The user can drag edges on the graph / toggle the matrix / edit roles in the
browser, then click **Copy YAML + notes** and paste the result back into the chat. When they do:
re-read their YAML and notes, apply the requested topology/role changes to
`configs/team.yaml`, re-run step 4, and report what you changed. Iterate until
they're happy.

## 6. Report
Return: the team YAML path, the exact `opencollab --team-config PATH` command, the
blueprint HTML path, the role count + entry, and any validation errors still
open (or "valid"). Keep it to a few lines.
