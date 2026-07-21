# OpenCollab Design-Pattern Catalog

> **Purpose.** The code-first source of truth for the technical report *OpenCollab × Agentic Design Patterns*. Every exhibit carries a `file:line` anchor so the report is *assembled from the code*, not written alongside it — a claim a reader cannot point to in the tree does not appear here.
>
> **Two lenses per exhibit.** Each piece of tellable logic is tagged with (a) the classic software design pattern it uses — and whether that pattern is already **clean** (just name it), needs to be made **explicit** (a docstring/rename/extract-existing-duplication, *never* a new abstraction), or sits in **latent tension**; and (b) the Agentic Design Pattern (ADP, P1–P21) it *operationalizes*, with an honest coverage grade **● full / ◐ partial / ○ gap**.
>
> **Through-line.** OC's contribution is not breadth of coverage but **operationalization** (making the book's concepts concrete engineering invariants) and **eval-integrity** — captured in one ethic: *defend states, not races*.
>
> Generated 2026-07-14 from an 8-subsystem code-grounded audit (baseline `cc04a36`). Companion diagrams: `oc-blueprint/architecture.html`, `oc-blueprint/pattern-catalog.html`. Supersedes the coverage numbers in the 2026-07-04 report draft.

---

## 1. ADP coverage matrix (12 ● / 6 ◐ / 3 ○)

The credibility centerpiece. Every row is defensible from code.

| # | Pattern | Cov | OC evidence | One-line claim |
|--|--|:--:|--|--|
| P3 | Parallelization | ● | `pending.py:1-113` + `scheduler_lifecycle.py:407-513`; `workflow.py:1207-1244` | Both regimes fan out with per-slot fault isolation, preserving tool-call ordering |
| P4 | Reflection | ● | `self_collaboration.py:72-146`; `run_tests.py:537-566` | Two producer-critic channels: a spoof-resistant LLM reviewer and a deterministic test critic |
| P5 | Tool Use | ● | `tool_execution.py:254-443`; `domain/tools.py:56` | All six book steps + a 7th evidence stage + never-unanswered-`tool_call_id` invariant |
| P7 | Multi-Agent Collaboration | ● | `scheduler.py:51-68` + `workflow.py:206`; `domain/scheduler.py:86-124` | Two interchangeable schedulers over one Session process; full OS process model |
| P8 | Memory Management | ● | `context.py:71-151`; `shaping/eager.py:47-199`; `autosave.py:32-153` | Typed layered short-term memory + concurrency-correct episodic persistence |
| P11 | Goal Setting & Monitoring | ● | `session_run.py:741-784`; `event_bus.py:20-85` | An explicit progress goal monitored every step, driving corrective action |
| P12 | Exception Handling & Recovery | ● | `session.py:350-374`; `session_run.py:1416-1430`; `scheduler_lifecycle.py:222-240` | A first-class recovery taxonomy: graceful terminals, transactional rollback, swallow-to-None |
| P13 | Human-in-the-Loop | ● | `adapters/tools/human.py:1-70`; `ports.py:100-116` | Two real HITL ports (confirm gate + free-text ask) wired to the TUI, costing the FSM nothing |
| P15 | Inter-Agent Communication (A2A) | ● | `scheduler_messaging.py:28-92,166-249` | Full async A2A: envelope, topology gating, scheduler-mediated durable safe-drain inbox |
| P16 | Resource-Aware Optimization | ● | `domain/scheduler.py:21-45` + `_scheduler_persistence.py:121-254`; `session_run.py:587-628` | Budget is a first-class reservable resource with per-turn leases + a predictive guard |
| P18 | Guardrails / Safety | ● | `run_tests.py:537-566`; `adapters/safety.py:21-107`; `test_*_boundaries.py` | False-green-proof grading, a three-tier sandbox, executable architecture fitness functions |
| P19 | Evaluation & Monitoring | ● | `run_tests.py:537-566`; `session.py:427-481`; `trace.py:24-135` | Eval-integrity through-line: capture-can-never-fail evidence ledger + false-green-proof grading |
| P1 | Prompt Chaining | ◐ | `workflow.py:584-773,1224-1244` | Realized as the workflow pipeline structure, not a first-class prompt-chain primitive |
| P2 | Routing | ◐ | `domain/team.py:18-70`; `use_skill.py:27-65` | A static permission graph + catalog→name skill dispatch, not content-based LLM routing |
| P6 | Planning | ◐ | `scheduler_lifecycle.py:77-220`; `workflow.py:584-773` | Delegation + fixed workflow phases; no explicit planner module (decomposition lives in prompts) |
| P17 | Reasoning Techniques | ◐ | `openai_provider.py:197-203`; `session_run.py:1349-1390` | Provider-native reasoning as a default-off passthrough; OC does not orchestrate reasoning |
| P20 | Prioritization | ◐ | `shaping/pipeline.py:24`; `context.py:60-68` | Pin floor fully load-bearing; graduated shed-by-priority is decorative today |
| P21 | Exploration & Discovery | ◐ | `session.py:427-481` | A novelty sensor detects diminishing returns and brakes; no active novelty-*seeking* policy |
| P9 | Learning & Adaptation | ○ | — | **Future-work gap:** no cross-episode learning; snapshots persist but are never re-ingested |
| P10 | Model Context Protocol | ○ | — (`sdk/` is the analog) | **Deliberate non-goal:** in-process ToolSpec→OpenAI schema; the SDK is a protocol-boundary analog, not MCP |
| P14 | Knowledge Retrieval (RAG) | ○ | — (`repo_map.py:32-65`) | **Deliberate non-goal:** only a bounded startup repo map; no embeddings/vector store |

