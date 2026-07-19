# ruff: noqa: E501 — verbatim prompt text; wrapping section lines would alter the
# string the model sees, so long lines are intentional here.
"""The conversation-compaction summary prompt and its surrounding scaffolding.

Natively ported (not vendored) from Claude Code's compaction prompt, by way of
the ``context-compaction-py`` reference library
(``context_compaction/prompt.py``). The valuable parts kept faithful are:

* the **9 named sections** (vs. a few generic lines),
* the ``<analysis>`` scratchpad / ``<summary>`` output split, with a parser that
  strips the scratchpad, and
* **section 6 ("All user messages")** plus the **verbatim "Next Step" quote** —
  together these are what stop the agent from drifting off-task after a compact.

Everything here is pure: plain strings, a regex parser, and OpenAI-style message
``dict`` builders. No LLM calls, no message-model conversion, no async — so it
drops straight onto OpenCollab's existing dict message model.
"""

from __future__ import annotations

import re
from typing import Any

# Hard framing so the summarizer spends its single turn on prose, not tool calls.
NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

NO_TOOLS_TRAILER = "\n\nREMINDER: text only — do NOT call any tools."

_ANALYSIS_INSTRUCTION = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts. In your analysis:

1. Chronologically analyze each message. For each section identify:
   - The user's (or teammate's) explicit requests and intents
   - Your approach to addressing them
   - Key decisions, technical concepts and code patterns
   - Specific details: file names, full code snippets, function signatures, file edits
   - Errors you ran into and how you fixed them
   - Specific feedback, especially when told to do something differently.
2. Double-check for technical accuracy and completeness."""

BASE_COMPACT_PROMPT = f"""Your task is to create a detailed summary of the conversation so far, paying close attention to the explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions essential for continuing work without losing context.

{_ANALYSIS_INSTRUCTION}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the explicit requests and intents in detail.
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and why each file matters.
4. Errors and fixes: List all errors you ran into and how you fixed them, plus any feedback received.
5. Problem Solving: Document problems solved and ongoing troubleshooting.
6. All user messages: List ALL user and teammate messages that are not tool results. These are critical for understanding feedback and changing intent.
7. Pending Tasks: Outline any pending tasks you have explicitly been asked to work on.
8. Current Work: Describe precisely what was being worked on immediately before this summary, with file names and code snippets.
9. Optional Next Step: List the next step, DIRECTLY in line with the most recent explicit request. Include verbatim quotes from the most recent conversation showing exactly where you left off, to avoid drift.

Structure your output as:

<analysis>
[Your thought process]
</analysis>

<summary>
1. Primary Request and Intent:
2. Key Technical Concepts:
3. Files and Code Sections:
4. Errors and fixes:
5. Problem Solving:
6. All user messages:
7. Pending Tasks:
8. Current Work:
9. Optional Next Step:
</summary>
"""


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """The full instruction text handed to the summarizing model."""
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


def format_compact_summary(raw: str) -> str:
    """Strip the ``<analysis>`` scratchpad and unwrap ``<summary>`` to plain text.

    Returns ``""`` when the model produced neither a ``<summary>`` block nor any
    usable text, so callers can detect the empty case and fall back.
    """
    if not raw:
        return ""
    out = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw)
    match = re.search(r"<summary>([\s\S]*?)</summary>", out)
    if match:
        out = re.sub(
            r"<summary>[\s\S]*?</summary>",
            f"Summary:\n{(match.group(1) or '').strip()}",
            out,
        )
    out = re.sub(r"\n\n+", "\n\n", out)
    return out.strip()


def transcript_recovery_note(transcript_path: str) -> str:
    """One line telling the model where to recover pre-compaction detail."""
    return (
        "If you need specific details from before compaction (exact code "
        "snippets, error messages, content you generated), read the full "
        f"transcript at: {transcript_path}"
    )


def build_continuation_message(
    summary: str,
    *,
    suppress_followups: bool = True,
    transcript_path: str | None = None,
    recent_preserved: bool = False,
) -> dict[str, Any]:
    """The ``user`` message that seeds a post-compaction conversation.

    Returned as an OpenAI-style ``dict`` (not the library's typed ``Message``)
    so it slots directly into OpenCollab's message history.
    """
    body = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        f"conversation.\n\n{summary}"
    )
    if transcript_path:
        body += "\n\n" + transcript_recovery_note(transcript_path)
    if recent_preserved:
        body += "\n\nRecent messages are preserved verbatim."
    if suppress_followups:
        body += (
            "\nContinue the conversation from where it left off without asking "
            "any further questions. Resume directly — do not acknowledge the "
            "summary or recap. Pick up the last task as if the break never "
            "happened."
        )
    return {"role": "user", "content": body}


def build_summary_request(
    segment: list[dict[str, Any]],
    *,
    custom_instructions: str | None = None,
) -> list[dict[str, Any]]:
    """The message list sent to the model to summarize ``segment``.

    Mirrors the library's ``compact_conversation``: replay the segment, then
    append the compaction instruction as a final ``user`` message. ``segment``
    is expected to be group-aligned (every ``tool_call_id`` answered within it),
    so the replay is a valid provider request.
    """
    return [*segment, {"role": "user", "content": get_compact_prompt(custom_instructions)}]


__all__ = [
    "NO_TOOLS_PREAMBLE",
    "NO_TOOLS_TRAILER",
    "BASE_COMPACT_PROMPT",
    "get_compact_prompt",
    "format_compact_summary",
    "transcript_recovery_note",
    "build_continuation_message",
    "build_summary_request",
]
