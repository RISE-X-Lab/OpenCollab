from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opencollab.domain.session import SessionState


@dataclass
class CompactResult:
    messages: list[dict[str, Any]] | None = None
    used_tokens_delta: int = 0
    did_compact: bool = False
    compacted_count: int = 0
    summary_len: int = 0

    def apply_to(self, state: SessionState) -> None:
        if self.messages is not None:
            state.replace_messages(self.messages)
        if self.used_tokens_delta:
            state.add_used_tokens(self.used_tokens_delta)
