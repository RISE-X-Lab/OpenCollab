"""Public-task rules and caller-aware role deadlines for Duo."""

from __future__ import annotations

import math
import os

STRUCTURED_ROLE_TIMEOUT_SECONDS = 900


CODER_ROLE_TIMEOUT_SECONDS = 1800


def _llm_aware_role_timeout(default: float) -> float | None:
    if os.environ.get("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION") == "1":
        return None
    raw = os.environ.get("OPENCOLLAB_LLM_TIMEOUT", "600")
    try:
        llm_timeout = float(raw)
    except ValueError as exc:
        raise ValueError("OPENCOLLAB_LLM_TIMEOUT must be a positive finite number") from exc
    if not math.isfinite(llm_timeout) or llm_timeout <= 0:
        raise ValueError("OPENCOLLAB_LLM_TIMEOUT must be a positive finite number")
    try:
        provider_budget = float(os.environ.get("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET", "0"))
    except ValueError as exc:
        raise ValueError("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET must be finite and non-negative") from exc
    if not math.isfinite(provider_budget) or provider_budget < 0:
        raise ValueError("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET must be finite and non-negative")
    return max(default, llm_timeout + provider_budget + 60)


def structured_role_timeout_seconds() -> float | None:
    """Let provider-managed retries finish before the workflow ends a role."""
    return _llm_aware_role_timeout(STRUCTURED_ROLE_TIMEOUT_SECONDS)


def coder_role_timeout_seconds() -> float | None:
    """Keep the coding role alive through its model client's retry window."""
    return _llm_aware_role_timeout(CODER_ROLE_TIMEOUT_SECONDS)


SHARED_RULES = """\
Rules:
- Use public issue, repository, test, and documentation evidence only.
- Never use hidden grader data, official hidden tests, grader patches, or FAIL_TO_PASS IDs.
- Obey this role and its tools.
- Keep probes under /tmp/opencollab-validation-* and out of the patch.
- Report unavailable probes as not_run. Make the smallest source fix.
- Read-only roles do not search for write tools.
- Do not run git commit."""


def _complete_goal(goal: str) -> str:
    """Keep every public task field visible to every solver role."""
    return goal.strip()
