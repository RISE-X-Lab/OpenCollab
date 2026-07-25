# ruff: noqa: E501 — wrapping prompt lines would alter the model instruction.
"""Claude Code-derived conversation compaction prompt and OpenCollab adapters.

The prompt follows a third-party technical description of the nine-section
conversation compaction prompt distributed with Anthropic Claude Code.
Anthropic's product repository, license, and legal terms are recorded
separately from that technical reference because the product repository does
not publish the corresponding core prompt source file.

Claude Code is a product of Anthropic PBC. OpenCollab is not affiliated with
or endorsed by Anthropic.
"""

from __future__ import annotations

import re
from typing import Any

CLAUDE_CODE_REPOSITORY_URL = "https://github.com/anthropics/claude-code"
CLAUDE_CODE_LICENSE_URL = "https://github.com/anthropics/claude-code/blob/main/LICENSE.md"
CLAUDE_CODE_TERMS_URL = "https://code.claude.com/docs/en/legal-and-compliance"
CLAUDE_CODE_COMPACTION_REFERENCE_URL = "https://y-agent.github.io/inside-claude-code/04-context-compaction.html"

# The summarizer receives the full segment, so it can produce the summary
# without external reads or side effects.
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
            "\n\nAct on the latest unfinished or next-action item now. Preserve outstanding "
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
    "CLAUDE_CODE_REPOSITORY_URL",
    "CLAUDE_CODE_LICENSE_URL",
    "CLAUDE_CODE_TERMS_URL",
    "CLAUDE_CODE_COMPACTION_REFERENCE_URL",
    "NO_TOOLS_PREAMBLE",
    "NO_TOOLS_TRAILER",
    "BASE_COMPACT_PROMPT",
    "get_compact_prompt",
    "format_compact_summary",
    "transcript_recovery_note",
    "build_continuation_message",
    "build_summary_request",
]
