"""SubmitFindingsTool — a scout's structured, cite-or-abstain recon report (STEP 0).

A schema-capture tool in the same family as ``StructuredOutputTool``: the
workflow engine injects one into a read-only scout session so the scout's report
is a *structured artifact* the runtime owns, not fragile last-message text. It
is available to the scout normally (so it CAN commit early — commit-first
friendly) AND becomes the ONLY tool during the enforcement wind-down, where the
scout's single protected turn must call it.

Cite-or-abstain (anti-fabrication): every finding marked ``verified=true`` must
carry a concrete ``evidence_anchor`` (a ``file:line`` or an exact string copied
from a real tool result). A scout that lacks evidence for its dimension sets
``insufficient_evidence=true`` — an explicit, VALID, non-penalized abstention,
NOT a reason to invent a citation. Post-validation enforces this lightly: a
``verified`` finding without an anchor is bounced back for correction instead of
being captured.

Pure application layer: stdlib + application imports only (no adapters import —
the workflow engine that injects this lives in ``application`` and the Clean
Architecture boundary forbids an inward layer importing an outer one).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opencollab.application.schema_validate import validate
from opencollab.application.tool_execution import ToolRuntime

SUBMIT_TOOL_NAME = "submit_findings"

# Confidence is a free-form short string (e.g. "high"/"medium"/"low") rather than
# an enum so a model that phrases it differently is not bounced on a label.
SUBMIT_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings", "summary", "insufficient_evidence"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["aspect", "claim", "evidence_anchor", "verified", "confidence"],
                "properties": {
                    "aspect": {"type": "string", "description": "Which sub-question this addresses."},
                    "claim": {"type": "string", "description": "The concrete finding."},
                    "evidence_anchor": {
                        "type": "string",
                        "description": "file:line OR an exact string copied from a real tool "
                        "result that backs the claim. Leave empty ONLY when verified is false.",
                    },
                    "verified": {
                        "type": "boolean",
                        "description": "True only if a real tool result (read/grep) confirms the "
                        "claim and the anchor points at it.",
                    },
                    "confidence": {"type": "string", "description": "high | medium | low."},
                },
            },
        },
        "summary": {"type": "string", "description": "One or two sentences answering the dimension."},
        "insufficient_evidence": {
            "type": "boolean",
            "description": "True if you could not gather enough evidence to answer — a VALID "
            "outcome. Do NOT fabricate findings to avoid setting this.",
        },
    },
}


class SubmitFindingsTool:
    """Captures a schema-validated, cite-or-abstain findings payload.

    Implements ``ToolPort`` structurally (no adapter base), mirroring
    ``StructuredOutputTool``. Stateful: ``captured`` holds the last accepted
    payload (``None`` until one is accepted). One instance per scout session, so
    there is no cross-call leakage.
    """

    name = SUBMIT_TOOL_NAME
    default_timeout: float | None = None
    disable_outer_timeout = False
    description = (
        "Submit your reconnaissance findings as structured evidence. Call this to "
        "commit your report. Each finding marked verified=true MUST carry an "
        "evidence_anchor (file:line or an exact string from a real tool result); if "
        "you lack evidence for your dimension, set insufficient_evidence=true rather "
        "than inventing one. You will be told if a finding is missing its anchor; "
        "fix it and call submit_findings again."
    )

    def __init__(self, on_capture: Callable[[], None] | None = None) -> None:
        self._on_capture = on_capture
        self.parameters = SUBMIT_FINDINGS_SCHEMA
        self.captured: dict[str, Any] | None = None
        self.terminal_capture_accepted = False

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": SUBMIT_FINDINGS_SCHEMA,
            },
        }

    async def execute_with_runtime(self, params: dict[str, Any], runtime: ToolRuntime) -> str:
        self.terminal_capture_accepted = False
        errors = validate(params, SUBMIT_FINDINGS_SCHEMA)
        if errors:
            joined = "; ".join(errors)
            return (
                "Validation failed; the output does not conform to the schema. Fix and call "
                f"{self.name} again. Errors: {joined}"
            )
        findings = params.get("findings") or []
        summary = str(params.get("summary") or "").strip()
        if not findings:
            if not params.get("insufficient_evidence"):
                return (
                    "Empty report rejected: provide at least one finding, or set "
                    "insufficient_evidence=true and explain the evidence gap in summary."
                )
            if not summary:
                return (
                    "Abstention rejected: explain why evidence is insufficient in summary, "
                    "then call submit_findings again."
                )
        # Abstention describes the report as a whole; it never turns an uncited
        # verified claim into evidence. Mixed payloads may retain partial cited
        # findings while abstaining on the remaining dimension.
        uncited = [
            i
            for i, finding in enumerate(params.get("findings") or [])
            if finding.get("verified")
            and not str(finding.get("evidence_anchor") or "").strip()
        ]
        if uncited:
            return (
                "Cite-or-abstain: every finding marked verified=true MUST carry a concrete "
                "evidence_anchor (file:line or an exact matched string from a real tool "
                f"result). These findings are missing it: {uncited}. Add the anchor or set "
                "verified=false, then call submit_findings again. Do NOT fabricate an anchor."
            )
        self.captured = params
        self.terminal_capture_accepted = True
        if self._on_capture is not None:
            self._on_capture()
        return "Recorded. Findings accepted. Your reconnaissance is complete."


def format_findings_report(captured: dict[str, Any]) -> str:
    """Render a captured payload as the dense, labelled findings text the planner
    reads. Marks each finding verified|unverified and shows its anchor."""
    lines: list[str] = []
    summary = str(captured.get("summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary}")
    for finding in captured.get("findings") or []:
        aspect = str(finding.get("aspect") or "").strip()
        claim = str(finding.get("claim") or "").strip()
        anchor = str(finding.get("evidence_anchor") or "").strip()
        confidence = str(finding.get("confidence") or "").strip()
        tag = "verified" if finding.get("verified") else "unverified"
        anchor_part = f" [{anchor}]" if anchor else ""
        prefix = f"({aspect}) " if aspect else ""
        suffix = f" — {tag}" + (f", confidence={confidence}" if confidence else "")
        lines.append(f"- {prefix}{claim}{anchor_part}{suffix}")
    if captured.get("insufficient_evidence"):
        lines.append(
            "(insufficient_evidence: the scout reports it could not gather enough evidence to "
            "fully answer this dimension)"
        )
    return "\n".join(lines)


# Cap on how much of each salvaged tool result / interim text the partial blob
# keeps, so a chopped scout's harvest can never itself blow the planner's context.
_PARTIAL_SNIPPET_CHARS = 600


def format_evidence_ledger(cards: list[dict[str, Any]] | None) -> str:
    """Render the runtime-authored evidence ledger as compact, labelled
    lines: one ``- [outcome] tool target: snippet`` per executed scout tool call.

    The ledger is built purely from tool-result envelopes (no model involvement),
    so this is a faithful, deterministic record of what the scout actually saw —
    used both by the harvest backstop and as the dead-scout synthesizer's input.
    """
    lines: list[str] = []
    for card in cards or []:
        tool = str(card.get("tool") or "").strip()
        target = str(card.get("target") or "").strip()
        outcome = str(card.get("outcome") or "").strip()
        snippet = str(card.get("snippet") or "").strip()
        head = f"- [{outcome}] {tool} {target}".rstrip()
        lines.append(f"{head}: {snippet}" if snippet else head)
    return "\n".join(lines)


def harvest_findings(
    captured: dict[str, Any] | None,
    fallback_text: str,
    messages: list[dict[str, Any]] | None,
    ledger: list[dict[str, Any]] | None = None,
    draft: str | None = None,
) -> str:
    """The harvest backstop: turn whatever a scout produced into a usable report.

    Priority: (1) the scout's own captured submit_findings payload, formatted (its
    refined commit always wins); else (2) the commit-first ``draft`` (STEP 5b — the
    fact-sheet-seeded structured cite-or-abstain artifact) so a scout that dies or
    strays before refining never loses the fact-sheet anchors; else (3) the scout's
    final assistant text (today's behavior); else (4) a "(partial …)" salvage built
    from what the scout actually gathered — preferring the runtime-authored evidence
    ``ledger`` (STEP 2, classified + targeted) when present, else the raw tool
    results + interim assistant texts in the transcript — so a chopped scout yields
    what it found, never a bare "(scout died)". Returns "" only when there is
    genuinely nothing, letting the caller's own fallback decide; this function never
    fabricates. ``draft`` defaults ``None`` (no commit-first), keeping the harvest
    byte-for-byte identical for every caller that does not seed one.
    """
    if captured is not None:
        report = format_findings_report(captured)
        if report.strip():
            return report
    # STEP 5b: the seeded fact-sheet draft is a structured cite-or-abstain artifact —
    # rank it above raw prose / a transcript partial so the anchors survive a scout
    # that never produced its own refine. (The caller's dead-scout synth, built from
    # the scout's REAL reads, still overrides this when a ledger exists.)
    if draft and draft.strip():
        return draft
    if fallback_text and fallback_text.strip():
        return fallback_text
    if ledger:
        body = format_evidence_ledger(ledger)
        if body.strip():
            return f"(partial — scout chopped; {len(ledger)} evidence cards)\n{body}"
    msgs = messages or []
    tool_results = [m for m in msgs if m.get("role") == "tool"]
    interim = [
        str(m.get("content"))
        for m in msgs
        if m.get("role") == "assistant" and m.get("content")
    ]
    if not tool_results and not interim:
        return ""
    parts = [f"(partial — scout chopped; {len(tool_results)} tool results)"]
    for m in tool_results:
        parts.append(str(m.get("content") or "")[:_PARTIAL_SNIPPET_CHARS])
    for text in interim:
        parts.append(text[:_PARTIAL_SNIPPET_CHARS])
    return "\n\n".join(parts)


# Cap on the raw tool-result block fed into the dead-scout synthesizer prompt, so
# the single bounded salvage call can never itself overflow the model window.
_SYNTH_RESULT_CHARS = 800
_SYNTH_MAX_RESULTS = 12

# The dead-scout synthesizer's instruction (STEP 2 part b). It is a transcript-ONLY
# salvage: the model is given the evidence the scout already gathered and the single
# submit_findings tool — NO exploration tools — and must commit cite-or-abstain.
_DEAD_SCOUT_SYNTH_PROMPT = """\
You are salvaging a reconnaissance scout that ran out of budget WITHOUT committing \
its findings. You have NO exploration tools — do not ask to read or grep anything. \
Your ONLY job is to call submit_findings, synthesizing ONLY what the evidence the \
scout already gathered (below) supports.

Cite-or-abstain: for each finding you mark verified=true, copy a concrete \
evidence_anchor (a file:line or an exact string) FROM the evidence below. If the \
evidence is too thin to answer the dimension, set insufficient_evidence=true — a \
valid outcome. Do NOT fabricate findings or anchors for evidence you were not given.

Runtime-recorded evidence ledger (what the scout's tool calls returned):
{ledger}

Raw tool results captured during the scout's run:
{results}"""


def _format_tool_results_for_synth(messages: list[dict[str, Any]] | None) -> str:
    """Compact, bounded rendering of the scout's raw tool results for the
    synthesizer prompt — capped in count and per-result length so the single
    salvage call cannot overflow the model window."""
    results = [m for m in (messages or []) if m.get("role") == "tool"]
    if not results:
        return "(no raw tool results captured)"
    rendered = [
        str(m.get("content") or "")[:_SYNTH_RESULT_CHARS]
        for m in results[:_SYNTH_MAX_RESULTS]
    ]
    return "\n---\n".join(r for r in rendered if r.strip()) or "(no raw tool results captured)"


def build_dead_scout_synthesis_prompt(
    ledger: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]] | None,
) -> str:
    """Seed prompt for the transcript-only dead-scout synthesizer (STEP 2 part b).

    Its ONLY inputs are the runtime-authored evidence ledger and the scout's raw
    tool results — never a directive to explore further. The single allowed tool is
    submit_findings (forced + cite-or-abstain), so the salvage cannot fabricate.
    """
    ledger_text = format_evidence_ledger(ledger) or "(empty ledger)"
    return _DEAD_SCOUT_SYNTH_PROMPT.format(
        ledger=ledger_text, results=_format_tool_results_for_synth(messages)
    )


def commitment_terminus_payload(
    *,
    role: str | None,
    captured: dict[str, Any] | None,
    wind_down_done: bool,
    used_tokens: int,
    max_budget_tokens: int,
    wind_down_token_mark: int,
    artifact: str,
) -> dict[str, Any]:
    """Build the per-agent ``commitment_terminus`` metric event payload.

    terminus: ``voluntary`` (committed before any wind-down), ``forced`` (committed
    on the protected wind-down turn), ``chopped`` (ran out with no commit, no
    wind-down), or ``strayed`` (wind-down fired but the scout still did not commit).
    ``submit_turn_cost`` (the tokens the protected turn spent) lets us calibrate the
    reserve to a p95 later.
    """
    findings = (captured or {}).get("findings") or []
    if captured is not None:
        terminus = "forced" if wind_down_done else "voluntary"
    else:
        terminus = "strayed" if wind_down_done else "chopped"
    return {
        "role": role,
        "terminus": terminus,
        "budget_slack": max(0, int(max_budget_tokens) - int(used_tokens)),
        "artifact_nonempty": bool(artifact and artifact.strip()),
        "submit_turn_cost": max(0, int(used_tokens) - int(wind_down_token_mark)) if wind_down_done else 0,
        "evidence_anchor_count": sum(
            1 for f in findings if str(f.get("evidence_anchor") or "").strip()
        ),
        "unverified_count": sum(1 for f in findings if not f.get("verified")),
    }


__all__ = [
    "SUBMIT_FINDINGS_SCHEMA",
    "SUBMIT_TOOL_NAME",
    "SubmitFindingsTool",
    "build_dead_scout_synthesis_prompt",
    "commitment_terminus_payload",
    "format_evidence_ledger",
    "format_findings_report",
    "harvest_findings",
]
