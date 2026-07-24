# ruff: noqa: E501 — wrapping prompt lines would alter the model instruction.
"""OpenCollab's independently authored conversation handoff protocol.

The protocol preserves the facts needed to continue a long-running task while
keeping the implementation provider-neutral. The module contains only strings,
a compatibility parser, and OpenAI-style message ``dict`` builders.
"""

from __future__ import annotations

import re
from typing import Any

# The summarizer receives the full segment, so it can produce the handoff
# without external reads or side effects.
NO_TOOLS_PREAMBLE = """Create a continuation record using plain text only. Do not invoke tools.

Use only the conversation already provided. Return exactly one <summary> block.

"""

NO_TOOLS_TRAILER = "\n\nReturn the completed <summary> block and nothing else."

BASE_COMPACT_PROMPT = """Prepare a continuation record for another agent that must resume this task without repeating completed work.

Use the following headings inside the <summary> block:

Goal
User directions
Completed work
Technical state
Decisions and constraints
Failures and diagnostics
Remaining work
Immediate next action

Record concrete facts rather than commentary. Preserve file paths, symbols, commands, error messages, identifiers, test results, and unresolved choices when they affect the next action. Under User directions, retain every user or teammate instruction in chronological order and make later corrections visibly supersede earlier ones. Under Completed work, distinguish verified outcomes from attempted work. Under Immediate next action, quote the latest unfinished request exactly enough to prevent a change of task.

Use this form:

<summary>
Goal
...

User directions
...

Completed work
...

Technical state
...

Decisions and constraints
...

Failures and diagnostics
...

Remaining work
...

Immediate next action
...
</summary>
"""


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """The full instruction text handed to the summarizing model."""
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


def format_compact_summary(raw: str) -> str:
    """Unwrap ``<summary>`` and accept legacy ``<analysis>`` prefixes.

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
    """Point to the archived evidence for details omitted from the handoff."""
    return (
        "Consult the archived session transcript when an exact command, code "
        "fragment, error, or earlier response is needed to complete the task. "
        f"Transcript location: {transcript_path}"
    )


def build_continuation_message(
    summary: str,
    *,
    suppress_followups: bool = True,
    transcript_path: str | None = None,
    recent_preserved: bool = False,
) -> dict[str, Any]:
    """Build the user message that resumes a session from a handoff."""
    body = (
        "Resume the active task using the continuation record below. Treat it "
        "as the authoritative handoff for earlier turns.\n\n"
        f"{summary}"
    )
    if transcript_path:
        body += "\n\n" + transcript_recovery_note(transcript_path)
    if recent_preserved:
        body += (
            "\n\nMessages after this record remain unchanged. Follow their "
            "newer directions when they supersede the record."
        )
    if suppress_followups:
        body += (
            "\n\nAct on Immediate next action now. Preserve outstanding "
            "constraints, avoid repeating completed steps, and respond to the "
            "active task directly without describing the handoff."
        )
    return {"role": "user", "content": body}


def build_summary_request(
    segment: list[dict[str, Any]],
    *,
    custom_instructions: str | None = None,
) -> list[dict[str, Any]]:
    """The message list sent to the model to summarize ``segment``.

    Replay the group-aligned segment, then append the handoff instruction as
    the final user message. Group alignment keeps every tool call paired with
    its result, producing a valid provider request.
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
