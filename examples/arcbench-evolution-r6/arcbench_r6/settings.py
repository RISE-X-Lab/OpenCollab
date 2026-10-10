"""Original competition limits, resolved separately for each invocation."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _number(values, name, default):
    try:
        value = float(values.get(name, default))
    except (ValueError, TypeError):
        value = default
    return value if math.isfinite(value) and value > 0 else default


@dataclass(frozen=True)
class Settings:
    budget: int = 16_000_000
    main_budget: int = 12_000_000
    main_hard_budget: int = 14_000_000
    repair_reserve: int = 2_000_000
    max_steps: int = 200
    wall_seconds: float = 6_000
    agent_seconds: float = 4_800
    max_output_tokens: int = 32_768
    context_window: int = 1_000_000
    history_trigger_tokens: int | None = None
    cleanup_seconds: float = 30
    tool_cleanup_seconds: float = 10

    def __post_init__(self):
        for name in (
            "budget",
            "main_budget",
            "main_hard_budget",
            "repair_reserve",
            "max_steps",
            "max_output_tokens",
            "context_window",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("wall_seconds", "agent_seconds", "cleanup_seconds", "tool_cleanup_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.history_trigger_tokens is not None and (
            isinstance(self.history_trigger_tokens, bool)
            or not isinstance(self.history_trigger_tokens, int)
            or self.history_trigger_tokens < 2
        ):
            raise ValueError("history_trigger_tokens must be at least 2")

    @classmethod
    def from_env(cls, values=None):
        values = os.environ if values is None else values
        if values.get("ARC_GROUPED_MAIN", "1") == "0":
            raise ValueError("The native r6 workflow uses grouped execution; set ARC_GROUPED_MAIN=1")
        cap = values.get("ARC_HISTORY_TRIGGER_TOKENS")
        if cap is not None and cap.strip():
            cap = int(cap)
            if cap < 2:
                raise ValueError("ARC_HISTORY_TRIGGER_TOKENS must be at least 2")
        else:
            cap = None
        return cls(
            budget=int(_number(values, "OPENCOLLAB_BUDGET", 16_000_000)),
            main_budget=int(_number(values, "ARC_MAIN_BUDGET", 12_000_000)),
            main_hard_budget=int(_number(values, "ARC_MAIN_HARD_BUDGET", 14_000_000)),
            repair_reserve=int(_number(values, "ARC_REPAIR_RESERVE", 2_000_000)),
            max_steps=int(_number(values, "OPENCOLLAB_MAX_STEPS", 200)),
            wall_seconds=_number(values, "ARC_WALL_LIMIT_MIN", 100) * 60,
            agent_seconds=_number(values, "ARC_AGENT_LIMIT_MIN", 80) * 60,
            context_window=int(_number(values, "OPENCOLLAB_CONTEXT_WINDOW", 1_000_000)),
            history_trigger_tokens=cap,
        )
