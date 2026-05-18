from __future__ import annotations

import asyncio
import copy

from opencollab.application.ports import SafetyPolicyPort
from opencollab.core.agent import Agent
from opencollab.core.env import Environment, LocalEnvironment
from opencollab.core.llm import LLMClient
from opencollab.core.session.autosave import AutoSaveSubscriber
from opencollab.core.session.compactor import DEFAULT_COMPACTION_THRESHOLD, ContextCompactor
from opencollab.core.session.events import EventBus, EventSink, SessionEvent
from opencollab.core.session.runner import SessionRunner
from opencollab.core.session.state import SessionPhase, SessionState
from opencollab.core.session.storage import SessionStore
from opencollab.core.session.tools import PermissionPolicy, ToolCallProcessor
from opencollab.core.tracer import Tracer


class BudgetExceededError(Exception):
    pass


class LoopDetectedError(Exception):
    pass


class Session:
    """Public facade for a stateful agent session."""

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
        permission_policy: PermissionPolicy | None = None,
        safety_policy: SafetyPolicyPort | None = None,
        llm=None,
        store=None,
    ):
        self.agent = agent
        self.env = env or LocalEnvironment()
        self.tracer = tracer
        self.max_budget_tokens = max_budget_tokens
        self.max_steps = max_steps
        self.compaction_threshold = compaction_threshold
        self.event_bus = EventBus(event_sink)
        self._permission_policy = permission_policy
        self._safety_policy = safety_policy
        self._auto_save_path = auto_save_path
        self.store = store if store is not None else SessionStore()
        if auto_save_path:
            self.event_bus.subscribe(AutoSaveSubscriber(self._auto_save))

        system_content = agent.system_prompt
        if repo_map:
            system_content += f"\n\nProject Structure:\n{repo_map}"
        self.state = SessionState(messages=[{"role": "system", "content": system_content}])

        if llm is not None:
            self._llm = llm
        else:
            self._llm = LLMClient(
                model=agent.model,
                api_key=agent.api_key,
                base_url=agent.base_url,
                provider=agent.provider,
            )
        self._build_runtime()

    def _build_runtime(self) -> None:
        self.tool_processor = ToolCallProcessor(
            agent=self.agent,
            env=self.env,
            state=self.state,
            event_bus=self.event_bus,
            tracer=self.tracer,
            permission_policy=self.permission_policy,
            safety_policy=self._safety_policy,
        )
        self.compactor = ContextCompactor(
            state=self.state,
            llm=self._llm,
            event_bus=self.event_bus,
            tracer=self.tracer,
            compaction_threshold=self.compaction_threshold,
        )
        self.runner = SessionRunner(
            agent=self.agent,
            state=self.state,
            llm=self._llm,
            event_bus=self.event_bus,
            tool_processor=self.tool_processor,
            compactor=self.compactor,
            tracer=self.tracer,
            max_budget_tokens=self.max_budget_tokens,
            max_steps=self.max_steps,
        )

    @property
    def permission_policy(self) -> PermissionPolicy | None:
        return self._permission_policy

    @permission_policy.setter
    def permission_policy(self, value: PermissionPolicy | None) -> None:
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
        # Reach into _targets to skip the internal AutoSaveSubscriber — the
        # snapshot intentionally does not inherit auto-save (no auto_save_path
        # is passed below, so no new subscriber is created either).
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
