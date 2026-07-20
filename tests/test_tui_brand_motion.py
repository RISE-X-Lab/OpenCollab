"""Unit tests for the pulsing brand dot (adapters/tui/brand_motion).

Proves the dot is animated (its brightness breathes as elapsed seconds advance)
and that every emitted segment carries an explicit, non-"white" style so the
TUI's per-glyph chrome walk stays satisfied.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from opencollab.adapters.tui.brand_motion import (
    DOT_GLYPH,
    PulseDot,
    dot_color,
    pulse_brightness,
)


def _assert_all_non_white(text: Text) -> None:
    console = Console(color_system="truecolor")
    for offset, char in enumerate(text.plain):
        if char.isspace():
            continue
        style = text.get_style_at_offset(console, offset)
        assert style.color is not None
        assert style.color.name != "white"


def test_pulse_breathes_between_trough_and_peak():
    trough = pulse_brightness(0.0)   # a breath starts dim
    peak = pulse_brightness(0.7)     # half of a 1.4s period → brightest
    assert peak > trough
    assert 0.0 <= trough < peak <= 1.0


def test_dot_color_changes_as_time_advances():
    # Brightness breathes, so the shaded colour differs between two elapsed
    # values a fraction of a period apart — i.e. it animates.
    assert dot_color(0.0) != dot_color(0.7)


def test_frame_changes_between_elapsed_values():
    early = PulseDot(show_seconds=False, elapsed=0.0).render()
    later = PulseDot(show_seconds=False, elapsed=0.7).render()
    assert early.plain == later.plain   # same single glyph
    assert early.spans != later.spans   # different brightness → it pulsed


def test_renders_a_single_dot():
    text = PulseDot(show_seconds=False, elapsed=0.5).render()
    assert text.plain.strip() == DOT_GLYPH
    _assert_all_non_white(text)


def test_every_segment_carries_an_explicit_non_white_style():
    bar = PulseDot("Lead thinking… · step 4", elapsed=3.0)
    text = bar.render()
    _assert_all_non_white(text)
    assert "(3s)" in text.plain      # live seconds counter, floored to int
    assert "step 4" in text.plain


def test_elapsed_reads_from_injected_clock():
    now = {"t": 100.0}
    bar = PulseDot(clock=lambda: now["t"], start=100.0)
    assert bar.elapsed() == 0.0
    now["t"] = 102.5
    assert bar.elapsed() == 2.5
