# OpenCollab Architecture Survey — 2026-06-10

Multi-agent survey with adversarial verification of every finding. All claims below were
independently confirmed in code unless noted; one finding was refuted and is listed in the
appendix. Repo state: `main` @ 816f365 (post code-clarity refactor).

**Overall verdict: healthy.** The recent refactor toward Clean Architecture succeeded. The
domain layer is genuinely pure, the application layer imports only domain + stdlib, ports are
real, and boundary tests enforce the import direction. The serious problems are concentrated at
the periphery (one verifiably broken SWE-bench script) and in retired-but-still-wired legacy
paths. No layer violations were confirmed anywhere in the core package.

---

## 1. Current-state report

### 1.1 Module / layer map

| Subsystem | Layer | Role | .py files | Public surface (key names) |
|---|---|---|---|---|
| `opencollab/domain/` | Domain (innermost) | Pure value objects + FSM: Agent, SessionState/SessionPhase FSM, PendingEventTable, SCB/SessionTable, events, hooks, Topology, ContextPlan, ToolSpec, CompactResult | 11 (~1,016 LOC, largest 282) | `SessionState`, `SessionPhase`, `PHASE_TRANSITIONS`, `Agent`, `Topology`, `PendingEventTable`, `ToolSpec`, `SessionRuntimeEvent`, `SchedulerEvent` |
| `opencollab/application/` (+`shaping/`) | Application (use cases + ports) | Session run-loop FSM, tool-batch execution, mixin-composed Scheduler, messaging, compaction (two generations), read-time shaping pipeline, EventBus/autosave/hooks subscribers, all port Protocols | 18 + 4 | `Session`, `SessionRunUseCase`, `ToolExecutionUseCase`, `ToolRuntime`, `Scheduler`, `LaunchSpec`, `EventBus`, `ShaperPipeline`, all `*Port` Protocols in `ports.py` |
| `opencollab/adapters/tools/` | Adapter | 13 Tool classes (bash, fs, apply_patch, git_diff, run_tests, ask_user, messaging, spawn) on a small `base.Tool` contract | 11 | All 13 tool classes (consumed via deep imports from `bootstrap/tool_registry.py`, `harness/evaluator.py`, `swebench/gen_prediction.py`) |
| `opencollab/adapters/llm/` | Adapter | LLMClient facade, per-provider modules (OpenAI-compat, Anthropic), retry policy, DTOs, model catalog (596 LOC) | 6 | `LLMClient`, `LLMResponse`, `Usage`, `estimate_messages_tokens`, `model_context_window` |
| `opencollab/adapters/cli/` | Adapter (entry) | Typer app, REPL, config/arg merge, toolbar, headless eval subcommand (~390 LOC) | 5 | `app` (console script), `main` (`python -m opencollab`) |
| `opencollab/adapters/tui/` | Adapter | Rich Live renderer (events mixin + display mixin), EventPublisherPort/PermissionPort bridges (597 LOC) | 5 | `TUI`, `TuiEventSink`, `TuiPermissionPolicy` |
| `opencollab/adapters/` (top-level misc) | Adapter | Environments (local/worktree/docker), worktree pool, safety policy, shell hooks, JSON session store, JSONL tracer | 7 | `LocalEnvironment`, `DockerEnvironment`, `SandboxInterceptor`, `Tracer`, `SessionStore`, `ShellHookRunner`, `WorktreePool` |
| `opencollab/bootstrap/` | Composition root | Config/env resolution (pydantic), team YAML, tool registry, ContextBuilder, session/spawn/lead factories, scheduler assembly | 9 | `get_config`, `build_config`, `build_runtime_context`, `build_scheduler`, `build_session`, `agent_save_path`, `make_run_dir`, `DefaultSessionFactory`, `TeamConfig` |
| `opencollab/harness/` | Outermost driver | Headless eval runner (199 LOC): env provision, fixed agent+tools, single/batch run, git-diff patch extraction | 2 | `EvalTask`, `run_eval_batch`, `save_results` (sole consumer: `adapters/cli/eval.py`) |
| `swebench/`, `scripts/` (peripheral) | Out-of-package drivers | SWE-bench prediction generation (`gen_prediction.py`), batch run+grade (`run_swe_docker.py`, **broken**), DashScope smoke test | 3 | None (entry points only; nothing imports them) |

