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

    def evolution_config(self):
        from opencollab.builtin_workflows.evolution import EvolutionConfig

        from .prompts import SYSTEM_PROMPT

        return EvolutionConfig(
            budget=self.budget,
            main_budget=self.main_budget,
            main_hard_budget=self.main_hard_budget,
            repair_reserve=self.repair_reserve,
            max_steps=self.max_steps,
            wall_seconds=self.wall_seconds,
            agent_seconds=self.agent_seconds,
            minimum_group_tokens=200_000,
            minimum_group_steps=3,
            minimum_group_seconds=150,
            main_agent_time_reserve=600,
            main_wall_time_reserve=1500,
            group_check_fraction=0.25,
            group_check_min_seconds=60,
            group_check_max_seconds=240,
            group_check_grace_seconds=90,
            group_check_timeout=450,
            minimum_session_seconds=60,
            cleanup_seconds=self.cleanup_seconds,
            tool_cleanup_seconds=self.tool_cleanup_seconds,
            max_output_tokens=self.max_output_tokens,
            history_trigger_tokens=self.history_trigger_tokens,
            extension_tokens=1_000_000,
            extension_margin_tokens=200_000,
            extension_margin_fraction=0.1,
            budget_final_prompt=(
                "Budget is near its current limit. Finish the current operation and focused verification; "
                "record checkpoint states and next actions now. Do not start another audit or redesign. "
                "Only recent source changes or new check evidence can unlock available contingency budget."
            ),
            max_repair_rounds=3,
            stagnant_round_limit=2,
            repair_budget=2_000_000,
            repair_budget_fraction=0.65,
            repair_time_reserve=300,
            minimum_repair_seconds=120,
            repair_agent_time_reserve=300,
            repair_timeout=600,
            repair_steps=60,
            final_check_timeout=600,
            final_check_time_reserve=60,
            system_prompt=SYSTEM_PROMPT,
        )

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
