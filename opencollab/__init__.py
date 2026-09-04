"""OpenCollab's compact, lazily loaded public Python API."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencollab.sdk import OpenCollab, RunError, RunResult, workflow

__version__ = "0.5.1"

__all__ = ["OpenCollab", "RunError", "RunResult", "workflow"]
_PUBLIC_MODULES = {
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
