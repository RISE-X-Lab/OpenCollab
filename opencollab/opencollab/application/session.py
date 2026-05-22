from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opencollab.application.compaction import DEFAULT_COMPACTION_THRESHOLD, ContextCompactionUseCase
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import (
    EnvironmentPort,
    LLMPort,
    PermissionPort,
    SafetyPolicyPort,
    SessionStorePort,
    TracePort,
)
from opencollab.application.session_run import SessionRunUseCase
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.session import SessionPhase, SessionState

if TYPE_CHECKING:
    from opencollab.application.scheduler import LaunchSpec


class BudgetExceededError(Exception):
    pass


class LoopDetectedError(Exception):
    pass


@dataclass
class SessionRuntime:
    """Pre-built collaborators a ``Session`` facade keeps as attributes.

    Not frozen: ``Session`` reassigns a few attributes during lifecycle
    operations (e.g. setting permission policy propagates through the
    tool processor); keeping this mutable keeps the facade simple.
    """

    state: SessionState
    event_bus: EventBus
    llm: LLMPort
    store: SessionStorePort
    tool_execution: ToolExecutionUseCase
    compactor: ContextCompactionUseCase
    runner: SessionRunUseCase
    auto_save_path: str | None


class Session:
    """Public facade for a stateful agent session.

    This application-layer facade owns session lifecycle state access but not
    concrete collaborator construction. Callers must pass a pre-built
    ``SessionRuntime`` from the composition root.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        runtime: SessionRuntime,
        env: EnvironmentPort | None = None,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        auto_save_path: str | None = None,
        permission_policy: PermissionPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
    ):
        self.agent = agent
        self.env = env
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self.compaction_threshold = compaction_threshold
        self._permission_policy = permission_policy
        self._safety_policy = safety_policy
        self._auto_save_path = auto_save_path
        self._launch_applied = False

        # Adopt the runtime's collaborators as Session attributes so the
        # public surface stays exactly what it used to be.
        self.state = runtime.state
        self.event_bus = runtime.event_bus
        self._llm = runtime.llm
        self.store = runtime.store
        self.tool_execution = runtime.tool_execution
        self.compactor = runtime.compactor
        self.runner = runtime.runner
        # The env attribute mirrors the runtime's tool_execution env so
        # downstream readers (snapshot, characterization tests) still see
        # the same Environment instance.
        if env is None:
            self.env = self.tool_execution.environment

    @property
    def permission_policy(self) -> PermissionPort | None:
        return self._permission_policy

    @permission_policy.setter
    def permission_policy(self, value: PermissionPort | None) -> None:
        self._permission_policy = value
        if hasattr(self, "tool_execution"):
            self.tool_execution.permission_policy = value

    @property
    def auto_save_path(self) -> str | None:
        return self._auto_save_path

    @property
    def messages(self) -> list[dict]:
        return self.state.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.state.replace_messages(value)

    @property
    def used_tokens(self) -> int:
        return self.state.used_tokens

    @used_tokens.setter
    def used_tokens(self, value: int) -> None:
        self.state.set_used_tokens(value)

    @property
    def step_count(self) -> int:
        return self.state.step_count

    @step_count.setter
    def step_count(self, value: int) -> None:
        self.state.set_step_count(value)

    @property
    def is_done(self) -> bool:
        return self.state.is_done

    @is_done.setter
    def is_done(self, value: bool) -> None:
        self.state.mark_done(value)

    @property
    def _recent_call_hashes(self) -> list[str]:
        return self.state.recent_call_hashes

    @_recent_call_hashes.setter
    def _recent_call_hashes(self, value: list[str]) -> None:
        self.state.replace_recent_tool_hashes(value)

    @property
    def phase(self) -> SessionPhase:
        return self.state.phase

    @phase.setter
    def phase(self, value: SessionPhase) -> None:
        self.state.set_phase(value)

    async def run_loop(self, cancel_event: asyncio.Event | None = None) -> str:
        return await self.runner.run_loop(cancel_event)

    async def add_user_message(self, content: str) -> None:
        self.state.append_message({"role": "user", "content": content})
        self.state.reset_for_user_turn()
        await self.event_bus.emit(SessionEvent(type="user_message_appended"))

    def apply_launch(self, launch: "LaunchSpec") -> None:
        """Apply launch-time persistence as a one-shot lifecycle step.

        Resumes from ``launch.session_file`` if it exists, otherwise seeds the
        auto-save file. Idempotent: a second call is a no-op, so live message
        state is never clobbered by a re-resume and the seed file is not
        re-truncated. The ongoing per-event ``AutoSaveSubscriber`` (wired at
        construction) is a separate concern and unaffected.
        """
        if self._launch_applied:
            return
        self._launch_applied = True
        if launch.session_file and os.path.exists(launch.session_file):
            self.messages = self.store.load_messages(launch.session_file, self.agent.system_prompt)
        elif launch.auto_save_path:
            self.save(launch.auto_save_path)

    def save(self, path: str) -> None:
        self.store.save(path, self.messages)

    def _auto_save(self) -> None:
        if self._auto_save_path:
            self.save(self._auto_save_path)


__all__ = [
    "BudgetExceededError",
    "LoopDetectedError",
    "Session",
    "SessionRuntime",
]
