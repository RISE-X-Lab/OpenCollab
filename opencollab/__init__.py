"""OpenCollab's compact, lazily loaded public Python API."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencollab.sdk import (
        BudgetDecision,
        BudgetSnapshot,
        OpenCollab,
        RunControl,
        RunError,
        RunEvent,
        RunResult,
        workflow,
    )

__version__ = "0.9.3"

__all__ = [
    "BudgetDecision", "BudgetSnapshot", "OpenCollab", "RunControl", "RunError", "RunEvent", "RunResult", "workflow",
]
_PUBLIC_MODULES = {
    **{name: "opencollab.application.run_control"
       for name in ("BudgetDecision", "BudgetSnapshot", "RunControl", "RunEvent")},
    "OpenCollab": "opencollab.sdk.client",
    "RunError": "opencollab.sdk.result",
    "RunResult": "opencollab.sdk.result",
    "workflow": "opencollab.workflows",
}


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_PUBLIC_MODULES[name]), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
