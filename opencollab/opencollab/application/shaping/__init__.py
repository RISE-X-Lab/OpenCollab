"""Context shapers — deterministic message reshaping before each model call.

A shaper is the *certain* counterpart to prompt guidance: prompts steer the
model probabilistically ("read files in narrow ranges"), while a shaper
guarantees an outcome regardless of whether the model complied. They run in
``SessionRunUseCase.call_llm`` against a copy of the history, so the persisted
transcript keeps the full, auditable content while the model sees a bounded
view.

The pipeline is an ordered, **lazy-degradation** chain (Liu et al. 2026,
§3.1/4.3): cheaper, lower-loss layers run before costlier ones, and each layer
only acts under the pressure it is responsible for. Two orthogonal pressures:

* **Per-message explosion** — ``PerToolResultBudgetShaper`` (``tool_budget``)
  caps *any one* tool result. It is unconditional: every oversize result is
  bounded each call.
* **History accumulation** — after dozens of turns the *total* view approaches
  the context limit even when no single message is oversize. The **reactive**
  layers (``reactive``) no-op until estimated context crosses a trigger, then
  degrade progressively —

    A0. ``ToolOutputClearShaper`` — lowest-loss. Clears the *content* of old
        compactable tool results in place, keeping the call/answer skeleton.
    A.  ``OldHistorySnipShaper``  — cheapest deletion. Drops whole old,
        low-reference tool-exchange turns (no model call).
    B.  ``AutoCompactShaper``     — last resort. Summarizes the remaining old
        span into one *visible* marker via an injected summarizer (model call;
        default-off switch).
    C.  ``ContextCollapseShaper`` — reserved read-time-projection slot; identity
        this period (insertion point only).

Every history layer is a read-time projection over a *copy*: ``state.messages``
and the persisted transcript always keep the full original history, so a resume
rebuilds losslessly from the transcript. Layers run oldest/cheapest-first; each
re-estimates the (already-reshaped) input it receives, so once a cheaper layer
brings the view under the trigger the costlier ones see no pressure and no-op.

Package layout: ``pipeline`` (chain + trigger math + span helpers),
``tool_budget`` (per-result cap), ``reactive`` (the history layers).
"""

from opencollab.application.shaping.pipeline import (
    DEFAULT_COMPACT_BUFFER_TOKENS,
    DEFAULT_HISTORY_KEEP_RECENT_GROUPS,
    DEFAULT_HISTORY_TARGET_TOKENS,
    DEFAULT_HISTORY_TRIGGER_TOKENS,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    HISTORY_TARGET_RATIO,
    ShaperPipeline,
    approx_messages_tokens,
    history_trigger_target,
)
from opencollab.application.shaping.reactive import (
    COMPACTED_MARKER_PREFIX,
    DEFAULT_CLEARED_TOOL_CONTENT,
    DEFAULT_COMPACTABLE_TOOLS,
    DEFAULT_TOOL_CLEAR_KEEP_RECENT,
    AutoCompactShaper,
    ContextCollapseShaper,
    OldHistorySnipShaper,
    SummarizerPort,
    ToolOutputClearShaper,
)
from opencollab.application.shaping.tool_budget import (
    DEFAULT_TOOL_RESULT_BUDGET,
    PerToolResultBudgetShaper,
)

__all__ = [
    "DEFAULT_TOOL_RESULT_BUDGET",
    "DEFAULT_HISTORY_TRIGGER_TOKENS",
    "DEFAULT_HISTORY_TARGET_TOKENS",
    "DEFAULT_HISTORY_KEEP_RECENT_GROUPS",
    "DEFAULT_CLEARED_TOOL_CONTENT",
    "DEFAULT_TOOL_CLEAR_KEEP_RECENT",
    "DEFAULT_COMPACTABLE_TOOLS",
    "DEFAULT_OUTPUT_RESERVE_TOKENS",
    "DEFAULT_COMPACT_BUFFER_TOKENS",
    "HISTORY_TARGET_RATIO",
    "COMPACTED_MARKER_PREFIX",
    "SummarizerPort",
    "approx_messages_tokens",
    "history_trigger_target",
    "ShaperPipeline",
    "PerToolResultBudgetShaper",
    "ToolOutputClearShaper",
    "OldHistorySnipShaper",
    "AutoCompactShaper",
    "ContextCollapseShaper",
]
