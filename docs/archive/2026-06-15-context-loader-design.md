---
title: ContextLoaderPort & deferred-context wiring — design (report only)
date: 2026-06-15
status: design / not yet implemented
context: follows "make context layers load-bearing" (commit d079864). This is the
  loader half — turning deferred PROJECT/MEMORY sources into real tagged content so
  the priority/pin/shed machinery has something to act on.
---

# Design: `ContextLoaderPort` and Deferred-Context Wiring for OpenCollab

## 0. Grounding — what's already true in the code

| Concern | Location | State today |
|---|---|---|
| Layer/timing/position enums, `ContextSource`, `ContextPlan`, stamping | `domain/context.py` | Pure domain. `startup_user_messages()` stamps `_ctx={layer,priority}`. `deferred_sources()` returns non-STARTUP sources but loads nothing. |
| Source registration | `bootstrap/context_builder.py:84-148` | PROJECT/MEMORY/TOOL_META are `LoadTiming.ON_DEMAND` with `loader_key` + empty `content`. |
| Plan build + seed | `session_factory.py:239,261` (spawn), `:283` (lead — builds agent **without** a plan, seeds nothing) | Seeds `plan.startup_user_messages()`. |
| Seed → messages | `container.py:88-96` (`_build_initial_state`) | system message + `seed_user_messages` → `SessionState.messages`. |
| Shaper pipeline | `container.py` (`_build_default_shaper`) | `LowPriorityContextShedShaper` runs 2nd, before tool/turn recency layers. |
| Shed logic / pin floor | `shaping/reactive.py`, `shaping/pipeline.py` (`PIN_FLOOR=70`) | Sheds messages whose `_ctx.priority < PIN_FLOOR`; dormant until PROJECT/MEMORY carry content. |
| Where shapers run | `session_run.py` (`call_llm`) | Shapes a copy; `state.messages` stays full. |
| User turn entry | `session.py:158-161` (`add_user_message`) | The PER_TURN hook point. |
| Persistence | `storage.py`, `session.py:141-151` (`enriched_messages`) | Whole message dict (incl. `_ctx`) persisted; replayed verbatim on resume. |
| Provider consumption | `openai_provider.py` passes `_ctx` straight to SDK (ignored); `anthropic_provider.py` rebuilds key-by-key (drops `_ctx`). | `_ctx` already reaches the OpenAI SDK today as an ignored extra key; loaders multiply this. |

The machinery is fully wired *except the producer*: no code turns a deferred source into content. That is the gap `ContextLoaderPort` fills.

## 1. Port interface (`application/ports.py`, Protocol, sync)

```python
@dataclass(frozen=True)
class ContextRequest:
    loader_key: str
    role: str
    timing: LoadTiming
    task: str | None = None
    trigger: str | None = None    # latest user message (PER_TURN)
    workspace: str | None = None  # repo root for file-backed loaders

class ContextLoaderPort(Protocol):
    loader_key: str
    def load(self, request: ContextRequest) -> ContextSource | None: ...
```

Returns a **fully-formed `ContextSource`** (so the loader owns `priority`/provenance and we reuse `effective_priority` + domain stamping), or `None` to contribute nothing. **Sync** to match `ShaperPort`; I/O-bound loaders do blocking reads off the hot path. Open decision: add a parallel `AsyncContextLoaderPort` later only if a loader truly needs async.

## 2. Resolution timing

