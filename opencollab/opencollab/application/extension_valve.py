"""Single-justified-extension valve (STEP 4b).

The enforcement wind-down (STEP 0/3) force-commits a scout at ~80% of its cap.
The red team flagged that a *uniform* forced commit can prematurely cut a
genuinely-deep dimension: a scout one read away from confirming a real hypothesis
is chopped exactly like a scout spinning on dead greps. This valve grants EXACTLY
ONE bounded extension at a wind-down trip IF — and only if — the model emits a
concrete, falsifiable, NOVEL reason ("reading parser.py:88 to confirm the
off-by-one hypothesis"). A vacuous / absent / duplicate reason is denied and the
scout goes straight to the forced submit. Hard cap = 1 extension per scout.

Mechanism (cleanest fit for this codebase, which already injects schema-capture
tools into scout sessions): a ``request_extension`` structured tool offered
ALONGSIDE ``submit_findings`` on the FIRST wind-down trip. The model either
commits (``submit_findings``) or justifies (``request_extension``). The tool only
*records* the requested reason — the harness (session_run precheck) judges it with
``judge_extension_reason`` and grants/denies, so the decision lives in the harness
and the model cannot self-grant.

Pure application layer: stdlib + application imports only (no adapters import),
mirroring ``submit_findings``.
"""

from __future__ import annotations

import re
from typing import Any

from opencollab.application.tool_execution import ToolRuntime

REQUEST_EXTENSION_TOOL_NAME = "request_extension"

REQUEST_EXTENSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["reason"],
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "A concrete, falsifiable reason to read exactly ONE more thing "
                "before committing. State WHAT you will read (a file:line or symbol) "
                "and WHICH hypothesis it will confirm or refute, e.g. 'reading "
                "parser.py:88 to confirm the off-by-one hypothesis'. Vacuous, "
                "generic, or repeated reasons are denied and you must submit_findings "
                "immediately."
            ),
        }
    },
}


class RequestExtensionTool:
    """Records a scout's request for ONE more exploration turn (STEP 4b).

    Stateful like ``SubmitFindingsTool``: ``requested`` holds the last requested
    payload (``None`` until one is recorded). The tool deliberately does NOT judge
    the reason — it only captures it; ``session_run`` precheck reads ``requested``
    and calls ``judge_extension_reason`` so the grant/deny decision stays in the
    harness. One instance per scout session, so there is no cross-call leakage.
    """

    name = REQUEST_EXTENSION_TOOL_NAME
    default_timeout: float | None = None
    disable_outer_timeout = False
    description = (
        "Request ONE more exploration turn before committing your findings. Only "
        "use this when you have a SPECIFIC, falsifiable reason — name the exact "
        "file:line or symbol you will read and the hypothesis it confirms or "
        "refutes. You get AT MOST one extension; a vacuous, generic, or repeated "
        "reason is denied and you must call submit_findings immediately."
    )

    def __init__(self) -> None:
        self.parameters = REQUEST_EXTENSION_SCHEMA
        self.requested: dict[str, Any] | None = None

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": REQUEST_EXTENSION_SCHEMA,
            },
        }

    async def execute_with_runtime(self, params: dict[str, Any], runtime: ToolRuntime) -> str:
        reason = str((params or {}).get("reason") or "").strip()
        self.requested = {"reason": reason}
        return (
            "Extension request received. The harness will decide whether to grant ONE more "
            "exploration turn; if your reason is not concrete and novel you must call "
            "submit_findings now."
        )


# Minimum words a reason must carry to clear the vacuousness floor. A one- or
# two-word "more" / "keep looking" is rejected outright.
_MIN_REASON_WORDS = 5

# Falsifiability signals: a genuine reason names a hypothesis it will confirm or
# refute, not merely "explore more". Matched as substrings of the normalized
# (lowercased, whitespace-collapsed) reason.
_FALSIFIABILITY_KEYWORDS = (
    "confirm",
    "verify",
    "refute",
    "disprove",
    "rule out",
    "rule-out",
    "falsify",
    "validate",
    "hypothesis",
    "whether",
    "because",
)

