"""Unit tests for the OpenCollab conversation handoff protocol."""

from __future__ import annotations

from pathlib import Path

from opencollab.application.compaction_prompt import (
    build_continuation_message,
    build_summary_request,
    format_compact_summary,
    get_compact_prompt,
)

HANDOFF_FIELDS = [
    "Goal",
    "User directions",
    "Completed work",
    "Technical state",
    "Decisions and constraints",
    "Failures and diagnostics",
    "Remaining work",
    "Immediate next action",
]


def test_prompt_contains_all_handoff_fields():
    prompt = get_compact_prompt()
    for field in HANDOFF_FIELDS:
        assert field in prompt


def test_prompt_preserves_directions_and_exact_next_action_without_scratchpad():
    prompt = get_compact_prompt()
    assert "every user or teammate instruction in chronological order" in prompt
    assert "quote the latest unfinished request exactly enough" in prompt
    assert "Do not invoke tools" in prompt
    assert "<summary>" in prompt
    assert "<analysis>" not in prompt


def test_prompt_has_no_external_prompt_provenance_claim():
    prompt = get_compact_prompt()
    assert "Claude" + " Code" not in prompt


def test_compaction_sources_have_no_porting_claims():
    repository = Path(__file__).resolve().parents[1]
    sources = [
        repository / "opencollab" / "application" / "compaction_prompt.py",
        repository / "opencollab" / "bootstrap" / "container.py",
        repository / "docs" / "archive" / "repomap" / "REPOMAP.md",
    ]
    forbidden = (
        "context-" + "compaction-py",
        "compact_" + "conversation",
        "ported" + " prompt",
        "nine-" + "section",
        "9-" + "section",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources).lower()
    assert all(phrase.lower() not in combined for phrase in forbidden)


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
    assert "Transcript location:" in msg["content"]


def test_continuation_message_omits_pointer_without_path():
    msg = build_continuation_message("S")
    assert "transcript" not in msg["content"].lower()
    assert "S" in msg["content"]


def test_continuation_message_flags_and_suppression():
    msg = build_continuation_message("S", recent_preserved=True, suppress_followups=True)
    assert "Messages after this record remain unchanged." in msg["content"]
    assert "Act on Immediate next action now." in msg["content"]
    quiet = build_continuation_message("S", suppress_followups=False)
    assert "Act on Immediate next action now." not in quiet["content"]


def test_build_summary_request_replays_segment_then_appends_prompt():
    segment = [
        {"role": "assistant", "content": "did work"},
        {"role": "user", "content": "more please"},
    ]
    request = build_summary_request(segment)
    assert request[:2] == segment  # replayed verbatim
    assert request[-1]["role"] == "user"
    assert "continuation record" in request[-1]["content"]
    # Pure: the input segment is not mutated.
    assert len(segment) == 2