Tests: 39 `test_*.py` files in `opencollab/tests/`, including explicit architecture-boundary
tests (`test_application_boundaries.py`, `test_domain_boundaries.py`, `test_tool_runtime_contract.py`).

### 1.2 Dependency graph

**Inferred layer rules (all observed to hold, and test-enforced for the inner layers):**

- `domain` → nothing internal outside `domain/`; stdlib only, I/O-free. Verified: zero outbound deps.
- `application` → `domain` + stdlib only. Owns the port Protocols (`application/ports.py`). Zero
  third-party or adapter imports — enforced by regex in `test_application_boundaries.py`.
- `adapters` → `application` + `domain` (implement the ports). Sanctioned exception:
  `adapters/cli/` is the process entry point and additionally imports `bootstrap` and `harness`.
- `bootstrap` → everything (only place that knows concrete types; only construction site of `LLMClient`).
- `harness` → `bootstrap` + `adapters` + `application` + `domain` (outermost; single inbound consumer `adapters/cli/eval.py`).
- `__main__` → `adapters.cli` only.

**Layer violations: none confirmed.** Two duck-typed escapes evade (rather than violate) the
import-regex boundary tests and are tracked as port-surface gaps, not violations:
`application/scheduler.py:202` probes `getattr(env, "get_diff", None)` (a `WorktreeEnvironment`-only
method not on `EnvironmentPort`), and `application/session_run.py:94` calls `tracer.flush()` which
`TracePort` does not declare.

**Cycles (2, both benign as wired):**

1. `application.ports` ⇄ `application.tool_execution` — **type-only**, not a runtime cycle. The
   back-edge is inside `if TYPE_CHECKING:` with string annotations; both import orders verified to
   succeed. Cosmetic at most.
2. Bootstrap SCC `{__init__, container, session_factory, scheduler_factory}` — **deliberate and
   documented** (container docstring; REPOMAP). Back-edges from `container.py` are deferred via
   PEP 562 `__getattr__` (lines 297/301) so the eager runtime import graph is acyclic (verified
   empirically). Correction to the raw graph: `session_factory` does **not** import
   `scheduler_factory`; the SCC is two container⇄factory pairs plus a one-way
   `scheduler_factory → session_factory` edge. `scheduler_factory.py:20`'s back-import is
   gratuitous — both names actually live in `runtime_context.py`. Cost is maintenance only:
   three parallel name lists in `container.py` (TYPE_CHECKING block, frozensets, `__all__`).

**Coupling hotspots (fan-in / fan-out):**

| Module | Fan-in | Fan-out | Note |
|---|---|---|---|
| `application.ports` | 19 | 2 | The dependency-inversion seam; highest fan-in — healthy |
| `bootstrap.container` | 3 | 20 | Composition root + compat facade; highest fan-out, sole `LLMClient` construction site (test seam) |
| `application.tool_execution` | 14 | 4 | ToolRuntime/ToolExecutionUseCase; member of the type-only cycle |
| `domain.session` | 12 | 1 | SessionState/Phase FSM — imported by every layer above; correct shape for a domain core |
| `bootstrap.session_factory` | 2 | 12 | Part of the deliberate lazily-broken bootstrap cycle |
| `adapters.tools.base` | 10 | 1 | Tool base; imported by all concrete tools + registry |
| `application.scheduler` | 3 | 10 | Mixin-composed orchestrator; widest fan-out inside application |

### 1.3 Oversized / multi-responsibility files (confirmed only)