### Reconciliation vs the 2026-07-04 draft (9 ● / 8 ◐ / 4 ○)

- **Upgraded (5):** P8 Memory, P13 HITL, P15 A2A, P18 Guardrails (all ◐→●, hardened by PRs #20–22 + verified line-by-line); P21 Exploration (○→◐, the novelty sensor counts).
- **Downgraded (1):** P1 Prompt Chaining (●→◐) — the earlier draft over-claimed; OC's "chain" is the workflow pipeline structure, not a first-class chain primitive.
- Net movement is upward **and** code-grounded; the P1 correction *raises* credibility. **Use this matrix, not the draft's.**

---

## 2. Headline exhibits

The six the report leads with, in spine order.

1. **Two-regime multi-agent as OS processes** (P7 ●, beyond-book). One `session.run_loop()` process primitive driven by two interchangeable schedulers — an LLM-supervised event-driven `Scheduler` and a deterministic code-driven `WorkflowContext` (single-agent CLI = team-of-one; eval harness = workflow). Underneath: `SessionControlBlock`=PCB, `SessionTable`=process table, `spawn()`=transactional fork with parent linkage. `scheduler.py:51-68`, `workflow.py:206`, `domain/scheduler.py:86-124`.
2. **Budget as a first-class reservable resource** (P16 ●, beyond-book). Pure fan-out split policy (`domain/scheduler.py:21-45`) + per-turn leases that return only unspent headroom (`_scheduler_persistence.py:121-254`) + a `commit_reserve` carved from the cap, protected by a predictive EWMA feed-forward guard (`session_run.py:587-628`) + an aggregate defense-in-depth ceiling. A reservation, not a meter.
3. **Deterministic harness-grounded Reflection + eval-integrity** (P4+P19 ●, beyond-book). `run_tests` refuses GREEN without parser-backed positive proof that the *named* test executed and passed (`run_tests.py:537-566`, `_target_has_pass_proof:513-534`); unsupported runners are RED before execution because "a bare exit code is forgeable." Paired with the softer LLM critic (`ReviewVerdict` last-line-only parse). Certify the *state* of a green claim; never trust an adversary-forgeable signal.
4. **Deterministic evidence ledger, capture-cannot-fail** (P19 ●, beyond-book). `record_evidence_signal` folds each tool result into a novelty sensor + a harness-authored `{tool,target,outcome,snippet}` card built purely from the tool-result envelope with zero model in the loop (`domain/session.py:427-481`). A dead scout is salvaged from what it provably touched; novelty is keyed on hashes so a re-read at a shifted range scores zero gain — the "no progress" signal is ungameable.
5. **Context engineering: typed layered plan + lazy-degradation shaping** (P8+P16 ●, beyond-book). The prompt is an ordered tuple of tagged `ContextSource` value objects assembled generically over position (`domain/context.py:71-151`) — "new context = register a source." A Chain-of-Responsibility of shapers projects a bounded view over a *copy* while `state.messages`/the transcript keep everything (`shaping/__init__.py`, `pipeline.py`) — the eval-integrity thesis in miniature. The always-on eager clear stubs name the exact slice already read to defeat re-read thrash (`eager.py:47-199`).
6. **Fork-ability discipline: hexagonal ports + compact SDK Facade + fitness functions** (●, beyond-book). 21 structural Protocol ports invert every dependency (`ports.py`); bootstrap owns concrete assembly (`container.py`, `programmatic.py`); a four-name SemVer SDK Facade is frozen by an executable export/signature contract (`test_sdk_api.py`); and the dependency rule is a green CI check (`test_*_boundaries.py`). The edge is nailed so the interior refactors freely.

---

## 3. The catalog (grouped by subsystem)

Format: **exhibit** `file:line` — SW pattern *(state)* · ADP *(coverage)* · ★ = headline.

### 3.1 Session lifecycle & FSM

- ★ **Validated transition table** `session.py:76-116,331-348` — Table-driven State machine *(clean)* · P11 *(◐)*. The single authority on legal edges; `transition_to` raises `InvalidPhaseTransition`, `set_phase` is the deliberate out-of-band primitive, `fail`/`cancel` the two escapes. A test asserts table keys == enum (topology completeness). *The FSM cannot silently grow an illegal edge.*
- ★ **Terminal taxonomy → 3 behaviors** `session.py:37-46,350-374`; `session_run.py:1416-1430` — State + exception-as-controlled-signal *(make-explicit)* · P12 *(●)*. Six terminals resolve to DONE / four graceful stops (deliver a clean result to the parent) / ERROR (sole failure). `_ContextOverflowStop` is caught so an overflowed child returns DONE rather than crashing the parent. *Two current labels lie (loop-block→STEP_LIMIT, wind-down success→BUDGET_EXCEEDED) — see §8.*
- **`run_loop` skeleton** `session_run.py:354-374,431-454` — Template Method + State dispatch *(clean)* · P11 *(◐)*. `while not _should_suspend(): await advance()`; `advance()` is a match-on-phase dispatcher whose every handler exits via a validated `transition_to`.
- **`SessionState` as two jobs** `session.py:135-243` — Value Object fighting a bolted-on control plane *(latent-tension)* · P8 *(◐)*. Genuine short-term memory *and* ~14 enforcement fields; the per-turn/lifetime partition is prose comments, not type structure. *The single cleanest before/after for the clean-arch thesis once the enforcement fields extract to a value object.*
- **`_UserTurnCheckpoint` transaction** `session.py:119-133,376-406`; `scheduler_messaging.py:216-225` — Memento *(clean)* · P12 *(◐)*. A frozen snapshot lets an interrupted user-message append either fully commit or leave the transcript byte-identical.

### 3.2 Enforcement, steering & evaluation (the eval-integrity core)

- ★ **`precheck` guard chain** `session_run.py:786-839` — Chain-of-Responsibility with one shared terminal action *(make-explicit)* · P16+P11 *(●)*. cancel → loop-block → enforcement → per-session budget → team-aggregate ceiling → step limit → else `CALLING_LLM`; every halt funnels through `_stop_precheck`. `max_budget_tokens`/`max_steps` are session-lifetime caps.
- ★ **Sensor→Controller→Actuator wind-down** `session_run.py:741-784,587-628`; `session.py:427-482` — control loop, one Command *(latent-tension)* · P16+P11+P20 *(●)*. A single physical actuator (`_enter_wind_down`: narrow toolset + force `tool_choice` via constrained decoding) fired by four OR'd triggers. The always-on sensor is byte-identical whether enforcement is on or off (proven by test); only the *controller* is gated.
- **Predictive EWMA guard** `session_run.py:587-628` — Feed-forward controller with a capped, deadline-banded term *(clean)* · P16+P11 *(●)*. Keeps an EWMA of per-turn cost and trips wind-down one turn early so the protected submit turn fits the reserve carved from the cap.
- **Extension valve** `session_run.py:630-707`; `extension_valve.py:134-176` — Guard + predicate judge feeding the same actuator *(latent-tension)* · P20 *(◐)*, P13 *(analog only — not real HITL)*. One appeal turn: commit or justify one more read, granted only for a novel + concrete reason, hard cap 1. *The subsystem's one negotiation branch and clearest slimming candidate.*
- ★ **Evidence ledger, capture-cannot-fail** `session.py:427-481`; `tool_execution.py:107-135` — fold/Observer + append-only bounded Ledger *(clean)* · P19+P21+P11 *(●)*. One novelty predicate drives both the info-gain counters and a harness-authored ledger built purely from the tool envelope. Novelty keyed on hashes → the "no progress" signal is ungameable.
- ★ **`run_tests` positive-proof green** `run_tests.py:537-566,513-534` — Specification + per-runner proof Strategy *(make-explicit)* · P19+P4+P18 *(●)*. GREEN demands exactly one pytest summary line, zero failures, exit 0, `passed>0`, AND the requested node-id in the `-rA` PASSED list. Unsupported runners are RED before execution. *The deterministic critic that grounds reflection.*
- **Reads-without-write nudge** `session_run.py:1079-1137,1432-1466` — Monotone state machine + allowlist actuator *(clean)* · P11 *(◐)*, P18 *(◐)*. status-only → SOFT at 8 → HARD at 16 that sets `tool_choice='required'` AND physically blocks non-write tool calls.
- **Batch-loop monitoring** `tool_execution.py:60-68,154-196` — Observer/accumulator *(clean)* · P16 *(●)* / P19+P11 *(◐)*. Path-normalized, full-window loop detection (catches cyclic A,B,C thrash); the evidence sensor/ledger/watchdog here are deliberately observational.

### 3.3 Orchestration — the two regimes

- ★ **Two schedulers over one Session** `scheduler.py:51-68`; `workflow.py:206-207` — Strategy (Session as Context) *(make-explicit)* · P7+P3 *(●)*. An event-driven LLM-supervised team and a deterministic code-driven pipeline drive the identical process primitive. *The report's organizing metaphor.*
- ★ **`SessionControlBlock`=PCB, `spawn`=fork** `domain/scheduler.py:86-124`; `scheduler_lifecycle.py:77-240` — Value Object + Registry + transactional Factory *(clean)* · P7+P6+P12 *(●)*. `aid`=pid, `parent_aid`=ppid; the whole reservation→`create_task` span is one transaction — any failure rolls back budget lease + worktree + maps so a half-forked child never leaks.
- ★ **Budget as reservable quota** `domain/scheduler.py:21-45`; `_scheduler_persistence.py:121-254` — Policy/Strategy + Lease ledger *(make-explicit)* · P16+P7+P20 *(●)*. Pure fan-out split + per-turn leases that record `used_tokens` at grant so consumption replaces reserved headroom; releasing a terminal turn returns only unspent grant. *Make-explicit: unify `_reservation`/`_turn_budget` vocabulary to one "lease" term.*
- **Topology: one policy, two verbs** `domain/team.py:18-70`; `_scheduler_team.py:28-34`; `scheduler_messaging.py:46-52` — Strategy/Policy + Guard *(clean)* · P15+P7 *(●)*, P2 *(analogy)*. The same directed who-may-talk-to-whom graph gates both spawn and `send_message`; a denial becomes a tool result, not a crash. *Guard currently written twice — single-source it (slimming S2).*
- **Mediated A2A inbox** `scheduler_messaging.py:28-92,166-249` — Mediator + Guarded Suspension *(clean)* · P15+P7 *(●)*. Agents hold only aids; a message becomes a user turn in an XML envelope, parked in a durable out-of-history inbox that drains only when the recipient can safely accept a turn.
- **`run_spawn_with_review` reflection loop** `self_collaboration.py:72-146`; `domain/scheduler.py:62-83` — Template Method + Value Object + Port *(clean)* · P4+P7+P18 *(●)*. Coder→Reviewer; `ReviewVerdict.parse` reads only the last non-empty line (`re.fullmatch` of `VERDICT: PASS`) so a quoted/superseded verdict cannot fake PASS.
- **Five workflow runners** `workflow.py:584-773` — Template Method, open-coded 5× *(latent-tension)* · P1+P6+P12 *(●)*. All repeat build→track→run→swallow; P12 fully realized as per-call swallow-to-None ("one dead agent never kills the fleet"). *The planned collapse makes the Template Method explicit — deletion, not a new layer.*
- **`parallel` / `pipeline`** `workflow.py:1207-1244` — Pipeline + error-boundary Decorator *(clean)* · P3+P12 *(●)*. Semaphore-bounded fan-out + a no-barrier concurrent pipeline with per-slot fault isolation — the deterministic mirror of the pending-row parallelism.
- **Frozen pending-row wait queue** `session_run.py:928-1004`; `pending.py:1-113` — Guarded Suspension + frozen rows (Memento) + Observer *(clean)* · P3+P7+P15 *(●)*. A deferred spawn parks the parent in `AWAITING_EVENTS` with zero half-written history; children run in parallel and the parent wakes only when the whole batch is answered, in original order.

### 3.4 Context assembly & shaping

- ★ **Typed layered `ContextPlan`** `context.py:71-151`; `context_builder.py:103-210` — Builder + Value Object *(clean)* · P8 *(●)*. An ordered tuple of tagged sources assembled generically over `ContextPosition` — no assembly method inspects `ContextLayer`, keeping "new context = register a source" literally true. Each message stamped with a `_ctx` provenance tag.
- ★ **Lazy-degradation shaping chain** `shaping/__init__.py`; `pipeline.py:117-131`; `reactive.py:47-269` — Chain-of-Responsibility + Template Method *(latent-tension)* · P8+P16 *(●)*. Seven shapers run cheapest-first over a *copy* while the transcript keeps everything. *After deleting the 2 inert rungs (LowPriorityContextShed, ContextCollapse) it is a clean 5-rung ladder where every rung provably fires.*
- ★ **Always-on eager clear** `shaping/eager.py:47-199` — Strategy/Decorator projection; pure stub-builder *(clean)* · P8+P16 *(●)*. Keeps K=12 recent tool results verbatim, replaces older ones with a deterministic stub that names the exact slice already read — turning "old output cleared" into a re-read deterrent. Deterministic + monotonic + idempotent (tested) so the deep prefix stays cacheable.
- **Prioritization: pin floor** `context.py:60-68`; `pipeline.py:24,171-206` — Strategy + threshold policy *(make-explicit)* · P20 *(◐)*. `PIN_FLOOR=70` makes identity/team/skill/task provably un-sheddable; graduated shed-by-priority is decorative until long-term sources load.
- **Forced-shape overflow recovery** `pipeline.py:73-114`; `session_run.py:1283-1308` — Template Method with temporary-mode override + retry-once *(clean)* · P12+P16 *(●)*. A real provider overflow flips every rung's `_forced` flag to compact unconditionally, retries once, then falls through to a graceful `_ContextOverflowStop` when the pinned seed alone overflows. *"Defend states not races" applied to overflow.*
- **Group-span compaction guard** `pipeline.py:134-168` — shared span-partition Guard *(clean)* · P18 *(◐)*. Compaction operates on call/answer *groups*, never a lone message, so it can never orphan a `tool_call_id`.
- **Lazy-loader scaffold (unbuilt)** `context.py:39-45,149-151`; `context_builder.py:167-209` — Null Object / reserved-slot placeholder *(latent-tension)* · P8-long-term + P14 *(○)*. `LoadTiming` futures, `loader_key`, `deferred_sources()` inject no content; the only retrieval is `build_repo_map`. *Deleting it (slimming S3) converts a cluttered "partial scaffold" into an honest gap.*

### 3.5 Tools, delegation & safety

- ★ **Six-step Tool-Use loop** `tool_execution.py:254-443` — Command + Unit-of-Work *(clean)* · P5 *(●)*. Definition (`to_openai_schema`) → Decision → whole-batch all-or-nothing preflight → Execution → Observation (every `tool_call_id` answered, failures answer with a string) → Processing (`apply_to`) + a 7th evidence stage.
- ★ **Delegation as an ordinary tool** `adapters/tools/spawn.py:53-81`; `tool_execution_runtime.py:49-53` — Command + Future/Promise *(clean)* · P7+P15+P5 *(●)*. `spawn_agent` returns a `DeferredCall`; the child's result is injected as this tool call's result, so the FSM needs zero new states. `message_agent`/`team_status` ride the same tool path.
- **Three-tier command/path sandbox** `adapters/safety.py:21-107`; `fs.py:82,190,332` — Strategy/Policy + Chain *(latent-tension)* · P18 *(●)*, P13 *(risky-confirm)*. BLOCKED hard-raise, RISKY confirm-gate, `check_path` realpath-jails every path. *Cooperative (each tool calls the policy) — `git_diff` omits it; extract one `_checked_path` helper to reveal the seam AND close the gap.*
- **Bounded tool interceptor** `tool_execution_runtime.py:73-130,186-218` — Decorator + Template Method *(clean)* · P12+P16 *(●)*. Per-tool deadline; PermissionError/Exception → a string; timeout runs a graduated cancel→grace→revoke→abort so cleanup is itself time-bounded.

### 3.6 LLM provider boundary & reasoning

- ★ **kimi tool-call markup recovery** `openai_provider.py:77-141,156-210`; `types.py:31-36` — Adapter + fallback Chain-of-Responsibility *(clean)* · P5+P12+P19 *(●)*. Recovers malformed provider markup (content → reasoning), validates each block's JSON, and *counts* every recovery (`markup_recovered`) — a silent function-calling failure becomes a self-alarming repair.
- ★ **Honest `Usage` value object** `anthropic_provider.py:160-186`; `openai_provider.py:213-249`; `types.py:15-40` — Adapter (two schemas → one) + Value Object *(clean)* · P16+P19 *(●)*. Sums uncached+cache_read+cache_creation for Anthropic, adds nothing for OpenAI-compat (already included), estimates when a proxy zeroes usage — so the budget meter is provably conservative.
- ★ **Default-off thinking passthrough** `config.py:18-20`; `openai_provider.py:197-203`; `session_run.py:1349-1390` — Strategy + flag-guarded Null-Object default *(make-explicit)* · P17 *(◐)*, P12. Off == byte-identical request; on, each provider translates differently and the CoT is retained for the trajectory + reused to rescue empty turns. *OC enables and records reasoning; it does not orchestrate it.*
- **Fault taxonomy: retry vs force-compact** `anthropic_provider.py:16-39`; `retry.py:25-96`; `errors.py:68-85` — Adapter + graceful-degradation Retry *(clean)* · P12+P16 *(●)*. Distinguishes transient (retry w/ Retry-After), non-transient-recoverable (session-layer force-compact + retry once), and terminal degradation; refuses to retry the one non-transient 400 (overflow).
- **`Agent` as stateless config** `domain/agent.py:17-67`; `client.py:33-152` — Strategy + Value Object + Facade *(make-explicit)* · P7+P17+P5 *(●)*. Every agent is a stateless config record reused across sessions; one `complete()` facade hides two SDKs, provider chosen once.

### 3.7 Observability, persistence & lifecycle hooks

- ★ **Isolated fan-out `EventBus`** `event_bus.py:20-85`; `domain/events.py:24-70` — Observer + Mediator + Factory *(clean)* · P19+P15 *(●)*. One bus fans a `(type,data)` event to N heterogeneous sinks with per-sink `try/except`, propagating cancellation via `asyncio.shield`. *The substrate the trace/log/autosave/hooks exhibits all hang on.*
- ★ **Freeze-then-flush autosave** `autosave.py:32-153`; `storage.py:46-143` — Command + Memento + ordered-queue *(make-explicit)* · P8 *(●)*. Deep-copies a self-consistent snapshot on the loop, flushes off-thread in strict submission order under subscriber ownership; atomic temp-then-rename write. *Within-episode persistence, NOT cross-episode learning.*
- **Fail-soft `Tracer`** `trace.py:24-135` — Observer sink + latch-and-drop state machine *(make-explicit)* · P19 *(●)*. On the first write error it latches, closes, and thereafter *counts* dropped steps instead of raising — observability that refuses to either lie or crash.
- **Lifecycle hooks (observe-only)** `domain/hooks.py:43-100`; `adapters/hooks.py:21-84` — Value Object + Specification + Strategy dispatch + Adapter *(clean)* · P13 *(◐)*, P18. Mirrors Claude Code's hook vocabulary; phase-1 is observe-only (the `HookOutcome.allow` deny seam is forward-declared but unbuilt). *The report must not claim hooks currently block tool calls.*
- **On-demand skills** `domain/skill.py:17-30`; `use_skill.py:27-65` — Strategy + Null Object + Dispatcher *(clean)* · P8 *(◐)*, P5, P2. A permission-gated catalog + a single generic `use_skill(name)` that pulls the body in as a tool result; two-phase lazy load. Human-curated, not learned.

### 3.8 Architectural boundaries (why OC is fork-able)

- ★ **21 hexagonal Protocol ports** `ports.py:14-443` — Ports & Adapters / Dependency Inversion *(clean)* · P5+P13+P8+P18 *(●)*. Adapters conform by shape (no inheritance); `test_*_boundaries.py` enforce it at CI. Two ports encode eval-integrity policy in their contract (`WorkingTreeProbe`: unknown-not-block; `AskUserPort`: presence-means-reachable).
- ★ **Composition root** `container.py:204-301` — Composition Root / DI + Abstract Factory *(clean)* · P7+P16+P8 *(◐)*. The only place `LLMClient` is instantiated; a PEP 562 `__getattr__` breaks the factory import cycle; factory ports let the scheduler spawn fully-wired sessions without importing `Session`.
- ★ **Compact SDK Facade** `sdk/client.py`; `sdk/result.py`; `bootstrap/programmatic.py`; `test_sdk_api.py` — Facade + Value Object + Composition Root *(clean)* · P10 *(◐)*. Four root names expose three symmetric regimes and one result model; optional contracts live in three tiny capability modules. Package SemVer is the sole compatibility version, while tamper-evident lifecycle evidence remains bootstrap-owned.
- ★ **Architecture fitness functions** `test_application_boundaries.py`; `test_domain_boundaries.py`; `test_sdk_boundaries.py` — Test-as-Specification *(clean)* · P18+P19 *(◐)*. The dependency rule is an executable test (inner may not import outer; SDK may not import eval/harness; retired shims stay deleted) — "stays fork-able" is a green check, not a hope.

---

## 4. Software-pattern inventory

The classic patterns OC leans on. **make-explicit** = a docstring / rename / extract-existing-duplication only, never a new abstraction.

| SW pattern | Where | Verdict | Light action (if any) |
|--|--|:--:|--|
| State machine (table-driven) | `session.py:76-116`; `session_run.py:1079-1137` | clean | — |
| Template Method | `session_run.py:354-374`; `self_collaboration.py:72-146`; `workflow.py:584-773` | make-explicit | Collapse the five open-coded workflow runners into one `_run_tracked_session(...)` helper |
| Strategy / Policy | two schedulers; `team.py:18-70`; `agent.py:17-67`; `context.py:91-96` | make-explicit | One-line docstring per scheduler: "one of two Strategies driving `session.run_loop()`" |
| Command | `tool_execution.py:254-443`; `autosave.py`; `session_run.py:525-544` | clean | — |
| Chain-of-Responsibility / Pipeline | `session_run.py:786-839`; `shaping/pipeline.py`; `openai_provider.py:156-210` | make-explicit | Docstring `precheck()` as a guard chain (each guard halts-via-`_stop_precheck` or passes) |
| Memento | `session.py:119-133`; `pending.py:39-104`; `autosave.py` | clean | — |
| Value Object | `scheduler.py:86-124`; `context.py:71-96`; `llm/types.py:15-40`; `sdk/result.py` | make-explicit | Extract the ~14 enforcement fields off `SessionState` into a `TurnEnforcementState` VO |
| Builder | `context_builder.py:103-210`; `scheduler_factory.py` | clean | — |
| Adapter / Anti-Corruption Layer | `llm/client.py:33-152`; `openai_provider.py:77-141`; `storage.py:95-115` | clean | — |
| Facade | `llm/client.py:33-152`; `sdk/client.py`; `sdk/__init__.py` | clean | Four root exports; package SemVer and `test_sdk_api.py` define the contract |
| Observer / Pub-Sub | `event_bus.py:20-85`; autosave + trace + hooks | clean | — |
| Mediator | `scheduler_messaging.py:28-92`; `event_bus.py` | clean | — |
| Registry / Identity-Map + Abstract Factory | `scheduler.py:86-124`; `tool_registry.py`; `ports.py:193-257` | clean | — |
| Guarded Suspension / Future-Promise | `pending.py`; `tool_execution_runtime.py:49-53` | clean | — |
| Specification / Guard | `run_tests.py:537-566`; `errors.py:68-85`; `hooks.py:81-100` | make-explicit | Docstring `_is_green` as the "positive-proof specification"; name the runner branch a proof-adapter Strategy |
| Ports & Adapters (Hexagonal) / DI | `ports.py:14-443`; `container.py:204-301` | clean | — |
| Null Object | `null_skill_store.py:12-22`; `human.py:42-70`; `context.py:149-151` | clean | — |
| Repository + Decorator | `storage.py:46-143`; `session.py:471-481` | clean | — |
| Architecture Fitness Function | `test_*_boundaries.py`; `test_sdk_api.py` | clean | — |

---

## 5. Report outline (concept spine)

| § | Thesis | Draws from |
|--|--|--|
| 1. The Session as a validated FSM | A table-driven FSM whose one authority on legal moves is a declarative table with a topology-completeness test, driven by a Template-Method `run_loop`. | §3.1; P11, P12 |
| 2. Multi-agent orchestration as an OS process model | Hierarchical collaboration as OS processes: SCB=PCB, `SessionTable`=process table, `spawn`=transactional fork; delegation and A2A are ordinary tools whose async results are Futures — the FSM stays untouched. | §3.3; P7, P15, P3, P6 |
| 3. Context engineering: memory as a typed, projected, cacheable resource | A typed layered immutable `ContextPlan` bounded before each call by a lazy-degradation Chain-of-Responsibility over a copy while the transcript keeps everything. | §3.4; P8, P16, P20, P18 |
| 4. Two interchangeable regimes over one process | The same `run_loop` primitive driven by an event-driven `Scheduler` or a code-driven `WorkflowContext` — Strategy over one Context; each realizes parallel fan-out with per-slot fault isolation. | §3.3; P7, P3, P1, P12 |
| 5. Enforcement, budget & reflection: the eval-integrity core | Budget as a reserved resource; a Sensor→Controller→Actuator loop with always-on sensors; two grounded critics; an ungameable evidence ledger; a provably-conservative meter. *Defend states, not races.* | §3.2, §3.6; P16, P11, P4, P19, P20, P12, P5 |
| 6. Provider boundary & fault taxonomy | One `complete()` facade over two SDKs; a default-off reasoning passthrough; a fault taxonomy that never lets degradation mask a real fault. | §3.6; P5, P17, P12, P16, P19 |
| 7. Observability, persistence & lifecycle hooks | All side effects through one isolated fan-out bus; episodic memory as a concurrency-correct freeze-then-flush ledger; fail-soft trajectory recorder; observe-only hooks. | §3.7; P19, P8, P13, P18 |
| 8. Architectural boundaries: why OC is fork-able | 21 Protocol ports invert every dependency; one composition root; a test-frozen versioned SDK Facade; the dependency rule as a green fitness function. | §3.8; P10 (analog), P18, P19 |
| 9. Honest boundaries (the ○ story) | OC deliberately omits MCP and RAG and does not learn across episodes; exploration is detected, not sought. Distinguishing deliberate non-goals from future work protects the operationalization thesis. | §6 below; P9, P10, P14, P21 |

---

## 6. Honest boundaries (the ○ story)

**Deliberate non-goals** (intentional scope, not deficiencies):
- **P10 MCP** — tool access is in-process `ToolSpec` rendered to OpenAI schemas; a repo-wide grep for MCP is empty. The versioned SDK Facade is OC's *analog* of a standardized protocol boundary — not the MCP wire standard (no JSON-RPC, no tool servers).
- **P14 RAG** — the only retrieval is `build_repo_map` (`repo_map.py:32-65`), a deterministic depth/size-bounded startup directory walk. No embeddings, vector store, or query-time semantic lookup. The lazy-loader scaffold is designed-but-unbuilt and slated for deletion, which turns a cluttered "partial scaffold" into an honest gap.

**Future-work gaps** (candid "not yet," with evidence it was investigated):
- **P9 Learning & Adaptation** — no cross-episode learning on main: no notes/recall tool, no lesson distillation, no carried-forward policy/prompt/weight update. Episodic snapshots and trajectory JSONL persist but are never re-ingested. The one experimental bridge (KOCO practice memory) lives only on `feat/koco-graph-seed`, was found oracle-leakage-prone by construction, and showed zero pass@1 lift. *Cite only as candid future-work.*
- **P21 Exploration** (◐, future-leaning) — the novelty sensor operationalizes "exploration exhausted" (low-yield detection) and drives a commit brake, but there is no active novelty-*seeking* policy.

**Partials worth naming honestly** — P20 (pin floor realized, priority-shed designed-not-built), P17 (passthrough + CoT retention only, no orchestrated reasoning), long-term P8 (within-episode resume only). The ~14-field enforcement control plane on the domain `SessionState` is a **debt** (latent-tension), not an ADP gap — the cleanest before/after exhibit once extracted.

---

## 7. Docstring plan (code stays the report's source of truth)

All actions are docstring / rename / extract-existing-duplication **only** — never a new abstraction — folded into the slimming stages so documentation lands with the deletion that sharpens it. (Stages per `project_slimming_plan`.)

- **S1 (LLM/provider + tools):** docstring `_is_green` as the positive-proof specification; extract the duplicated empty-turn rescue guard into one named rung; extract `adapters/safety.py`'s copy-pasted `check_path` into one `_checked_path` helper the file tools share (reveals the interceptor seam **and** closes the `git_diff` gap).
- **S2 (scheduler/workflow):** one-line module docstring on each scheduler ("one of two Strategies driving `session.run_loop()`"); docstring `precheck()` as a guard chain; rename `_reservation`/`_turn_budget` to one "lease" term; extract the five open-coded workflow runners into one `_run_tracked_session(...)` Template Method.
- **S3 (context/shaping):** delete the 2 inert shaper rungs so the ladder is a clean 5-rung chain; delete the lazy-loader scaffold so long-term-memory/RAG reads as an honest gap; docstring `LAYER_PRIORITY` (only `PIN_FLOOR` is load-bearing today).
- **S4 (domain VO — the headline before/after):** extract the ~14 enforcement fields off `SessionState` into a `TurnEnforcementState` value object (pure regrouping, no new behavior) — makes "enforcement off == byte-for-byte reference" *structural*. Interim: group the fields under `=== per-turn ===` / `=== session-lifetime ===` banners.
- **SDK (completed in 0.4):** the request DTO graph and obsolete submodules are deleted; `test_sdk_api.py` locks the four-name facade and `test_sdk_boundaries.py` keeps concrete adapters and eval policy out.
- **Already clean — name in prose, touch no code:** the FSM table, `pending.py`, `_UserTurnCheckpoint`, the six-step tool loop, `event_bus.py`, `ports.py`, `container.py`, `test_*_boundaries.py`.

---

## 8. Seams surfaced (fold into slimming)

Two real defects the catalog surfaced while mapping patterns:

1. **Two terminal labels lie.** A loop-block halt is labeled `STEP_LIMIT_EXCEEDED` and a *successful* wind-down is labeled `BUDGET_EXCEEDED` (`session_run.py:755,801`). Collapsing the four graceful terminals into `STOPPED(reason)` (slimming S1) makes the reason explicit and **fixes this honesty bug** as a side effect.
2. **`git_diff` skips the path jail.** Every other filesystem tool calls `check_path`; `git_diff` omits it. Extracting the shared `_checked_path` helper (the §7 S1 make-explicit action) closes the seam.
