---
name: sequence-diagram
description: Draw a polished UML sequence / swimlane diagram (时序图 / 泳道图) of a software-engineering interaction. Use whenever the task asks to diagram, chart, visualize, or draw a flow over TIME between components — an API request, login/auth flow, checkout/payment saga, message-queue pipeline, RPC call chain, deploy sequence — from a description or from source you read. Produces a styled .d2 (d2's shape: sequence_diagram) rendered to .svg.
---

Turn a software-engineering interaction — an API request, an auth/login flow, a
checkout saga, a queue pipeline, a deploy sequence — into a STYLED UML sequence
diagram (时序图 / 泳道图, lifelines = lanes) using d2's native
`shape: sequence_diagram`, then render it to SVG. Input is either a
natural-language description or source you read as TEXT. You already have `bash`
(and likely `file_read`); this grants no new tools. The shell cwd does NOT
persist between bash calls — use ABSOLUTE paths.

## 1. Paths
- `ROOT=$(git rev-parse --show-toplevel)`
- `OUT="$ROOT/.opencollab/diagrams"; mkdir -p "$OUT"` — gitignored, never pollutes
  the repo. Write the `.d2` and `.svg` here.
- `<name>` = a short kebab slug of the flow (e.g. `login-flow`, `checkout`).

## 2. Gather the interaction (do NOT import code)
From the user's description, or read source as TEXT (`file_read`/`cat`) — never
import/run it. Identify, IN ORDER:
- **participants** (the lanes): components that exchange messages — client,
  gateway, services, datastores, queues, external/3rd-party APIs. List them
  LEFT→RIGHT in the order a request first touches them.
- **messages**, top-to-bottom in time: who calls whom, each a short verb phrase
  (`POST /login`, `verify(creds)`); tag each as a call, a return, or an async event.
- **combined fragments**: branches (`alt` success/error), optional steps (`opt`),
  repeats (`loop`/retry — read the cap), concurrency (`par`).
- **notes**: side-effects, latency, security caveats worth pinning to a lane.
- **activations**: which call keeps a participant "busy" (the call stack).

Runtime values (retry counts, item N) are unknown from source — label loops with
the cap/condition and draw 1–2 representative branches. The diagram shows the
PROTOCOL, not one actual run.

## 3. Style header — paste VERBATIM as the top of every .d2
```
vars: { d2-config: { theme-id: 0; sketch: false; pad: 28 } }
shape: sequence_diagram
classes: {
  actor:    { style: { fill: "#e7eefb"; stroke: "#3b5fa0"; stroke-width: 2; font-size: 14 } }
  service:  { style: { fill: "#e8f5ec"; stroke: "#3f8c5a"; stroke-width: 2; font-size: 14 } }
  external: { style: { fill: "#efeaf7"; stroke: "#7a5bb0"; stroke-width: 2; font-size: 14 } }
  store:    { shape: cylinder; style: { fill: "#fff4dc"; stroke: "#c79a2f"; stroke-width: 2; font-size: 14 } }
  note:     { style: { fill: "#fbf7e8"; stroke: "#c0ad6a"; font-size: 12; italic: true } }
  call:     { style: { stroke: "#2c3e6b"; stroke-width: 2; font-size: 12 } }
  return:   { style: { stroke: "#8693ad"; stroke-width: 1; stroke-dash: 4; font-size: 12; font-color: "#5a6a86" } }
  async:    { style: { stroke: "#b25b00"; stroke-width: 2; font-size: 12; font-color: "#8a4600" } }
  ok:       { style: { stroke: "#2f8f4e"; stroke-width: 2; font-size: 12; font-color: "#2f8f4e" } }
  err:      { style: { stroke: "#c0392b"; stroke-width: 2; stroke-dash: 3; font-size: 12; font-color: "#c0392b" } }
}
```

## 4. Mapping — construct → syntax
**Order matters twice**: actor columns = order of first appearance (so declare
EVERY participant up front, before any edge); messages render top-to-bottom in
declaration order. Never set `direction` — time always flows down.

Participant: `id: "Label" { class: NAME }` — pick the lane class by role:
- client / end-user → `actor`   · your service or gateway → `service`
- third-party / external system → `external`   · database / cache → `store`
- (reuse one class for many lanes of the same kind; `\n` makes a 2-line label)

Message: `from -> to: "verb phrase" { class: NAME }`
- synchronous call / request → `call` (solid)   · response / return → `return` (thin dashed)
- success branch → `ok` (green)   · error / failure → `err` (red dashed)
- async event / fire-and-forget / webhook → `async` + open arrowhead:
  `{ class: async; target-arrowhead: { shape: triangle; style.filled: false } }`
- self-call (internal step) → `api -> api: "verify signature"`

Activation (busy bar / call stack): give the caller a 1-letter span child and
route the call through it — `order.t -> db: ...` then `db -> order.t: ...`; nest
deeper for inner calls (`order.t.u`).

Note: `lane."text" { class: note }` — a standalone quoted child pins a note to
that lane at that moment in time.

Fragment (group): a CONTAINER whose label is the frame label, wrapping messages:
- `loop: "until shipped" { ... }`   ·   `opt: "if coupon" { ... }`
- `alt: { ok: "valid" { ... }   bad: "invalid" { ... } }` — each child container is one alternative
- `par: { a: "branch A" { ... }   b: "branch B" { ... } }`
Fragments nest.

## 5. Worked example (checkout) — append BELOW the header, swap in real topology
```
browser:  "Browser\n(顾客)"        { class: actor }
api:      "API Gateway"            { class: service }
order:    "Order Service"          { class: service }
pay:      "Payment Gateway\n(外部)" { class: external }
db:       "Orders DB"              { class: store }

browser -> api: "POST /checkout" { class: call }
api -> order.t: "createOrder(cart)" { class: call }
order.t -> db: "INSERT order (PENDING)" { class: call }
db -> order.t: "order_id" { class: return }
order.t -> pay.p: "charge(order_id, amount)" { class: call }
pay.p."PCI vault · 3-D Secure" { class: note }
alt: "payment result" {
  paid: "captured" {
    pay.p -> order.t: "receipt" { class: return }
    order.t -> db: "UPDATE order = PAID" { class: ok }
    api -> browser: "200 + order page" { class: return }
    order.t -> pay: "emit order.paid" { class: async; target-arrowhead: { shape: triangle; style.filled: false } }
  }
  declined: "card declined" {
    pay.p -> order.t: "402 declined" { class: err }
    order.t -> db: "UPDATE order = FAILED" { class: err }
    api -> browser: "402 + retry" { class: err }
  }
}
loop: "until shipped (webhook)" {
  pay -> api: "status webhook" { class: async; target-arrowhead: { shape: triangle; style.filled: false } }
  api -> api: "verify signature" { class: call }
}
```

## 6. Render (or degrade gracefully)
```
if command -v d2 >/dev/null 2>&1; then
  d2 --sketch=false --pad 28 "$OUT/<name>.d2" "$OUT/<name>.svg"
else
  echo "d2 not installed — .d2 saved at $OUT/<name>.d2."
  echo "Install: curl -fsSL https://d2lang.com/install.sh | sh  (or paste into https://play.d2lang.com)."
fi
```
- Never pass `--watch` (starts a blocking server). Sequence diagrams use a fixed
  layout — do NOT pass `--layout elk/tala`; it is ignored or errors.
- CJK labels render in any viewer with a system CJK font (browsers, VS Code SVG
  preview). To EMBED the font so Chinese looks identical everywhere (rasterizers /
  PDF), add `--font-regular <cjk>.ttf` — d2 needs a real `.ttf`, not `.ttc`/`.otf`.
- On a compile error d2 names the offending line — fix the `.d2` and re-run.

## 7. Report
Return the absolute `.d2` and `.svg` paths, the d2 exit status, and a one-line
summary (participants / messages / fragments captured).
