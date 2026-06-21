---
name: workflow-to-d2
description: Visualize an OpenCollab workflow as a polished diagram. Use whenever the task asks to diagram, chart, visualize, or draw the structure / topology / flow of a workflows/*.py module — its phases, agents, structured-output schemas, fan-out and retry loops — producing a styled .d2 and rendering it to .svg.
---

Turn an OpenCollab workflow module (a `@workflow` async fn in `workflows/*.py`)
into a STYLED D2 diagram of its static topology, then render it to SVG. You
already have `bash` (and likely `file_read`); this grants no new tools. The shell
cwd does NOT persist between bash calls — use ABSOLUTE paths.

## 1. Paths
- `ROOT=$(git rev-parse --show-toplevel)`
- `OUT="$ROOT/.opencollab/diagrams"; mkdir -p "$OUT"` — gitignored, never pollutes
  the repo. Write the `.d2` and `.svg` here.
- `<name>` = the workflow's `name=` (e.g. `split-solve`).

## 2. Read the workflow (do NOT import it)
Read the `.py` as TEXT (`file_read` / `cat`); never import it (top-level code runs).
Extract:
- `@workflow(... phases=[...])` → ordered phase containers;
- each `ctx.agent(..., label="X")` → an agent node named by its literal `label`;
- a `schema=XXX_SCHEMA` kwarg on an agent → a structured-output artifact beside it;
- `for`/`while` around `ctx.agent` → a retry loop; read the cap constant
  (e.g. `MAX_ROUNDS_PER_SUBTASK = 3`);
- `ctx.parallel` / `ctx.pipeline` → fan-out / staged nodes;
- `if` / early-return gates (PASS vs not) → conditional edges to a `done` / stop sink.

Runtime-only counts (number of subtasks N, real rounds) are unknown from source —
draw 2–3 representative instances (`subtask 1 / 2 / N`) and label loops with the
cap. The graph shows POSSIBLE PATHS, not one actual run.

## 3. Style header — paste VERBATIM as the top of every .d2
```
vars: { d2-config: { theme-id: 0; sketch: false; pad: 24 } }
direction: down
classes: {
  agent:    { style: { fill: "#dbe6f7"; stroke: "#4a6fa5"; stroke-width: 1; border-radius: 8; font-size: 13 } }
  schema:   { shape: page; style: { fill: "#fff6e0"; stroke: "#d4a83a"; stroke-width: 1; stroke-dash: 3; font-size: 11 } }
  terminal: { shape: oval; style: { fill: "#f0f0f0"; stroke: "#555555"; stroke-width: 1; font-size: 13 } }
  stop:     { shape: oval; style: { fill: "#fbecec"; stroke: "#cc9988"; stroke-width: 1; font-size: 13 } }
  phase-analyze: { style: { fill: "#eef3fb"; stroke: "#c9d8f0"; stroke-width: 1; border-radius: 10; font-size: 15 } }
  phase-solve:   { style: { fill: "#eef7f0"; stroke: "#bfe0c8"; stroke-width: 1; border-radius: 10; font-size: 15 } }
  phase-synth:   { style: { fill: "#f3f0fb"; stroke: "#d4c9ee"; stroke-width: 1; border-radius: 10; font-size: 15 } }
  subtask-box:   { style: { fill: "#e3f1e7"; stroke: "#7fb98c"; stroke-width: 1; border-radius: 8; font-size: 12 } }
}
```

## 4. Mapping — node → class, edge → meaning
Node: `id: "Label" { class: NAME }`
- start (goal/args) & end (done) → `terminal`
- an agent (`ctx.agent`) → `agent`
- a `schema=` structured output → `schema`
- a phase container → `phase-analyze` / `phase-solve` / `phase-synth` (apply the 3
  tints to phases in order; reuse them if a workflow has >3 phases)
- a subtask / loop group inside a phase → `subtask-box`
- a failure / stop sink → `stop`

Edge: `a -> b: "label" { style: {...} }` — refer to nested nodes by dotted path
(`solve.s1.t -> synthesize.S`).
- normal flow → no style
- success / PASS → green: `{ stroke: "#3a7d4a"; font-color: "#3a7d4a"; font-size: 10 }`
- failure / retry / non-PASS → red dashed: `{ stroke: "#c0392b"; font-color: "#c0392b"; stroke-dash: 3; font-size: 10 }`
- "produces SCHEMA" → gold dashed: `{ stroke: "#d4a83a"; stroke-dash: 3; font-size: 10 }`

## 5. Worked example (split-solve) — copy this shape, swap in the real topology
```
goal: "goal (任务目标)" { class: terminal }
analyze: "① analyze" { class: phase-analyze
  A: Analyst { class: agent }
  A_out: "PLAN_SCHEMA\n{root_cause, subtasks[]}" { class: schema }
  A -> A_out: produces { style: { stroke: "#d4a83a"; stroke-dash: 3; font-size: 10 } }
}
solve: "② solve   (subtask 逐个串行)" { class: phase-solve
  s1: "subtask 1" { class: subtask-box
    c: Coder { class: agent }
    t: Tester { class: agent }
    v: "VERDICT_SCHEMA" { class: schema }
    c -> t: summary { style.font-size: 10 }
    t -> c: "findings (重试 ≤3)" { style: { stroke-dash: 3; font-size: 10 } }
    t -> v: { style: { stroke: "#d4a83a"; stroke-dash: 3 } }
  }
  sn: "subtask N" { class: subtask-box
    c: Coder { class: agent }
    t: Tester { class: agent }
    c -> t: summary { style.font-size: 10 }
    t -> c: "findings (重试)" { style: { stroke-dash: 3; font-size: 10 } }
  }
  s1.t -> sn.c: "PASS → 下一个" { style: { stroke: "#3a7d4a"; font-color: "#3a7d4a"; font-size: 10 } }
}
synthesize: "③ synthesize   (仅当 ≥1 PASS)" { class: phase-synth
  S: Synthesizer { class: agent }
}
stop: 止损 { class: stop }
done: "done | incomplete" { class: terminal }
goal -> analyze.A
analyze.A_out -> solve.s1.c: "subtasks[]" { style.font-size: 10 }
solve.sn.t -> synthesize.S: PASS { style: { stroke: "#3a7d4a"; font-color: "#3a7d4a"; font-size: 10 } }
solve.s1.t -> stop: 非PASS { style: { stroke: "#c0392b"; font-color: "#c0392b"; stroke-dash: 3; font-size: 10 } }
synthesize.S -> done
stop -> done: incomplete { style: { stroke: "#c0392b"; font-color: "#c0392b"; stroke-dash: 3; font-size: 10 } }
```

## 6. Render (or degrade gracefully)
```
if command -v d2 >/dev/null 2>&1; then
  d2 --sketch=false --pad 24 "$OUT/<name>.d2" "$OUT/<name>.svg"
else
  echo "d2 not installed — .d2 saved at $OUT/<name>.d2."
  echo "Install: curl -fsSL https://d2lang.com/install.sh | sh  (or paste into https://play.d2lang.com)."
fi
```
- Never pass `--watch` (starts a blocking server). Use `--layout elk` only if dagre
  crowds a dense graph.
- CJK labels render in any viewer that has a system CJK font (browsers, VS Code
  SVG preview). To EMBED the font so Chinese looks identical everywhere
  (rasterizers / PDF), add `--font-regular <cjk>.ttf` — d2 rejects `.ttc`, it needs
  a real `.ttf`.
- On a compile error d2 names the offending line — fix the `.d2` and re-run.

## 7. Report
Return the absolute `.d2` and `.svg` paths, the d2 exit status, and a one-line
summary (phases / agents / edges captured).