| File | Confirmed problem | Severity |
|---|---|---|
| `swebench/run_swe_docker.py` | 1,008 lines (> the 800 cap), seven-plus concerns (docker plumbing, env adapter, prompts, runners, test-cmd heuristics, grading, batch CLI) — **and unrunnable**: lazy `importlib` loads of deleted `opencollab.core/tools/team.*` paths; `main()` hits `opencollab.core.config` unconditionally (line 834) and rewraps the failure as a misleading "install opencollab package first" error. Verified `ModuleNotFoundError` for all 9 paths in the project venv. Its single-agent runner duplicates `harness/evaluator.py`; its `ExistingContainerEnvironment` duplicates `adapters/env.py`. Grading/test-cmd logic is unique to this script (does *not* overlap harness, contrary to the raw finding). | **High** |
| `application/compaction.py` + wiring | Two parallel compaction implementations. Legacy mutating `ContextCompactionUseCase` is knowingly retired (`compaction_enabled=False` at `container.py:256`, comment "retired (constructed but never routed to)") yet stays a *required* `SessionRunUseCase` ctor arg, a whole `COMPACTING` FSM phase, the `compactor` attribute on `SessionRuntime`/`Session`, and an `__init__` export. Hazard: `compaction_enabled` *defaults to True* (`session_run.py:52`), so any construction outside the container silently re-enables the legacy generic-prompt path. Live path is `AutoCompactShaper` + `ReadTimeSummarizer` (9-section prompt). Cost is one object allocation (it reuses the resolved LLM client — no extra client), but it is duplicated abstraction + a hazardous default. | Medium |
| `bootstrap/session_factory.py` | Free `build_spawn_session` (lines 159-201) and `DefaultSessionFactory.build_spawn_session` (231-271) are near line-for-line identical and have already diverged once (`auto_save_path`, cached builder). Free function has zero production callers (tests only); both are exported. Also `snapshot_session` reaches into `session.event_bus._targets` and `session._safety_policy` (loud-failure coupling, but private-internals dependency). | Medium |
| `application/scheduler*.py` mixins | File-level (by documented design), not responsibility-level split: Lifecycle/Messaging mixins form a guarded cyclic call graph and share ~15 undeclared attributes set only in `Scheduler.__init__`; no Protocol/annotations, not unit-testable in isolation. Reentrancy is correctly lock-guarded — no correctness bug. | Medium (low end) |
| `adapters/cli/config_resolve.py` | Owns presentation (`_print_missing_key_hint` imports TUI, renders banner + rich error) in a module scoped to config resolution; plus redundant/divergent API-key env re-checks vs `bootstrap.config` (provider→env-key knowledge in three layers incl. `adapters/llm/client.py`; one edge case where a whitespace-only env key silently suppresses the missing-key error). | Medium |
| `domain/scheduler.py` | 113 lines, mostly coherent around spawn/delegation lifecycle; the one misfit is `ReviewVerdict.parse` (LLM review-output parsing consumed only by `application/self_collaboration.py`). | Low |
| `harness/evaluator.py` | Not oversized, but bypassed: all three real SWE-bench paths skip it because it is not parameterizable (fixed prompt, fixed tools, hardwired env choice) and its `DockerEnvironment` cannot run official `sweb.eval` images or attach to an existing container. The duplication has already diverged behaviorally (`git add -A && git diff --cached` vs plain `git diff` — untracked files silently dropped by the harness path). Also `EvalResult.success` means "non-empty diff + no error", reported by the CLI as "X/Y passed". | Medium |

Other confirmed medium-severity defects (not size-related):

- **`adapters/tools/spawn.py`** — defer-vs-resolve signalled by implicit return type (`-> int`
  annotation, actually `int | str`; executor dispatches on `isinstance(ref, int)`), violating the
  `base.Tool -> str` contract. Contained: name-gated by `DEFAULT_DEFERRABLE_TOOLS` and test-pinned,
  but statically unverifiable.
