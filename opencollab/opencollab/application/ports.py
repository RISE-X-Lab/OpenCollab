from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from opencollab.domain.hooks import HookOutcome

if TYPE_CHECKING:
    from opencollab.application.tool_execution import ToolRuntime


class EnvironmentPort(Protocol):
    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> Any:
        ...

    async def read_file(self, path: str) -> str:
        ...

    async def write_file(self, path: str, content: str) -> None:
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
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        ...

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: "ToolRuntime",
    ) -> str:
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
        env: Any,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
        scheduler: Any = None,
        task: str | None = None,
        context: str = "",
    ) -> Any:
        ...

    def create_lead_session(
        self,
        *,
        scheduler: Any,
        launch: Any,
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


class LLMPort(Protocol):
    """LLM client surface used by the session run loop and compaction."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> Any:
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


# Callable that estimates a token cost for a messages list.
TokenEstimatorPort = Callable[[list[dict[str, Any]]], int]


class WorktreePoolPort(Protocol):
    """Per-spawn worktree environment lifecycle."""

    async def acquire(self, role: str) -> Any:
        ...

    async def release(self) -> None:
        ...
