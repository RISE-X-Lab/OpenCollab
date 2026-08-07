"""Unit tests for the Claude Code-derived nine-section compaction prompt."""

from __future__ import annotations

from pathlib import Path

from opencollab.application.compaction_prompt import (
    CLAUDE_CODE_COMPACTION_REFERENCE_URL,
    CLAUDE_CODE_LICENSE_URL,
    CLAUDE_CODE_REPOSITORY_URL,
    CLAUDE_CODE_TERMS_URL,
    build_continuation_message,
    build_summary_request,
    format_compact_summary,
    get_compact_prompt,
)

COMPACTION_SECTIONS = [
    "1. Primary Request and Intent",
    "2. Key Technical Concepts",
    "3. Files and Code Sections",
    "4. Errors and fixes",
    "5. Problem Solving",
    "6. All user messages",
    "7. Pending Tasks",
    "8. Current Work",
    "9. Optional Next Step",
]


def test_prompt_contains_all_claude_code_sections():
    prompt = get_compact_prompt()
    template = prompt.split("Structure your output as:", maxsplit=1)[1]
    positions = [template.index(section) for section in COMPACTION_SECTIONS]
    assert positions == sorted(positions)
    assert all(template.count(section) == 1 for section in COMPACTION_SECTIONS)


def test_prompt_preserves_directions_exact_next_action_and_scratchpad():
    prompt = get_compact_prompt()
    assert "ALL user and teammate messages" in prompt
    assert "Include verbatim quotes from the most recent conversation" in prompt
    assert "Do NOT call any tools" in prompt
    assert "<summary>" in prompt
    assert "<analysis>" in prompt


def test_compaction_source_records_verifiable_provenance():
    repository = Path(__file__).resolve().parents[1]
    source = (repository / "opencollab" / "application" / "compaction_prompt.py").read_text(encoding="utf-8")
    notices = (repository / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Anthropic Claude Code" in source
    assert "context-compaction-py" not in source
    expected_urls = (
        "https://github.com/anthropics/claude-code",
        "https://github.com/anthropics/claude-code/blob/main/LICENSE.md",
        "https://code.claude.com/docs/en/legal-and-compliance",
        "https://y-agent.github.io/inside-claude-code/04-context-compaction.html",
    )
    assert (
        CLAUDE_CODE_REPOSITORY_URL,
        CLAUDE_CODE_LICENSE_URL,
        CLAUDE_CODE_TERMS_URL,
        CLAUDE_CODE_COMPACTION_REFERENCE_URL,
    ) == expected_urls
    assert all(url in source for url in expected_urls)
    assert all(url in notices for url in expected_urls)


def test_compaction_provenance_stays_outside_model_instruction():
    prompt = get_compact_prompt()
    assert "Anthropic Claude Code" not in prompt
    assert CLAUDE_CODE_REPOSITORY_URL not in prompt
    assert CLAUDE_CODE_LICENSE_URL not in prompt
    assert CLAUDE_CODE_TERMS_URL not in prompt
    assert CLAUDE_CODE_COMPACTION_REFERENCE_URL not in prompt


def test_prompt_appends_custom_instructions_when_given():
    assert "Additional Instructions:\nfocus on db" in get_compact_prompt("focus on db")
    assert "Additional Instructions" not in get_compact_prompt()
    assert "Additional Instructions" not in get_compact_prompt("   ")


def test_format_strips_analysis_and_unwraps_summary():
    raw = "<analysis>private scratch reasoning</analysis>\n<summary>the real summary</summary>"
    out = format_compact_summary(raw)
    assert "private scratch reasoning" not in out
    assert "<analysis>" not in out and "<summary>" not in out
    assert out == "Summary:\nthe real summary"


def test_format_preserves_backslashes_as_literal_summary_text():
    raw = r"<summary>Use C:\code\app.py, then keep \1 and \g<1> literal.</summary>"
    assert format_compact_summary(raw) == (
        "Summary:\n"
        r"Use C:\code\app.py, then keep \1 and \g<1> literal."
    )


def test_format_keeps_each_summary_block_distinct():
    raw = "<summary>original draft</summary>\nBETWEEN\n<summary>corrected draft</summary>"
    assert format_compact_summary(raw) == (
        "Summary:\noriginal draft\nBETWEEN\nSummary:\ncorrected draft"
    )


def test_format_collapses_blank_lines_and_trims():
    raw = "<summary>line1\n\n\n\nline2</summary>"
    out = format_compact_summary(raw)
    assert out == "Summary:\nline1\n\nline2"


def test_format_returns_empty_for_no_usable_text():
    assert format_compact_summary("") == ""
    # Only an analysis block, no summary and nothing else → empty after strip.
    assert format_compact_summary("<analysis>just thinking</analysis>") == ""


def test_format_keeps_text_when_no_summary_tags():
    # Graceful: a model that ignored the tags still yields its prose.
    assert format_compact_summary("plain prose with no tags") == "plain prose with no tags"


def test_continuation_message_includes_transcript_pointer_when_supplied():
    msg = build_continuation_message("S", transcript_path="/runs/a0.json")
    assert msg["role"] == "user"
    assert "/runs/a0.json" in msg["content"]
    assert "Transcript location:" in msg["content"]


def test_continuation_message_omits_pointer_without_path():
    msg = build_continuation_message("S")
    assert "transcript" not in msg["content"].lower()
    assert "S" in msg["content"]


def test_continuation_message_flags_and_suppression():
    msg = build_continuation_message("S", recent_preserved=True, suppress_followups=True)
    assert "Messages after this record remain unchanged." in msg["content"]
    assert "Act on the latest unfinished or next-action item now." in msg["content"]
    quiet = build_continuation_message("S", suppress_followups=False)
    assert "Act on the latest unfinished or next-action item now." not in quiet["content"]


def test_continuation_message_accepts_legacy_eight_field_summary():
    legacy = """Summary:
Goal
Finish the release review.

Immediate next action
Run the final tests."""
    message = build_continuation_message(legacy)
    assert legacy in message["content"]
    assert "Act on the latest unfinished or next-action item now." in message["content"]
    assert "Optional Next Step" not in message["content"]


def test_build_summary_request_replays_segment_then_appends_prompt():
    segment = [
        {"role": "assistant", "content": "did work"},
        {"role": "user", "content": "more please"},
    ]
    request = build_summary_request(segment)
    assert request[:2] == segment  # replayed verbatim
    assert request[-1]["role"] == "user"
    assert "detailed summary of the conversation" in request[-1]["content"]
    # Pure: the input segment is not mutated.
    assert len(segment) == 2
