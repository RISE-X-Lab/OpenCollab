---
title: Skill interface — design + P0/P1 plan (report only)
date: 2026-06-18
status: historical proposal / implemented
context: a "skill" is a packaged unit of INSTRUCTIONS (name + description + body),
  loaded into an agent's context when relevant — NOT a tool/function. Trigger model
  chosen = model-invoked (like Claude Code's Skill tool): the model reads a catalog
  and calls a thin `use_skill(name)` dispatcher. Delivery = tool-result (option A),
  chosen over pinned-context-injection (option B) because B mutates the cached prefix
  mid-stream and breaks prompt caching. This route rides existing infra only — no
  ContextLoader / resolver / relevance-ranker / DURING_EXECUTION seam required.
---

> **Historical proposal.** The repository now contains a working skill system.
> This document records the earlier design process and is not the current API
> or implementation reference.

# Design: Skill Interface for OpenCollab

## 0. Grounding — what's already true in the code

| Concern | Location | State today |
|---|---|---|
| Tool base class | `adapters/tools/base.py` | `name` / `description` / `parameters` (JSON Schema) + `async execute_with_runtime(params, runtime) -> str \| DeferredCall`; `to_openai_schema()`. Domain mirror Protocol in `domain/tools.py`. |
| Tool registry | `bootstrap/tool_registry.py:27-42` | Two factory maps: `STATELESS_TOOL_FACTORIES` (zero-arg) and `SCHEDULER_TOOL_FACTORIES` (one-arg, takes `SchedulerPort`). |
| Tool resolution | `bootstrap/tool_registry.py:56-84` | `build_tools_for_role(names, *, scheduler=...)` maps names→instances; unknown name → `ValueError` listing known tools. |
| Tool runtime | `application/tool_execution.py:38-51` | `ToolRuntime` carries generic execution capabilities (environment, safety/permission/ask policies, aid, tool_call_id). Construction-time deps (e.g. scheduler) are injected via the factory, NOT the runtime. |
| Context layers | `domain/context.py:27-35` (`ContextLayer`), `:59-66` (`LAYER_PRIORITY`) | Six layers (IDENTITY/TEAM/PROJECT/MEMORY/TASK/TOOL_META). Assembly is **generic over layer** — `test_assembly_is_generic_over_position_not_layer` proves a new layer assembles with zero core changes. |
| Context source / plan | `domain/context.py:69-94` (`ContextSource`), `:97-150` (`ContextPlan`) | `ContextSource(name, layer, timing, position, content, loader_key, visible, priority)` + `effective_priority`. SYSTEM/STARTUP sources fold into `system_prompt()`. |
| Plan build | `bootstrap/context_builder.py:70-148` (`build_plan`), `:150-173` (`build_agent`) | Emits ordered `ContextSource` tuple per role; SYSTEM sources fold into `Agent.system_prompt`. |
| Seed → messages | `bootstrap/container.py:92-100` (`_build_initial_state`) | system message + `seed_user_messages` → `SessionState.messages`. |
| Shed shaper | `application/shaping/reactive.py` (`LowPriorityContextShedShaper`), `shaping/pipeline.py` (`PIN_FLOOR=70`) | Sheds only messages tagged `_ctx.priority < 70`. **Untagged messages (tool work, turns) are never shed.** |

At proposal time, nothing knew the word "skill". The tool/port machinery was the substrate; the implemented design added a producer (`SkillStorePort` + one dispatcher tool + a static catalog source) without touching the deferred-loader work.

## 1. Why model-invoked + tool-result (the two decisions)

- **Model-invoked, not auto-injected by relevance.** The model reads a catalog of `name: description` lines and decides. The model *is* the relevance ranker, so we need **no** ContextLoader relevance machinery (scoring / `min_score` / vector search). This decouples skills almost entirely from the `2026-06-15-context-loader-design.md` work.
- **Delivery = tool result (A), not pinned context-source (B).** B injects a high-priority SKILL source mid-execution; high-priority content lives near the front, so the injection **mutates the cached prefix** → prompt cache invalidates for the rest of the conversation (every later turn re-pays full input). A appends the body at the tail (natural tool-result position) → prefix intact → cache hits continue. Secondary: the shed shaper already ignores untagged tool results (`reactive.py`), so A's body needs no explicit pin — the "B protects from shedding" argument is moot. A's only residual exposure is long-horizon auto-compaction of an *old* body, acceptable since the skill is usually done by then.

## 2. The skill = instructions, the tool = a switch

One generic `use_skill(name)` dispatcher serves **all** skills — N skills add **zero** tools. The skill package (`SKILL.md`) stays an independent file artifact; its content stays pure instructions. The tool only fetches a body by name and returns it. So "skill is not a tool / does not depend on the tool registry" still holds — the registry merely hosts the trigger.

## 3. Domain — pure value object (`domain/skill.py`, new)

```python
@dataclass(frozen=True)
class SkillManifest:
    """A skill's catalog metadata — what the model sees to decide whether to invoke."""
    name: str          # unique invocation key, e.g. "debug-flaky-tests"
    description: str    # one-liner shown in the catalog
```

Body is **not** in the manifest: the catalog only needs name+description; the body is fetched on invocation. Add `ContextLayer.SKILL` to `domain/context.py:35` and an entry to `LAYER_PRIORITY` (`:59`). stdlib only.

## 4. Application — the reserved plug-point (`application/ports.py`)

```python
from opencollab.domain.skill import SkillManifest

class SkillStorePort(Protocol):
    """Discovery + retrieval of skill packages. The reserved plug-point for skills."""
    def list_manifests(self) -> tuple[SkillManifest, ...]:
        """Catalog metadata (name + description) for every available skill."""
        ...
    def get_body(self, name: str) -> str | None:
        """Full instruction body for `name`, or None if unknown."""
        ...
```

`list_manifests()` = catalog; `get_body()` = invocation. This is the entire inward-facing contract. application→domain import is allowed.

## 5. Adapters

### 5.1 `UseSkillTool` (`adapters/tools/use_skill.py`, new)

Construction-time store injection, mirroring the `SCHEDULER_TOOL_FACTORIES` pattern (a global, stateless catalog is a construction dep, not a runtime capability — keeps `ToolRuntime` lean).

```python
class UseSkillTool(Tool):
    name = "use_skill"
    description = ("Load the full instructions for a named skill into your context. "
                  "Call this when a skill from the catalog matches the task at hand.")
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string",
                                          "description": "Exact skill name from the catalog."}},
                  "required": ["name"]}

    def __init__(self, store: SkillStorePort) -> None:
        self._store = store

    async def execute_with_runtime(self, params: dict, runtime: ToolRuntime) -> str:
        name = params.get("name", "")
        body = self._store.get_body(name)
        if body is None:
            available = ", ".join(m.name for m in self._store.list_manifests())
            return f"Unknown skill '{name}'. Available: {available}"
        return body  # A: body IS the tool result — tail-appended, cache-friendly
```

### 5.2 `FileSkillStore` (`adapters/skills/file_skill_store.py`, new)

Scans `skills/<name>/SKILL.md`; parses frontmatter (`name`, `description`); body = text after frontmatter. Format aligns with Claude Code's `SKILL.md`, so a skill is a self-contained, droppable file package.

```python
class FileSkillStore:                       # implements SkillStorePort
    def __init__(self, root: Path) -> None: ...      # scan + parse once
    def list_manifests(self) -> tuple[SkillManifest, ...]: ...
    def get_body(self, name: str) -> str | None: ...
```

A `NullSkillStore` (empty lists / `None`) is the default so the system is identical to today when no skills directory exists.

## 6. Bootstrap — register + wire

### 6.1 Registry (`bootstrap/tool_registry.py`)

```python
SKILL_TOOL_FACTORIES: dict[str, Callable[[SkillStorePort], Tool]] = {
    "use_skill": UseSkillTool,
}

def build_tools_for_role(names, *, scheduler=None, skill_store=None) -> list[Tool]:
    # resolve: stateless → scheduler (needs scheduler) → skill (needs skill_store);
    # a name whose dependency wasn't provided → ValueError (existing fail-fast style)
```

### 6.2 Catalog injection (`bootstrap/context_builder.py:70` `build_plan`)

Inject **only when the role's tools include `use_skill`** (no invoke permission → no catalog):

```python
if skill_store is not None and "use_skill" in role.tools:
    catalog = _render_skill_catalog(skill_store.list_manifests())  # "- name: description" lines
    if catalog:
        sources.append(ContextSource(
            name="skills", layer=ContextLayer.SKILL,
            timing=LoadTiming.STARTUP, position=ContextPosition.SYSTEM,  # folds into system prompt
            content=catalog))
```

SYSTEM position folds the catalog into `Agent.system_prompt` (`build_agent` `:150`) → part of the stable cached prefix, and not in the sheddable user-message set.

### 6.3 Composition root (`bootstrap/container.py`)

Construct `FileSkillStore(skills_root)` (else `NullSkillStore`); thread into the context builder and `build_tools_for_role`. Only layer that knows the concrete type.

### 6.4 Declaration

A role opts in via one entry in `team.yaml`: `tools: [..., use_skill]`. Single switch; no per-skill declaration.

## 7. Boundary check

`FileSkillStore` / `UseSkillTool` (adapters) → `SkillStorePort` (application) → `SkillManifest` / `ContextSource` (domain). All inward; the layer contract in `.importlinter` stays green.

## 8. Test plan

- Domain: `SkillManifest` frozen; `ContextLayer.SKILL` priority present.
- `FileSkillStore`: frontmatter parse; `list_manifests`; `get_body` hit / miss (`None`); absent dir; oversized/garbage `SKILL.md` skipped not raised.
- `UseSkillTool`: known name returns body; unknown name returns available list (never raises).
- `build_tools_for_role`: wires `use_skill` with store; `ValueError` when store missing.
- `context_builder`: catalog source emitted iff `use_skill` in role tools; SYSTEM position; content lists each skill.
- Headline: a role with `use_skill` + a populated store sees the catalog in its system prompt; calling `use_skill("x")` returns x's body; cache prefix (system prompt) is unchanged by the call.

## 9. Phased plan

- **P0 — `feat:` foundations, ships dark (no observable behavior change).** `domain/skill.py` (`SkillManifest`); `ContextLayer.SKILL` + `LAYER_PRIORITY` entry; `SkillStorePort` in `application/ports.py`; `FileSkillStore` + `NullSkillStore` in `adapters/skills/`; full unit tests for store + domain. Nothing wired into bootstrap → no role can invoke, no catalog appears, suite identical. (Scaffold slice.)
- **P1 — `feat:` the ROI slice, end-to-end.** `UseSkillTool` + `SKILL_TOOL_FACTORIES` + `build_tools_for_role` extension; catalog injection in `build_plan`; `FileSkillStore` wired in `container.py` (default `NullSkillStore`). A role with `use_skill` in `team.yaml` now gets the catalog and can invoke. Add the headline behavior test + boundary tests.
- **Deferred (P2+, not in scope here).** Global skills layer (`~/.opencollab/skills`) + project override (mirrors CLAUDE.md layering); body/catalog size caps unified with the tool-guardrails char caps; tracing of `use_skill` invocations; (only if ever needed) the option-B pinned-injection path behind the DURING_EXECUTION seam.

## Open decisions for the maintainer

1. **`ContextLayer.SKILL` priority value** — catalog is SYSTEM-positioned so shed never touches it; value is semantic only. Recommend a high value (~85) to mark "infrastructure-level catalog".
2. **Skill location** — repo `skills/` only first; defer global + project-override layering to P2. Recommend repo-only for P1.
3. **Size caps** — apply the existing tool-guardrails char caps to both catalog and body. Recommend yes, for consistency.
4. **Catalog position** — SYSTEM (cache-stable, recommended) vs USER_CONTEXT.
5. **`get_body` returning raw vs capped** — recommend the store caps and the tool trusts the store (single cap site).
