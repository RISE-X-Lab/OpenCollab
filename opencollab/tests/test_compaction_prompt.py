"""Unit tests for the ported 9-section compaction prompt and its scaffolding."""

from __future__ import annotations

from opencollab.application.compaction_prompt import (
    build_continuation_message,
    build_summary_request,
    format_compact_summary,
    get_compact_prompt,
)

NINE_SECTIONS = [
    "1. Primary Request and Intent:",
    "2. Key Technical Concepts:",
    "3. Files and Code Sections:",
    "4. Errors and fixes:",
    "5. Problem Solving:",
    "6. All user messages:",
    "7. Pending Tasks:",
    "8. Current Work:",
    "9. Optional Next Step:",
]


def test_prompt_contains_all_nine_sections():
    prompt = get_compact_prompt()
    for section in NINE_SECTIONS:
        assert section in prompt


def test_prompt_keeps_section_six_and_verbatim_next_step():
    prompt = get_compact_prompt()
    assert "All user messages" in prompt  # section 6 — anti-drift
    assert "verbatim quotes" in prompt  # section 9 — anti-drift
    # No-tools framing so the single turn is spent on prose.
    assert "Do NOT call any tools" in prompt
    assert "<analysis>" in prompt and "<summary>" in prompt


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
    assert "read the full transcript at:" in msg["content"]


def test_continuation_message_omits_pointer_without_path():
    msg = build_continuation_message("S")
    assert "transcript" not in msg["content"].lower()
    assert "S" in msg["content"]


def test_continuation_message_flags_and_suppression():
    msg = build_continuation_message("S", recent_preserved=True, suppress_followups=True)
    assert "Recent messages are preserved verbatim." in msg["content"]
    assert "without asking" in msg["content"]
    quiet = build_continuation_message("S", suppress_followups=False)
    assert "without asking" not in quiet["content"]


def test_build_summary_request_replays_segment_then_appends_prompt():
    segment = [
        {"role": "assistant", "content": "did work"},
        {"role": "user", "content": "more please"},
    ]
    request = build_summary_request(segment)
    assert request[:2] == segment  # replayed verbatim
    assert request[-1]["role"] == "user"
    assert "detailed summary" in request[-1]["content"]
    # Pure: the input segment is not mutated.
    assert len(segment) == 2
