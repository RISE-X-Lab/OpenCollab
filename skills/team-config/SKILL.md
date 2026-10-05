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

`roles` contains one block per role, keyed by its name. The current role fields
are defined by `opencollab/bootstrap/team_config.py`.

| Role field | Meaning |
| --- | --- |
| `prompt` or `prompt_file` | Required role instructions. A prompt file resolves relative to the team file. |
| `tools` | Ordered allowlist from the tool menu below. Unknown names fail at startup. |
| `model` | Optional override of the runtime's configured model. |
| `temperature` | Optional value from 0.0 to 2.0. Omission inherits the global configuration. |
| `thinking`, `thinking_params` | Optional thinking switch and provider parameter mapping, inherited from the global configuration when omitted. |
| `profile` | `single2` selects that profile and `base` follows the Base mapping. Omission or `default` uses the team's ordinary agent configuration. A named profile supplies the base prompt, shaper, safety wrapper and output caps, with the role card appended. |
| `budget.tokens` | Optional positive token allowance for each agent in this role. It overrides the team-level `budget.tokens`. |

`topology` declares directed role edges. A role needs both its coordination tool
and an allowed edge to spawn or message a destination. `entry` names agent 0.
When omitted it selects `lead`, or the first declared role when `lead` is absent.
An explicit entry must name a declared role.

A top-level `budget.tokens` supplies a default allowance to every declared role.
If any role declares an allowance, every role needs one through that default or
its own override. Each agent then spends its own allowance. With all allowances
omitted, the runtime keeps its shared team pool and `per_agent_cap` allocation.

`tool_limits` sets output caps for `bash`, `file_read`, `git_diff` and `grep`.
Use the accepted constructor keys from `opencollab/bootstrap/tool_registry.py`.

| Tool category | Current names |
| --- | --- |
| Work and delivery | `bash`, `file_read`, `file_write`, `apply_patch`, `git_diff`, `grep`, `adopt`, `submit`, `ask_user` |
| Coordination | `spawn_agent`, `spawn_with_review`, `message_agent`, `team_status` |
| Skill loading | `use_skill` |

`ask_user` is available when the runtime provides human interaction for that
seat. `adopt` checks out an existing commit by its SHA under the runtime's
command policy. `submit` ends the current turn and records the agent's summary.

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

The shipped blueprint menu currently omits `adopt` and `submit`. Add those tools
in YAML before rendering. The role cards display them with an unknown category
and the banner reports them as unknown, while Copy YAML and Download retain
them. Keep their chips selected when editing the graph. Check that specific
finding against `KNOWN_TOOL_NAMES` in `opencollab/bootstrap/tool_registry.py`.
The runtime loader accepts both names.

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
