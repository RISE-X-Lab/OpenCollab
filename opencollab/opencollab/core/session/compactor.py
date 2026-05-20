"""Compatibility re-export for legacy core.session.compactor imports."""

from opencollab.application.context_compactor import (
    COMPACTION_KEEP_RECENT,
    DEFAULT_COMPACTION_THRESHOLD,
    CompactResult,
    ContextCompactor,
)

__all__ = [
    "COMPACTION_KEEP_RECENT",
    "DEFAULT_COMPACTION_THRESHOLD",
    "CompactResult",
    "ContextCompactor",
]