# A concrete anchor: a file (``name.ext``), a qualified symbol (``a::b`` / ``a.b``),
# a snake_case identifier, or a ``:line`` reference. Presence of any one means the
# reason points at a specific thing to read, not a vague "look around".
_CONCRETE_ANCHOR_RE = re.compile(
    r"[\w/]+\.[A-Za-z]\w*"  # path/file.ext
    r"|\b\w+::\w+"  # qualified::symbol
    r"|\b[A-Za-z]\w*\.[A-Za-z]\w*"  # module.symbol
    r"|\b\w+_\w+\b"  # snake_case identifier
    r"|:\d+\b"  # :line
)


def _normalize_reason(reason: str) -> str:
    return " ".join((reason or "").lower().split())


def judge_extension_reason(
    reason: str | None, prior_reasons: list[str] | None
) -> tuple[bool, str]:
    """Decide whether a requested extension reason earns the single bounded grant.

    Returns ``(granted, why)`` where ``why`` is a short machine tag
    (``granted`` | ``absent`` | ``duplicate`` | ``too_vacuous`` | ``vacuous``)
    for the trace. A reason is GRANTED only when it is non-empty, NOVEL versus
    every prior granted reason (normalized), clears the word floor, AND carries at
    least one concreteness signal — a concrete anchor (file/symbol/line) OR a
    falsifiability keyword (confirm/refute/hypothesis/…). This is a deliberately
    conservative heuristic: paired with the hard cap of one extension, the
    worst-case cost of a false grant is a single extra turn, while the common
    "let me keep looking" filler is reliably denied.
    """
    text = (reason or "").strip()
    if not text:
        return False, "absent"
    norm = _normalize_reason(text)
    prior_norm = {_normalize_reason(r) for r in (prior_reasons or [])}
    if norm in prior_norm:
        return False, "duplicate"
    if len(text.split()) < _MIN_REASON_WORDS:
        return False, "too_vacuous"
    has_anchor = bool(_CONCRETE_ANCHOR_RE.search(text))
    has_falsifiability = any(kw in norm for kw in _FALSIFIABILITY_KEYWORDS)
    if has_anchor or has_falsifiability:
        return True, "granted"
    return False, "vacuous"


# Injected the instant a scout crosses the wind-down threshold for the FIRST time
# (extension still available): commit now, or justify exactly one more read.
# STEP 2A (Phase 2): this is the FIRST post-brake turn and the toolset has just been
# narrowed to exactly {submit_findings, request_extension} — every exploration tool
# (grep/file_read/bash) is gone. The notice states that UP FRONT so the model does
# not reflexively re-issue a now-removed tool and burn this turn on an "unknown
# tool" error (a stray call records no extension reason and is denied → straight to
# the forced submit, wasting the offer).
EXTENSION_OFFER_NUDGE = (
    "Exploration budget spent — all exploration tools (grep/file_read/bash) have now been "
    "removed; ONLY submit_findings and request_extension are available, so do not call any other "
    "tool. Either COMMIT now by calling submit_findings, OR — only if you "
    "have a SPECIFIC, falsifiable reason to read exactly ONE more thing — call request_extension "
    "naming WHAT you will read (file:line or symbol) and WHICH hypothesis it confirms or refutes. "
    "You get AT MOST one extension; a vacuous, generic, or repeated reason is denied and you must "
    "submit_findings immediately. Choose one now."
)

# Injected when the harness GRANTS the single extension: one more read, then submit.
EXTENSION_GRANTED_NUDGE = (
    "Extension granted: ONE more exploration turn. Read the single thing you named, then your "
    "NEXT action MUST be submit_findings — no further extensions will be granted."
)

# Injected when the harness DENIES the extension (absent/vacuous/duplicate): force the submit.
EXTENSION_DENIED_NUDGE = (
    "Extension denied — your reason was absent, too vague, or a repeat of one already used. Your "
    "NEXT action MUST be submit_findings with the evidence you already have. Cite file:line / exact "
    "matched strings from your real tool results; mark each finding verified|unverified; set "
    "insufficient_evidence if you lack evidence — do NOT fabricate."
)


__all__ = [
    "EXTENSION_DENIED_NUDGE",
    "EXTENSION_GRANTED_NUDGE",
    "EXTENSION_OFFER_NUDGE",
    "REQUEST_EXTENSION_SCHEMA",
    "REQUEST_EXTENSION_TOOL_NAME",
    "RequestExtensionTool",
    "judge_extension_reason",
]
