"""Pure domain value objects."""

from opencollab.domain.compaction import CompactResult
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.tools import MAX_CALL_HASH_WINDOW, ToolProcessingResult

__all__ = [
    "CompactResult",
    "MAX_CALL_HASH_WINDOW",
    "SessionPhase",
    "SessionState",
    "ToolProcessingResult",
]
