from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, runtime_checkable

from opencollab.domain.hooks import HookOutcome
from opencollab.domain.skill import SkillManifest

if TYPE_CHECKING:
    from opencollab.application.scheduler_types import LaunchSpec
    from opencollab.application.tool_execution import DeferredCall, ToolRuntime


class EnvironmentPort(Protocol):
    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> Any:
        ...

    async def read_file(self, path: str) -> str:
        ...

    async def write_file(self, path: str, content: str) -> None:
        ...


@runtime_checkable
class DiffCapablePort(Protocol):
    """An environment that can report its accumulated changes as a diff.

    Satisfied by worktree-style environments; the scheduler appends the diff
    to a finished child's result before delivering it to the parent.
    """

    async def get_diff(self) -> str:
        ...


@runtime_checkable
class WorkingTreeProbe(Protocol):
    """Read-only probe answering "has the working tree changed?".

    Lets an application-layer workflow verify that an agent actually edited the
    tree before it declares a phase/run successful — without importing an
    Environment. The concrete impl (env-backed ``git status --porcelain``) is
    wired in ``adapters``/``bootstrap``/the harness where the env exists; when no
    probe is wired the workflow treats the answer as "unknown" and must not
    hard-block on it.
    """

    async def changed(self) -> bool:
        """True when the working tree has uncommitted changes."""
        ...

    async def changed_excluding(self, paths: Sequence[str]) -> bool:
        """True when the tree has changes OUTSIDE ``paths`` (e.g. harness-injected
        test files). Empty ``paths`` is equivalent to :meth:`changed`."""
        ...

    async def diff(self) -> str:
        """The current working-tree diff (best-effort, may be empty)."""
        ...


