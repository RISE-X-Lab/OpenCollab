from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from opencollab.application.ports import (
    EventPublisherPort,
    LLMPort,
    TokenEstimatorPort,
    TracePort,
)
from opencollab.domain.compaction import CompactResult
from opencollab.domain.session import SessionState

# Compaction thresholds (ref: opencode PRUNE_MINIMUM / PRUNE_PROTECT)
DEFAULT_COMPACTION_THRESHOLD = 64_000  # tokens - trigger compaction
COMPACTION_KEEP_RECENT = 8  # keep last N messages un-summarized


@dataclass(frozen=True)
class CompactionEventFactory:
    compaction: Callable[[], Any]
    compaction_applied: Callable[[int], Any]


class ContextCompactionUseCase:
    def __init__(
        self,
        *,
        state: SessionState,
        llm: LLMPort,
        event_publisher: EventPublisherPort,
        event_factory: CompactionEventFactory,
        estimate_tokens: TokenEstimatorPort,
        tracer: TracePort | None = None,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
    ):
        self.state = state
        self.llm = llm
        self.event_publisher = event_publisher
        self.event_factory = event_factory
        self.estimate_tokens = estimate_tokens
        self.tracer = tracer
        self.compaction_threshold = compaction_threshold

    def should_compact(self) -> bool:
        # Auto-compact if context is too large (ref: opencode isOverflow)
        estimated = self.estimate_tokens(self.state.messages)
        return estimated > self.compaction_threshold

    async def compact(self, apply: bool = True) -> CompactResult:
        """Summarize older messages to reduce context size."""
        await self.event_publisher.emit(self.event_factory.compaction())

        if len(self.state.messages) <= COMPACTION_KEEP_RECENT + 2:
            return CompactResult()  # Not enough messages to compact

        system_msg, older, recent = self.split_messages_for_compaction()
        summary_request, older_text = self.build_compaction_prompt(older)
        summary_text, used_tokens_delta = await self.call_compaction_llm(summary_request, older_text)
        result = CompactResult(
            messages=self.build_compacted_messages(system_msg, older, recent, summary_text),
            used_tokens_delta=used_tokens_delta,
            did_compact=True,
            compacted_count=len(older),
            summary_len=len(summary_text),
        )

        if self.tracer:
            self.tracer.log_step(
                step_type="compaction",
                payload={"messages_compacted": result.compacted_count, "summary_len": result.summary_len},
                tokens=0,
                latency=0,
            )
        if apply:
            result.apply_to(self.state)
            if result.did_compact:
                await self.event_publisher.emit(self.event_factory.compaction_applied(self.state.used_tokens))
        return result

    def split_messages_for_compaction(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        # Split: system prompt | older messages | recent messages
        system_msg = self.state.messages[0]
        older = self.state.messages[1:-COMPACTION_KEEP_RECENT]
        recent = self.state.messages[-COMPACTION_KEEP_RECENT:]
        return system_msg, older, recent

    def build_compaction_prompt(self, older: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
        older_text = []
        for m in older:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str) and content:
                older_text.append(f"[{role}]: {content[:2000]}")
            elif m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    older_text.append(f"[tool_call]: {tc['function']['name']}(...)")

        summary_request = [
            {
                "role": "system",
                "content": (
                    "You are a context compaction assistant. Summarize the following conversation history into a "
                    "concise but complete summary. Preserve: all file paths mentioned, key decisions made, current "
                    "task status, and any errors encountered. Be factual and brief."
                ),
            },
            {"role": "user", "content": "\n".join(older_text)},
        ]
        return summary_request, older_text

    async def call_compaction_llm(
        self,
        summary_request: list[dict[str, str]],
        older_text: list[str],
    ) -> tuple[str, int]:
        try:
            summary_resp = await self.llm.complete(summary_request, temperature=0.0)
            summary_text = summary_resp.content or "[compaction failed]"
            return summary_text, summary_resp.usage.total_tokens
        except Exception:
            return "\n".join(older_text[:5000]), 0  # Fallback: keep raw truncated

    def build_compacted_messages(
        self,
        system_msg: dict[str, Any],
        older: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        summary_text: str,
    ) -> list[dict[str, Any]]:
        # Rebuild messages: system + compaction summary + recent
        return [
            system_msg,
            {
                "role": "system",
                "content": f"[Context compacted — summary of {len(older)} earlier messages]:\n{summary_text}",
            },
            *recent,
        ]