- **`adapters/tools/fs.py` / `apply_patch.py`** — host-side `FileLock(f"{path}.lock")` while I/O
  goes through `runtime.environment` (possibly Docker): the lock protects nothing remotely, and in
  the Docker eval path resolves against the harness host CWD where missing parent dirs make
  acquire raise `FileNotFoundError` → spurious tool errors. Also blocks the event loop
  (synchronous acquire, timeout=10). This is exactly why `swebench/gen_prediction.py` monkeypatches
  `_fs.FileLock` to a no-op (a private-name coupling that breaks silently, and which does **not**
  cover `apply_patch.py`'s identical lock).
- **`adapters/tools/fs.py`** — six `if env: ... else: open(...)` direct-IO fallbacks duplicating
  `LocalEnvironment` with silently divergent semantics (process-CWD resolution, dropped
  `errors="replace"`); production never hits them (`container.py:209` defaults to
  `LocalEnvironment()`), but contract tests pin them.
- **`adapters/tools/human.py`** — `AskUserTool` does terminal I/O directly (rich Prompt /
  raw `input()`), bypassing the TUI's suspend/resume prompt mechanism; the live render will
  clobber the prompt in exactly the interactive mode where the tool runs. Bonus defect: in
  `--yolo` mode it falsely reports "non-interactive" despite a present user.
- **`adapters/cli/main.py`** — dead `cancel_event`: created/cleared/set but never read; the
  `except KeyboardInterrupt` that sets it is itself likely unreachable under `asyncio.run`. Ctrl-C
  mid-turn tears down the whole CLI instead of interrupting the turn. Confirmed leftover from the
  removed chat mode (commit c952ee9).
- **`adapters/llm/client.py`** — streaming API (`stream`, `StreamDelta`, `stream_*`) is dead code
  across five files (~85-90 lines), zero callers/tests, and bypasses the subsystem's own
  `with_retry` policy.
- **`application/self_collaboration.py`** — drives Scheduler via `scheduler: Any` + private
  members (`_tasks`, `_emit_scheduler_event`); verified it does *not* bypass any lock-guarded wake
  path (downgraded to low), but typing is erased and the docstring mislabels privates as public.
- **`adapters/env.py` / peripheral** — docker-exec environment triplicated (`DockerEnvironment`,
  `gen_prediction.ContainerEnv`, `run_swe_docker.ExistingContainerEnvironment`) with `ExecResult`
  redeclared in all three files and observable drift (conda activation, sentinels, timeout rc).
- **`bootstrap/config.py` seams** — validated pydantic `OpenCollabConfig` erased to `model_dump()`
  dict at every seam; downstream re-defaults (`cfg.get("llm_timeout", 600.0)`, `_safe_int(...,
  200_000)`, `cfg["budget"] = max(..., 500_000)`) duplicate defaults in three places. Values stay
  consistent today; type/model is lost, not correctness.
- **Env-file key precedence** duplicated at the periphery (`scripts/check_dashscope.py:16`,
  `swebench/gen_prediction.py:250-257`); core `resolve_ordered` (since 3baa910) already fixes
  cross-name shadowing — the residual gap is same-name shadowing only, and the peripheral copy was
  added a day *after* the core fix (drift demonstrated).

### 1.4 Architectural invariants a refactor must preserve

(Each is pinned by named tests; breaking the test is the tripwire.)

1. **Inward-only imports** — no `opencollab.{core,tools,bootstrap,cli,adapters,team}` under
   `application/` or `domain/` (`test_application_boundaries.py`, `test_domain_boundaries.py`).
2. **Session phase FSM** — complete validated `PHASE_TRANSITIONS` table; terminal phases → IDLE
   only; `fail()`/`cancel()` escape from any phase; AWAITING_EVENTS resumes to PRECHECK
   (`test_session_phase_fsm.py`).
3. **Session-lifetime resource caps** — `reset_for_user_turn` preserves `step_count`/`used_tokens`;
   BUDGET_EXCEEDED and STEP_LIMIT_EXCEEDED distinct, checked before the next LLM call.
4. **Lossless transcript / read-time shaping** — tool results persisted verbatim; every shaper
   returns a new list; the model sees a bounded copy while `state.messages` stays full
   (`test_session_characterization.py:616`, `test_shaping.py`, `test_session_run_loop.py:422`).
5. **Group-aware shaping never orphans a `tool_call_id`**; degradation order clear→snip→
   autocompact→collapse with anti-thrash trigger/target gap (`test_shaping.py`).
6. **Deferred tool execution** — pending row keyed by `tool_call_id`, AWAITING_EVENTS suspend,
   exactly-once wake under per-aid lock, same-turn resume, contiguous tool-result blocks
   (`test_scheduler_awaiting.py`, `test_pending_event_table.py`, `test_deferred_spawn_integration.py`).
7. **team_status vocabulary mapping** — `done`→`idle`, `awaiting_events`→`awaiting`; the model must
   never see `done` (`test_message_tool.py:55-58`).
8. **Tool failures become tool-role messages, never exceptions** through the run loop; 3rd
   identical call skipped via `recent_call_hashes` (`test_session_characterization.py:491-583`).
9. **Tool contract** — `execute_with_runtime` only (no legacy `execute`); all path/cmd access
   through the safety policy; tool modules never import bootstrap or concrete `SandboxInterceptor`
   (`test_tool_runtime_contract.py`).
10. **Event delivery never crashes the loop**; every event carries an `aid`; EventBus swallows
    subscriber exceptions (`test_session_characterization.py:160`, `test_team_event_emission.py`).
11. **Stable re-export facades** — `opencollab.adapters.llm` (LLMClient, LLMResponse, Usage,
    StreamDelta, model_context_window), `opencollab.bootstrap.container` (build_session,
    build_session_runtime, SpawnConfig, ContextBuilder, agent_save_path, make_run_dir, … incl.
    lazy `__getattr__`), `opencollab.application.scheduler.LaunchSpec`.
12. **`container.LLMClient` monkeypatch seam** — `build_session_runtime` must keep resolving
    `LLMClient` late through the container module globals; moving construction or early-binding
    breaks the entire fake-LLM test fleet.
13. **Retired compaction stays unwired** — run loop must not route to COMPACTING in the wired
    runtime (`test_session_characterization.py:300`).
14. **Topology gates every spawn/message edge; single-flight (role,task) spawn dedup**
    (`test_topology.py`, `test_spawn_dedup.py`, `test_inter_agent_messaging.py`).
15. **Persistence format** — structured `{aid,role,model,messages}` JSON + manifest, legacy JSONL
    read-compat, collision-safe run dirs, timestamps in sidecar not messages
    (`test_session_persistence.py`).
16. **Context assembly generic by position/timing, never special-cased by layer**; child task is a
    startup USER_CONTEXT source (`test_context_source.py:77`, `test_context_builder.py`).

**Public API consumed outside the package (must keep resolving):**

- Console script `opencollab` → `adapters.cli.main:app` (used by `scripts/start_opencollab.sh`,
  `start_team_run.sh`, `run_team_batch.sh` — the live SWE-bench team path).
- `swebench/gen_prediction.py` imports: `adapters.tools.bash.BashTool`,
  `adapters.tools.fs.{FileReadTool,FileWriteTool,GrepTool}`, **module-level late-bound
  `adapters.tools.fs.FileLock`** (monkeypatched — name must stay module-level, resolved at call
  time), `adapters.trace.Tracer`, `bootstrap.config.{get_config,load_config_env}`,
  `bootstrap.container.{build_session,agent_save_path,make_run_dir}`, `domain.agent.Agent`.
- `scripts/check_dashscope.py`: `bootstrap.config.{build_config,load_config_env}`.
- STALE (need *not* be preserved): `opencollab.team.orchestrator`, `opencollab.tools.*`,
  `opencollab.core.*` — referenced only by the broken `run_swe_docker.py`.

### Appendix: investigated, refuted

- **"Two divergent token estimators cause compaction/shaping disagreement" — refuted.**
  Both heuristics exist (`estimate_messages_tokens` chars//3 vs `approx_messages_tokens` chars//4),
  but bootstrap injects the *same* adapter estimator into all reactive shapers
  (`container.py:164-174`) and the compactor is disabled, so no disagreement is possible in
  production; the application-side copy is a documented dependency-free fallback, and the
  recommended `TokenEstimatorPort` already exists (`ports.py:234`). Residual low-severity note:
  the injected estimator ignores `tool_call` arguments (undercounts tool-heavy histories), and the
  unused fallback's divergent semantics could surprise future non-injected construction.

Verified-but-reframed (not refuted, severity reduced by verification): the domain purity guard
gap is narrower than reported (the glob test in `test_application_boundaries.py` already covers
all 10 modules; only the domain→application rule is limited to 5 hardcoded files);
`self_collaboration`'s private-member use does not bypass any lock (low, not medium); the
bootstrap import cycle is documented intent with maintenance-only cost.

---

## 2. Refactor plan

Honesty first: **propose nothing for what is already sound.** The domain layer (pure, small,
boundary-tested), the application layer's import discipline, the adapters-llm structure
(facade + per-provider modules + retry), the adapters-tui structure, the bootstrap module split,
and the shaping pipeline are all in good shape. The plan below is deliberately narrow: kill dead
code, consolidate the duplicated eval/environment paths, and harden a handful of implicit
contracts. No big-bang restructuring is warranted.

### 2.1 Target directory structure (only where change is warranted)

```
BEFORE                                          AFTER
swebench/
  gen_prediction.py        (working)            gen_prediction.py   (thinner: imports attach-mode env, no FileLock patch)
  run_swe_docker.py        (1008 ln, BROKEN)    run_swe_docker/     (only if revived; else DELETED)
                                                  __init__.py, images.py, runner.py, grading.py, cli.py
opencollab/opencollab/
  adapters/env.py                               adapters/env.py     (+ DockerEnvironment.attach(container_id=...),
                                                                     exec working-dir + command-prefix hooks)
  application/compaction.py (legacy, wired)     application/compaction.py  (deleted OR kept unwired with
  application/session_run.py (compaction        session_run.py compaction arg made Optional, default OFF)
    required ctor arg, default ON)
  domain/scheduler.py (ReviewVerdict misfit)    domain/review.py    (ReviewVerdict moves; ~16 lines)
  adapters/cli/config_resolve.py (prints UI)    config_resolve.py returns data; hint rendering in main.py
```

Everything else keeps its current layout.

### 2.2 Proposed file splits (responsibility seams)

1. **`swebench/run_swe_docker.py`** — decision first: it is dead code on current main (every
   importlib target deleted) yet still advertised by `swebench/README.md`. Either **delete it and
   fix the README** (recommended: `gen_prediction.py` + `start_team_run.sh` are the live paths), or
   rewrite split per concern (image/container plumbing; runner built on `SessionFactoryPort` /
   `harness`; grading; batch CLI) reusing `adapters/env.py` — never reproducing the
   `types.MethodType` monkeypatching of deleted Team internals.
2. **`domain/scheduler.py`** — move `ReviewVerdict` to a review-owned domain module
   (`domain/review.py`); the rest of the file coheres around spawn/delegation and stays.
3. **`adapters/cli/config_resolve.py`** — extract `_print_missing_key_hint` rendering into
   `main.py` (or TUI); module returns data only. Cohesion fix inside one adapter, not a layering fix.
4. **`adapters/tools/_output.py`** — one shared head+tail `_truncate` helper for the three tool
   copies (bash/git_diff/run_tests). Note: the fourth copy in `application/scheduler.py:208` cannot
   import from adapters (boundary test); either leave it or host the helper in domain.
5. **No split** for `application/scheduler*.py` mixins: the split is documented design, reentrancy
   is correctly guarded, and a responsibility-level split would not remove the domain-inherent
   lifecycle⇄messaging cycle. Harden with declared attributes instead (see 2.3).

### 2.3 Where Protocol/ABC + DI genuinely reduce coupling (and where not)

Worth doing:

- **`CompletionResponse` Protocol in `application/ports.py`** — the LLM response contract is
  currently docstring-only `Any`; a structural Protocol is satisfiable by the existing
  `adapters/llm/types.LLMResponse` without any application→adapters import, making
  `session_run.py`'s attribute dereferences checkable. Cheap, high signal.
- **Deferred-tool contract** — replace the `int`-vs-`str` implicit return protocol
  (`spawn.py` / `tool_execution.py:215`) with an explicit `DeferredCall` marker type (or a
  `DeferrableTool` Protocol). Must update `DEFAULT_DEFERRABLE_TOOLS` dispatch and
  `test_spawn_dedup.py`'s isinstance assertions together.
- **`TracePort.flush()`** — add to the port (or drop the use-case-side flush: the concrete Tracer
  already self-flushes every `log_step`), eliminating the only off-port access through a
  port-typed reference.
- **Diff capability** — either an optional `DiffCapablePort` or surface diff extraction through
  `WorktreePool` (which already tracks its `WorktreeEnvironment`s); removes the
  `getattr(env, "get_diff", None)` duck-probe that evades the boundary tests.
- **Type the factory plumbing with existing ports** — the leaf tools already take
  `scheduler: SchedulerPort`; the `Any` originates in `SessionFactoryPort` itself
  (`ports.py:111-124`) and is mirrored by bootstrap. Fix at the port, then the factories. For
  `build_workspace_safety_policy`, the right annotation is the concrete `Environment` (the port
  has no `workspace` attr) — already used by the sibling function.
- **Ask/confirm port for `AskUserTool`** — `PermissionPort.confirm` is bool-only; add a free-text
  ask capability provided by the runtime so the tool stops doing terminal I/O and the TUI's
  suspend/resume mechanism is reused.
- **Scheduler mixin state** — annotated attribute declarations (or a small `SchedulerState`
  object) on the mixins; no behavior change, makes the ~15 shared attributes discoverable.

Explicitly **not** worth new abstraction:

- EventBus/`_emit_scheduler_event` — don't add a port; just require `EventPublisherPort`, simplify
  to `await self._event_sink.emit(event)`, wrap test sinks in `EventBus(...)` (pattern already used).
- Provider dispatch in `LLMClient` — the OpenAI-compatible catch-all is documented contract
  (DeepSeek/Ollama/vLLM); a closed enum would break it. Add a near-miss warning and centralize the
  `== "anthropic"` check (3 sites) instead.
- Domain `to_openai_schema` naming — test-pinned, anti-corrupted at the Anthropic adapter,
  functional coupling is zero. Rename only if touching all pinned sites anyway; not a priority.
- `TokenEstimatorPort` — already exists and is already injected everywhere (refuted finding).

### 2.4 Prioritized step list

Each step is small, independently committable. Risk = chance of behavior change; verification =
existing tests that prove behavior unchanged (plus targeted additions).

**Phase 0 — dead/broken code (do first, near-zero risk)**

| # | Step | Risk | Verification |
|---|---|---|---|
| 0.1 | Delete `swebench/run_swe_docker.py` + `scripts/run_swe_docker.sh`, update `swebench/README.md` to point at `gen_prediction.py` / `start_team_run.sh` (or explicitly mark it as needing the rewrite in 2.2.1) | Low (verifiably unrunnable; nothing imports it) | `git grep run_swe_docker`; full pytest run untouched |
| 0.2 | Delete dead streaming surface in `adapters/llm` (`stream`, `StreamDelta`, `stream_*`, `__init__` re-export — note invariant 11 lists `StreamDelta`; update the facade `__all__` and REPOMAP in the same commit) | Low | grep zero callers; full pytest; `test_session_characterization.py` (uses only `complete`) |
| 0.3 | Remove dead `cancel_event` plumbing in `cli/main.py` (or wire it: add `cancel_event` pass-through `Scheduler.run` → `_drive_agent` → `session.run_loop`; prefer wiring — the run-loop side is already implemented and tested) | Low (delete) / Med (wire) | `test_session_run_loop.py:207`, `test_session_characterization.py:265` already cover run_loop cancellation; add a scheduler-level cancel test if wiring |
| 0.4 | Remove dead exports: `BudgetExceededError`/`LoopDetectedError` (`application/session.py`), `LLMResponse.raw` field + two `raw=resp` kwargs, `LLMClient.max_output_tokens()`, `build_default_tools` export, unused `domain/__init__` facade (complete it or shrink to docstring), unused `adapters/tools/__init__` facade (zero consumers — safe to drop) | Low | Full pytest; grep for each symbol |

**Phase 1 — legacy compaction retirement (single biggest internal cleanup)**

| # | Step | Risk | Verification |
|---|---|---|---|
| 1.1 | Make `SessionRunUseCase.compaction` Optional and flip `compaction_enabled` default to False (kills the silent re-enable hazard) | Low | `test_session_characterization.py:300` (no routing), `test_context_compaction_use_case.py` stays green |
| 1.2 | Drop the `compactor` attribute from `SessionRuntime`/`Session` facade, the `__init__` export, and the inert `compaction_threshold` knob; update the characterization tests that pin `session.compactor` and REMAP docs | Med (test-pinned surface) | Updated `test_session_construction.py`, `test_autosave_subscriber.py`; full suite |
| 1.3 | Delete `ContextCompactionUseCase` + its inline prompt + the COMPACTING phase edge (keep the FSM table test in sync) | Med | `test_session_phase_fsm.py` (table updated), `test_session_run_loop.py`, shaping tests prove the live path |

**Phase 2 — eval-path consolidation (de-triplicate the SWE-bench machinery)**

| # | Step | Risk | Verification |
|---|---|---|---|
| 2.1 | Add attach-mode to `DockerEnvironment` (`container_id=...`), plus exec working-dir and optional command-prefix hook (covers conda activation), parametrized timeout rc | Low (additive) | New unit test with a fake `docker` shim; existing `harness` path untouched |
| 2.2 | Fix host-side FileLock: skip the host lock whenever `runtime.environment` is set (or move locking behind the env port / out-of-tree lock dir) in `fs.py` AND `apply_patch.py` | Med | `test_tool_runtime_contract.py`, `test_edit_tool.py`; update the None-env contract tests deliberately |
| 2.3 | Rewrite `swebench/gen_prediction.py` on the attach-mode env; delete its `ContainerEnv`, `ExecResult` copy, and the `_fs.FileLock` monkeypatch (enabled by 2.2) | Med (live eval path) | One-instance smoke run per the SWE-bench eval workflow; diff predictions JSONL format unchanged |
| 2.4 | Parameterize `harness/evaluator.py` (prompt, tools, env factory, max_steps) and rename/document `EvalResult.success` → `patch_produced` (CLI wording + results.jsonl key) | Low (single consumer: `cli/eval.py`) | `adapters/cli/eval.py` compile + a tiny EvalTask smoke; no core tests touch harness |
| 2.5 | Move env-file key precedence into `bootstrap/config.py` (same-name file-first option for provider keys), with tests; delete the copies in `check_dashscope.py` / `gen_prediction.py` | Med (key resolution is a known footgun) | Extend `tests/test_config.py` (pattern: `test_dashscope_file_key_beats_generic_export`); `scripts/check_dashscope.py` smoke |

**Phase 3 — contract hardening (typing, ports, small seams)**

| # | Step | Risk | Verification |
|---|---|---|---|
| 3.1 | `CompletionResponse` Protocol in `ports.py`; type `LLMPort.complete` and `PendingStep.response` | Low | Full suite (structural typing; no runtime change); boundary tests stay green |
| 3.2 | Explicit deferred-call type for spawn (replace `isinstance(ref, int)` dispatch) | Med | `test_spawn_dedup.py` (update isinstance assertions), `test_scheduler_awaiting.py`, `test_deferred_spawn_integration.py` |
| 3.3 | `TracePort.flush` (or drop the cancellation-path flush); `DiffCapablePort`/pool-surfaced diff replacing the `get_diff` getattr-probe | Low | `test_team_event_emission.py`; add one test pinning worktree-diff delivery to parents (currently untested seam) |
| 3.4 | Promote `Scheduler.wait_for(aid)` + typed event emit; retype `self_collaboration.run_spawn_with_review` against a Protocol; fix its docstring | Low | `test_team_event_emission.py` review-lifecycle tests |
| 3.5 | Type factory plumbing: fix `SessionFactoryPort` `Any`s, `scheduler: SchedulerPort` in context_builder/session_factory/tool_registry, `env: Environment` in `build_workspace_safety_policy` | Low | Full suite (annotations only) |
| 3.6 | Ask port for `AskUserTool`; route through TUI suspend/resume; fix the `--yolo` false "non-interactive" report | Med (interactive-only behavior) | `test_tool_runtime_contract.py` monkeypatch site updated; manual TUI check |
| 3.7 | Collapse the fs direct-IO fallbacks (make env required or error like bash.py), updating the contract tests that pin them | Med | `test_tool_runtime_contract.py` rewritten deliberately; `test_edit_tool.py` |
| 3.8 | Misc low items, batched: free `build_spawn_session` → delegate to method (add `auto_save_path` param) or drop from surface; `snapshot_session` public accessors (EventBus subscribers, Session safety policy); `_emit_scheduler_event` → require `EventPublisherPort`; shared `_truncate` helper; underscore-API renames in `adapters/cli`; consolidate /save into the REPL command table (needs `lead` threading); domain→application glob guard in `test_domain_boundaries.py` (then delete the redundant 5-file list); Literal-typed `_emit_scheduler_event` event types + factory builders (note declared-but-never-emitted `budget_warning`); WorktreePool/env cleanup logging with rc checks; hook action-type validation at config load; TUI `_args_preview` unification (keep the scheduler-event `task` payload shape working) + roster DTO/TypedDict (drop the phantom `completed` phase, unify with toolbar's mapping); provider near-miss warning across the 3 `== "anthropic"` sites; `replace_messages` non-mutating comprehension | Low each | Each maps to an existing pinned test file: `test_team_decomposition.py`, `test_session_construction.py`, `test_cli_toolbar.py`, `test_tui_event_rendering.py`, `test_hooks.py`, `test_team_event_emission.py`, `test_session_persistence.py:137` |

**Sequencing rationale:** Phase 0 is pure deletion of verified-dead code — do it immediately to
stop the rot from misleading readers (the broken script is still README-advertised). Phase 1
removes the one hazardous default (`compaction_enabled=True`) and the largest duplicate
abstraction. Phase 2 is where real eval-correctness risk lives (FileLock spurious failures,
untracked-file diff divergence) and unblocks deleting the FileLock monkeypatch. Phase 3 is
incremental hardening; every step is optional and individually shippable. Throughout: the
re-export facades, the `container.LLMClient` seam, and `gen_prediction.py`'s import surface
(section 1.4) are the contract — any step touching them must keep the names resolving.