class SafetyPolicyPort(Protocol):
    def check_path(self, target_path: str) -> str:
        ...

    def check_cmd(self, cmd: str) -> None:
        ...

    def is_risky(self, cmd: str) -> bool:
        ...

    def check_cmd_interactive(
        self,
        cmd: str,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> Awaitable[None]:
        ...


SafetyPolicyFactory = Callable[[Any], SafetyPolicyPort | None]


class PermissionPort(Protocol):
    async def confirm(self, prompt: str) -> bool:
        ...


class AskUserPort(Protocol):
    """Free-text human input for the ``ask_user`` tool.

    Distinct from ``PermissionPort`` (a yes/no confirm gate): this returns the
    user's answer to an open question. A TUI implementation pauses its live
    render around the prompt; ``None`` is never returned — its presence is the
    signal that a human is reachable, so a wired ask port keeps ``ask_user``
    interactive even in auto-approve (yolo) mode.
    """

    async def ask(self, question: str) -> str:
        ...


class EventPublisherPort(Protocol):
    async def emit(self, event: Any) -> None:
        ...


class ShaperPort(Protocol):
    """Reshapes the message list just before a model call.

    A shaper is a pure transform: given the current messages it returns a new
    list (never mutating the input), applied in ``SessionRunUseCase.call_llm``
    before ``LLMPort.complete``. It bounds what the model *sees* without
    touching the persisted history, so the transcript stays a complete audit
    record. Shapers compose in order via ``ShaperPipeline``.
    """

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ...


class HookPort(Protocol):
    """Runs the hooks bound to a lifecycle event.

    ``payload`` carries ``hook_event_name`` plus the originating event's data
    (``aid`` and, for tool-scoped events, ``tool``/``args``). Phase-1
    implementations observe only and return an allowing ``HookOutcome``; the
    return type is fixed now so phase-2 blocking (PreToolUse deny) adds no
    signature churn.
    """

    async def fire(self, event_name: str, payload: dict[str, Any]) -> HookOutcome:
        ...


class ToolPort(Protocol):
    """A callable tool: JSON Schema input, string result. A deferrable tool
    (e.g. ``spawn_agent``) returns a ``DeferredCall`` instead when it hands
    work off whose result arrives later.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        ...

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: "ToolRuntime",
    ) -> "str | DeferredCall":
        ...


class SkillStorePort(Protocol):
    """Discovery + retrieval of skill packages. The reserved plug-point for skills.

    ``list_manifests()`` is the catalog the model reads (name + description per
    skill); ``get_body()`` is the on-invocation retrieval of the full instruction
    body. This is the entire inward-facing contract for skills; concrete stores
    (file-backed, null) live in ``adapters.skills``.
    """

    def list_manifests(self) -> tuple[SkillManifest, ...]:
        """Catalog metadata (name + description) for every available skill."""
        ...

    def get_body(self, name: str) -> str | None:
        """Full instruction body for ``name``, or ``None`` if unknown."""
        ...


class SessionFactoryPort(Protocol):
    """Factory the scheduler uses to build sessions.

    Bootstrap binds this to the concrete builders so the scheduler layer does
    not import ``opencollab.application.session.Session`` or know how a session
    is wired (env, tools, prompt, store). The returned session is driven through
    ``add_user_message`` / ``run_loop`` / ``apply_launch`` and read via
    ``used_tokens``.
    """

    def build_spawn_session(
        self,
        *,
        role: str,
        env: EnvironmentPort,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
        scheduler: SchedulerPort | None = None,
        task: str | None = None,
        context: str = "",
    ) -> Any:
        ...

    def create_lead_session(
        self,
        *,
        scheduler: SchedulerPort,
        launch: LaunchSpec,
        budget: int,
        aid: int = 0,
    ) -> Any:
        """Build agent 0 (the lead / init process).

        The factory owns all construction detail — local environment, tool
        bundle (including the spawn tools bound to ``scheduler``), prompt, and
        store. ``launch`` carries persistence locations; the factory uses only
        ``launch.auto_save_path`` (subscriber wiring) and leaves resume/seed to
        the scheduler via ``Session.apply_launch``.
        """
        ...


class WorkflowSessionFactoryPort(Protocol):
    """Factory the workflow engine uses to build one-shot agent sessions.

    Bootstrap binds this to the concrete ``build_session`` builder so the
    ``WorkflowContext`` engine never imports ``Session`` or knows how a session
    is wired. The returned session is duck-typed and driven through
    ``add_user_message`` / ``run_loop`` and read via ``used_tokens`` and
    ``state.messages`` — no inheritance contract beyond those names.
    """

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> Any:
        ...    # ``thinking`` None -> factory default; False -> force reasoning off.


class SchedulerPort(Protocol):
    """Port for the scheduler — called by tools to spawn agents."""

    async def spawn(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str = "",
        tool_call_id: str | None = None,
    ) -> int:
        """Non-blocking spawn. Returns aid immediately.

        ``tool_call_id`` ties the child to the parent's pending row so its
        completion re-activates the suspended parent; ``None`` is fire-and-forget.
        """
        ...

    def inflight_spawn(self, role: str, task: str) -> int | None:
        """The aid already handling this (role, task) if a spawn is in flight,
        else ``None``. Lets the spawn tool refuse a duplicate single-flight.
        """
        ...

    @property
    def budget_exhausted(self) -> bool:
        """True once the team's *aggregate* spend has reached the global cap.

        Lets a session's precheck enforce the global ceiling as defense-in-depth
        without importing the concrete scheduler.
        """
        ...

    async def spawn_with_review(
        self,
        parent_aid: int,
        task: str,
        context: str = "",
        max_iterations: int = 3,
    ) -> str:
        """Blocking review loop. Returns final result."""
        ...

    async def send_message(self, from_aid: int, to_aid: int, summary: str, content: str) -> str:
        """Queue a teammate message for async delivery and return an acknowledgement."""
        ...

    def team_snapshot(self) -> list[dict[str, Any]]:
        """Read-only roster: one dict per agent (aid, role, parent_aid, phase, busy)."""
        ...


class CompletionUsage(Protocol):
    """Token accounting attached to a completion."""

    @property
    def input_tokens(self) -> int:
        ...

    @property
    def total_tokens(self) -> int:
        ...


class CompletionResponse(Protocol):
    """Structural contract for one LLM completion result.

    Exactly the attributes the run loop dereferences — adapter response types
    (e.g. ``adapters.llm.LLMResponse``) satisfy it without this layer importing
    them. ``tool_calls`` holds OpenAI-shaped dicts (``id`` + ``function``).
    """

    @property
    def content(self) -> str | None:
        ...

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        ...

    @property
    def usage(self) -> CompletionUsage:
        ...

    @property
    def finish_reason(self) -> str | None:
        ...

    @property
    def reasoning(self) -> str | None:
        """Provider chain-of-thought, if any (recorded to the trajectory).

        Optional: implementations may omit it (the run loop reads it
        defensively via ``getattr``).
        """
        ...


class LLMPort(Protocol):
    """LLM client surface used by the session run loop and compaction."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        thinking: bool = False,
        thinking_params: dict[str, Any] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResponse:
        ...


class SessionStorePort(Protocol):
    """Message persistence surface (structured JSON per agent)."""

    def save(
        self,
        path: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        ...

    def load_messages(
        self,
        path: str,
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        ...

    def save_manifest(self, path: str, manifest: dict[str, Any]) -> None:
        ...


class TracePort(Protocol):
    """Trajectory recorder surface."""

    def log_step(
        self,
        *,
        step_type: str,
        payload: dict[str, Any],
        tokens: int = 0,
        latency: float = 0.0,
    ) -> None:
        ...

    def flush(self) -> None:
        ...


# Callable that estimates a token cost for a messages list.
TokenEstimatorPort = Callable[[list[dict[str, Any]]], int]


class WorktreePoolPort(Protocol):
    """Per-spawn worktree environment lifecycle."""

    async def acquire(self, role: str) -> Any:
        ...

    async def release(self) -> None:
        ...