- **ON_DEMAND (project/memory/tool_meta today): resolve eagerly at session build** — there is no "reference" mechanism to trigger true laziness, conventions are wanted from turn 1, and build-time content persists/resumes + is governed by pin/shed immediately. A `ContextResolver` walks `plan.deferred_sources()`, calls the loader, and rebuilds a **new immutable plan** with the resolved source flipped to `STARTUP` (so `_startup()` assembles it). Honors the frozen-dataclass / immutability rule.
- **PER_TURN: re-resolve each user turn** at `add_user_message`. Avoid growth with **replace-last-turn** (tag `_ctx.turn_scoped`, drop the prior turn's block before injecting) + shed as backstop. Insert the recall block immediately before the new user message. Injected via an optional runner callback so default = no-op.
- **DURING_EXECUTION: deferred** — no consumer; document the hook slot (`session_run.py` after tool results) and leave inert.

## 3. How loaded content enters the live window

Factor the existing stamping comprehension into a shared domain helper `stamp(source) -> {role,content,_ctx{layer,priority}}`; STARTUP and loaded paths share it. Pin/shed shapers then govern loaded content for free. USER_CONTEXT order stays **project → task → memory**. Optional hardening: strip `_ctx` before the OpenAI request (`{k:v for k,v in m.items() if k!="_ctx"}`) to be provider-agnostic.

## 4. First adapter — `ProjectConventionsLoader` (`adapters/context/`)

`loader_key="project"`; reads `<workspace>/CLAUDE.md` (real file at repo root) then a project-local override; size-capped; returns a PROJECT `ContextSource`, or `None` when absent / on I/O error (never raises into build). Open decision: env-routed (`EnvironmentPort.read_file`, correct for worktree/container roots) vs direct read.

## 5. `MemoryRecallLoader` — stub-friendly sketch

`MemoryStorePort.recall(query, k) -> list[MemoryHit]` (text+score). Loader queries on `trigger` (PER_TURN) or `task`, gates by `min_score`, returns a MEMORY source (priority 20, sheddable) or `None`. First store impl = no-op/stub so it ships dark. The MEMORY layer is the *agent's* cross-session memory — distinct from any host memory.

## 6. Bootstrap wiring

A registry keyed by `loader_key` (`bootstrap/context_loaders.py`), injected into the `ContextResolver`, wired into `DefaultSessionFactory` for **both** spawn and lead paths (closing the lead-has-no-plan gap at `session_factory.py:283`, which is what finally gives the lead project conventions). Loaders are adapters; resolver-as-orchestration is bootstrap; the port keeps application clean (verified against `test_application_boundaries.py`).

## 7. Resume / persistence

Loaded content persists with `_ctx`. **ON_DEMAND project: replay persisted, do not re-load** (resume's message-replace wins — verify spawn vs lead paths don't double up). **PER_TURN memory: don't trust persisted blocks as live** — replace-last-turn overwrites next turn anyway; stale block is sheddable meanwhile. Add a resume test asserting no duplicate project block.

## 8. Shed-vs-compact ordering (open, data-driven)

Keep shed **before** the recency layers but make it a **two-phase** shed: a conservative first bite (memory + truly-stale only, small headroom), recency layers next (tool-clear, snip), an aggressive second shed (project too) last — rather than reordering wholesale. Moving shed entirely after recency would let stale memory crowd out reconstructible-but-bulky tool output, contradicting the loss model. Ship single-phase first; settle the two-phase reorder with a SWE-bench A/B vs the 61.7% baseline.

## 9. Test plan

Domain: `stamp()` regression-lock; resolver rebuilds an immutable new plan. Loaders: project present/absent/oversized/error; memory empty-query/below-score/k. Resolver/registry: unknown key no-crash; ON_DEMAND content populated + timing→STARTUP; PER_TURN replace leaves exactly one block. Boundary: new ports keep application stdlib/domain-only. **Headline behavior test:** real loaded project block (priority 30) is shed under pressure while a pinned source survives, and `state.messages` still holds both (read-time projection). Resume: exactly one project block.

## 10. Phased plan

- **Phase 0** — `refactor:` extract the `stamp()` helper (no behavior change).
- **Phase 1** — `feat:` port + registry + `ProjectConventionsLoader` resolved at build, wired for spawn **and** lead. PROJECT stops being empty; shed shaper goes live. (The ROI slice.)
- **Phase 2** — `feat:` `MemoryStorePort` + stub + `MemoryRecallLoader` + PER_TURN seam (ships dark by default).
- **Phase 3** — `feat:` two-phase shed, gated on a SWE-bench A/B.
- **Deferred** — DURING_EXECUTION (document the slot, leave inert).

## Open decisions for the maintainer

1. Port sync vs async — recommend sync now.
2. Strip `_ctx` before OpenAI — recommend a one-line defensive strip.
3. ProjectConventionsLoader env-routed vs direct read — env-routed is more correct for worktrees.
4. Shed ordering — ship single-phase, settle two-phase with an A/B.
5. Where `ContextRequest` lives — domain (pure) vs application.
