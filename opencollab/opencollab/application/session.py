from __future__ import annotations

from dataclasses import dataclass

from opencollab.application.context_compactor import ContextCompactor
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import LLMPort, SessionStorePort
from opencollab.application.session_runner import SessionRunner
from opencollab.application.tool_processor import ToolCallProcessor
from opencollab.domain.session import SessionState


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
    tool_processor: ToolCallProcessor
    compactor: ContextCompactor
    runner: SessionRunner
    auto_save_path: str | None


__all__ = ["SessionRuntime"]
