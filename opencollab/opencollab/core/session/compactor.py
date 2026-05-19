from __future__ import annotations

from typing import Any

from opencollab.application.compaction import (
    COMPACTION_KEEP_RECENT,
    DEFAULT_COMPACTION_THRESHOLD,
    CompactionEventFactory,
    ContextCompactionUseCase,
)
from opencollab.application.event_bus import EventBus
from opencollab.adapters.llm import estimate_messages_tokens
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.compaction import CompactResult
from opencollab.domain.session import SessionState


class ContextCompactor:
    def __init__(
        self,
        *,
        state: SessionState,
        llm: Any,
        event_bus: EventBus,
        tracer: Any = None,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    ):
        self.state = state
        self.llm = llm
        self.event_bus = event_bus
        self.tracer = tracer
        self.compaction_threshold = compaction_threshold
        self._use_case = ContextCompactionUseCase(
            state=self.state,
            llm=self.llm,
            event_publisher=self.event_bus,
            event_factory=self._event_factory(),
            estimate_tokens=estimate_messages_tokens,
            tracer=self.tracer,
            compaction_threshold=self.compaction_threshold,
        )

    def should_compact(self) -> bool:
        return self._use_case.should_compact()

    async def compact(self, apply: bool = True) -> CompactResult:
        return await self._use_case.compact(apply=apply)

    def _split_messages_for_compaction(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        return self._use_case.split_messages_for_compaction()

    def _build_compaction_prompt(self, older: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
        return self._use_case.build_compaction_prompt(older)

    async def _call_compaction_llm(
        self,
        summary_request: list[dict[str, str]],
        older_text: list[str],
    ) -> tuple[str, int]:
        return await self._use_case.call_compaction_llm(summary_request, older_text)

    def _build_compacted_messages(
        self,
        system_msg: dict[str, Any],
        older: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        summary_text: str,
    ) -> list[dict[str, Any]]:
        return self._use_case.build_compacted_messages(system_msg, older, recent, summary_text)

    def _event_factory(self) -> CompactionEventFactory:
        return CompactionEventFactory(
            compaction=lambda: SessionEvent(type="compaction", data={"reason": "context_overflow"}),
            compaction_applied=lambda tokens_after: SessionEvent(
                type="compaction_applied",
                data={"tokens_after": tokens_after},
            ),
        )
