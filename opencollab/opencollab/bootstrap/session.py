from __future__ import annotations

import copy

from opencollab.adapters.env import Environment
from opencollab.adapters.trace import Tracer
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.context_compactor import DEFAULT_COMPACTION_THRESHOLD
from opencollab.application.event_bus import EventSink
from opencollab.application.ports import LLMPort, PermissionPort, SafetyPolicyPort, SessionStorePort
from opencollab.application.session import (
    BudgetExceededError,
    LoopDetectedError,
    Session as _AppSession,
    SessionRuntime,
)
from opencollab.bootstrap.container import build_session_runtime
from opencollab.domain.agent import Agent


class Session(_AppSession):
    """Bootstrap-owned self-wiring Session convenience facade."""

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
        llm: LLMPort | None = None,
        store: SessionStorePort | None = None,
        runtime: SessionRuntime | None = None,
    ):
        if runtime is None:
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
        super().__init__(
            agent=agent,
            runtime=runtime,
            env=env,
            tracer=tracer,
            max_budget_tokens=max_budget_tokens,
            max_steps=max_steps,
            compaction_threshold=compaction_threshold,
            auto_save_path=auto_save_path,
            permission_policy=permission_policy,
            safety_policy=safety_policy,
        )
        self._repo_map = repo_map

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

    @classmethod
    def load(cls, path: str, agent: Agent, **kwargs) -> Session:
        session = cls(agent=agent, **kwargs)
        session.messages = session.store.load_messages(path, agent.system_prompt)
        return session


__all__ = ["BudgetExceededError", "LoopDetectedError", "Session"]
