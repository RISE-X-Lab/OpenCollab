"""Terminal UI adapter: renders runtime/team events, prompts for permission."""

from opencollab.adapters.tui.renderer import TUI
from opencollab.adapters.tui.session_adapter import (
    SuspendableRender,
    TuiEventSink,
    TuiPermissionPolicy,
)

__all__ = ["TUI", "TuiEventSink", "TuiPermissionPolicy", "SuspendableRender"]
