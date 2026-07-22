"""Public structural tool contract.

Pass custom implementations directly to ``OpenCollab.agent``; built-in tools
are selected by the ``"coding"`` and ``"read"`` preset names.
"""

from opencollab.application.ports import ToolPort as Tool

__all__ = ["Tool"]
