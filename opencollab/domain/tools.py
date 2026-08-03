from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from opencollab.domain.session import SessionState

MAX_CALL_HASH_WINDOW = 200


class ToolSpec(Protocol):
    """The schema surface every tool exposes to an agent.

    Domain-side Protocol describing only what an Agent needs at configuration
    time: name + description + parameters + OpenAI schema rendering. Tool
    execution lives in application.ports.ToolPort.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LoopDetection:
    tool: str
    count: int


@dataclass
class ToolProcessingResult:
    messages_to_append: list[dict[str, Any]] = field(default_factory=list)
    recent_hash_updates: list[str] = field(default_factory=list)
    loop_detections: list[LoopDetection] = field(default_factory=list)
    # Closed-loop steering counters for this batch: how many read-only tool calls
    # executed, and whether a write landed. A successful write RESETS
    # ``reads_since_last_edit``; otherwise the reads accumulate onto it.
    reads_executed: int = 0
    write_succeeded: bool = False
    # STEP 1 information-gain sensor: one ``(content_hash, call_hash,
    # intrinsic_low_yield)`` tuple per EXECUTED tool result, in call order. Folded
    # into SessionState's novelty counters by ``apply_evidence_counter_to``.
    # Short-circuited calls (bad args / unknown tool / loop block) produce no real
    # tool output and contribute no signal.
    evidence_signals: list[tuple[str, str, bool]] = field(default_factory=list)
    # STEP 2 evidence ledger: one ``{tool, target, snippet}`` raw card per EXECUTED
    # tool result, index-aligned with ``evidence_signals`` (same producer loop). The
    # ``outcome`` is decided at fold time (it needs SessionState's novelty memory),
    # so the card here carries only the envelope facts. Additive to STEP 1 — a
    # caller that never populates it folds counters exactly as before.
    evidence_cards: list[dict[str, Any]] = field(default_factory=list)

    def apply_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
        for message in self.messages_to_append:
            state.append_message(message)
        self.apply_hashes_to(state, max_window=max_window)
        self.apply_read_write_counter_to(state)
        self.apply_evidence_counter_to(state)

    def apply_read_write_counter_to(self, state: SessionState) -> None:
        """Fold this batch's read/write activity into ``reads_since_last_edit``.

        A landed edit zeroes the counter (the model committed); a batch with no
        edit adds its read calls so the steering layer can escalate when the
        model keeps reading without writing.
        """
        if self.write_succeeded:
            state.turn.reads_since_last_edit = 0
        else:
            state.turn.reads_since_last_edit += self.reads_executed

    def apply_evidence_counter_to(self, state: SessionState) -> None:
        """Fold this batch's information-gain signals into the novelty counters
        (STEP 1) and the evidence ledger (STEP 2).

        Replays each executed result in call order through
        ``state.record_evidence_signal`` so the within-batch ordering of
        informative vs low-yield results is preserved (a duplicate that follows
        its own first occurrence in the same batch scores low-yield). The
        index-aligned ``evidence_cards`` ride along so the ledger's outcome label
        is decided from the SAME novelty decision. Like
        ``apply_read_write_counter_to`` it must be applied even on the deferred
        path, where the result MESSAGES are buffered into the pending table.
        Observational only — no control flow depends on the counters/ledger.

        STEP 3 watchdog: also folds this batch's PER-STEP progress signal into
        ``steps_since_progress``. A step makes progress when it lands a write OR a
        novel informative ("hit") result (``distinct_evidence_count`` rose). A real
        tool-executing batch with neither increments the counter by 1; a progress
        batch resets it to 0. Counted per STEP (batch), not per result, so a single
        turn that fired several low-yield reads still costs one watchdog step. Like
        the counters above this is maintained ALWAYS — only the precheck brake that
        reads it is gated, so the off path is unchanged.
        """
        distinct_before = state.turn.distinct_evidence_count
        made_progress = self.write_succeeded
        for i, (content_hash, call_hash, intrinsic_low_yield) in enumerate(self.evidence_signals):
            card = self.evidence_cards[i] if i < len(self.evidence_cards) else None
            state.record_evidence_signal(
                content_hash, call_hash, intrinsic_low_yield, card=card
            )
        if self.evidence_signals:
            made_progress = (
                self.write_succeeded or state.turn.distinct_evidence_count > distinct_before
            )
            if made_progress:
                state.turn.steps_since_progress = 0
            else:
                state.turn.steps_since_progress += 1
        if made_progress:
            state.turn.loop_blocked_since_progress = 0
        elif self.loop_detections:
            state.turn.loop_blocked_since_progress += 1

    def apply_hashes_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
        """Apply only the loop-detection hashes, not the result messages.

        Used when a batch contains a deferred tool: immediate results are
        buffered into the pending table (to keep the whole batch's tool-result
        block contiguous on resume) while their call hashes still record now.
        """
        if self.write_succeeded:
            state.turn.recent_call_hashes.clear()
        for call_hash in self.recent_hash_updates:
            state.remember_tool_call_hash(call_hash, max_window=max_window)


__all__ = [
    "LoopDetection",
    "MAX_CALL_HASH_WINDOW",
    "ToolProcessingResult",
    "ToolSpec",
]
