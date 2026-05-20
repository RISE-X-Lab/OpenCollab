from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING

from opencollab.application.event_bus import EventSink
from opencollab.application.ports import PermissionPort, SafetyPolicyPort
from opencollab.domain.agent import Agent
from opencollab.adapters.env import Environment
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.context_compactor import DEFAULT_COMPACTION_THRESHOLD
from opencollab.adapters.trace import Tracer
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.session import SessionPhase, SessionState

if TYPE_CHECKING:
    from opencollab.bootstrap.container import SessionRuntime


class BudgetExceededError(Exception):
    pass


class LoopDetectedError(Exception):
    pass


class Session:
    """Public facade for a stateful agent session.

    Construction of runtime collaborators (EventBus, SessionState,
    LLMClient, SessionStore, AutoSaveSubscriber, ToolCallProcessor,
    ContextCompactor, SessionRunner) lives in
    ``opencollab.bootstrap.container.build_session_runtime``. The facade
    either accepts a pre-built ``runtime`` or builds a default one via the
    bootstrap factory.
    """

    def __init__(
        self,
        agent: Agent,
        env: Environment | None = None,
        tracer: Tracer | None = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        repo_map: str | None = None,
        auto_save_path: str | None = None,
        event_sink: EventSink | None = None,
        permission_policy: PermissionPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
        llm=None,
        store=None,
        runtime: "SessionRuntime | None" = None,
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
        self._repo_map = repo_map

        if runtime is None:
            # Facade default-factory shim: bootstrap owns concrete construction.
            from opencollab.bootstrap.container import build_session_runtime

            runtime = build_session_runtime(
                agent=agent,
                env=env,
                tracer=tracer,
                max_budget_tokens=max_budget_tokens,
                max_steps=max_steps,
                compaction_threshold=compaction_threshold,
                repo_map=repo_map,
                auto_save_path=auto_save_path,
                event_sink=event_sink,
                permission_policy=permission_policy,
                safety_policy=safety_policy,
                llm=llm,
                store=store,
                auto_save_callback=self._auto_save,
            )

        # Adopt the runtime's collaborators as Session attributes so the
        # public surface stays exactly what it used to be.
        self.state = runtime.state
        self.event_bus = runtime.event_bus
        self._llm = runtime.llm
        self.store = runtime.store
        self.tool_processor = runtime.tool_processor
        self.compactor = runtime.compactor
        self.runner = runtime.runner
        # The env attribute mirrors the runtime's tool_processor env so
        # downstream readers (snapshot, characterization tests) still see
        # the same Environment instance.
        if env is None:
            self.env = self.tool_processor.env

    @property
    def permission_policy(self) -> PermissionPort | None:
        return self._permission_policy

    @permission_policy.setter
    def permission_policy(self, value: PermissionPort | None) -> None:
        self._permission_policy = value
        if hasattr(self, "tool_processor"):
            self.tool_processor.permission_policy = value

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

    def snapshot(self) -> Session:
        # Snapshots intentionally drop the internal AutoSaveSubscriber but
        # keep every external subscriber that was attached to the bus.
        external_sink: EventSink | None = None
        for target in self.event_bus._targets:
            if not isinstance(target, AutoSaveSubscriber):
                external_sink = target  # type: ignore[assignment]
                break
        new = Session(
            agent=self.agent,
            env=self.env,
            tracer=self.tracer,
            max_budget_tokens=self.max_budget_tokens,
            max_steps=self.max_steps,
            compaction_threshold=self.compaction_threshold,
            event_sink=external_sink,
            permission_policy=self.permission_policy,
            safety_policy=self._safety_policy,
        )
        new.messages = copy.deepcopy(self.messages)
        new.used_tokens = self.used_tokens
        new.step_count = self.step_count
        return new

    def save(self, path: str) -> None:
        self.store.save(path, self.messages)

    def _auto_save(self) -> None:
        if self._auto_save_path:
            self.save(self._auto_save_path)

    @classmethod
    def load(cls, path: str, agent: Agent, **kwargs) -> Session:
        session = cls(agent=agent, **kwargs)
        session.messages = session.store.load_messages(path, agent.system_prompt)
        return session
