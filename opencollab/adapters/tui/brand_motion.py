"""Brand-motion primitive for the TUI: a single, gently pulsing brand dot.

A small, self-contained (stdlib + Rich) renderable — one ``●`` that *breathes*
(its brightness eases up and down) in a soft brand violet. The frame is a pure
function of *elapsed seconds*, computed at render time from ``time.monotonic()``.
Because ``rich.live.Live`` re-renders on its own timer, dropping one of these
into the live view animates it for free — no extra thread, loop, or ``sleep``,
and it keeps pulsing even when no new events arrive (which is what fixes the old
"frozen" LLM-wait line).

Kept deliberately calm: one soft-violet dot with a low-contrast breathe — not a
saturated, multi-colour sweep — so it sits quietly inside the TUI's calm HUD.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from rich.text import Text

# Soft brand violet — a light tint of the logo's #7C3AED, so the dot reads as
# "brand" without the heavy, saturated colour. Used for the pulsing dot and the
# static ◆ marker alike.
DOT_COLOR = (159, 122, 234)  # #9F7AEA
DOT_GLYPH = "●"              # ● U+25CF
MARK_HEX = "#9F7AEA"         # the static ◆ reply / banner marker — same soft violet

# Breathing model — a slow, smooth brightness pulse (a calm breathe, not a blink).
PULSE_PERIOD = 1.4     # seconds per full breath (dim → bright → dim)
PULSE_TROUGH = 0.45    # brightness factor at the dimmest point
PULSE_PEAK = 1.0       # brightness factor at the brightest point

RGB = tuple[int, int, int]


def _shade(color: RGB, k: float) -> RGB:
    """Scale an RGB triple by brightness factor ``k``, clamped to [0, 255]."""
    return tuple(max(0, min(255, round(channel * k))) for channel in color)  # type: ignore[return-value]


def _hex(color: RGB) -> str:
    """Rich-style truecolor hex (e.g. ``#9F7AEA``) for a Text segment style."""
    return "#{:02X}{:02X}{:02X}".format(*color)


def pulse_brightness(elapsed: float) -> float:
    """Eased breathing curve → brightness factor at ``elapsed`` seconds.

    A cosine ease starting at the trough, so the dot fades up and back down
    smoothly (a calm breathe rather than a hard on/off blink).
    """
    phase = (elapsed % PULSE_PERIOD) / PULSE_PERIOD       # 0..1 within a breath
    eased = 0.5 - 0.5 * math.cos(2 * math.pi * phase)     # 0 → 1 → 0
    return PULSE_TROUGH + (PULSE_PEAK - PULSE_TROUGH) * eased


def dot_color(elapsed: float) -> RGB:
    """Soft-violet dot colour at ``elapsed`` seconds (its brightness breathes)."""
    return _shade(DOT_COLOR, pulse_brightness(elapsed))


class PulseDot:
    """One-line Rich renderable: a single gently pulsing brand dot, an optional
    status label, and a live seconds counter.

    The rendered frame is a pure function of :meth:`elapsed`, so Live's periodic
    re-render animates it without any incoming events or a background thread.

    Elapsed is injectable for testing: pass ``elapsed=`` to pin a frame, or a
    ``clock`` + ``start`` to control the time source; otherwise it defaults to
    ``time.monotonic()`` measured from construction.
    """

    def __init__(
        self,
        label: str = "",
        *,
        muted_style: str = "grey46",
        show_seconds: bool = True,
        clock: Callable[[], float] = time.monotonic,
        start: float | None = None,
        elapsed: float | None = None,
    ) -> None:
        self.label = label
        self.muted_style = muted_style
        self.show_seconds = show_seconds
        self._clock = clock
        self._start = self._clock() if start is None else start
        self._fixed_elapsed = elapsed

    def elapsed(self) -> float:
        """Seconds since the dot started (or the pinned value, for tests)."""
        if self._fixed_elapsed is not None:
            return self._fixed_elapsed
        return max(0.0, self._clock() - self._start)

    def render(self) -> Text:
        """Compute the current frame as a fully-styled ``Text``.

        Every visible glyph carries an explicit, non-"white" style: the dot uses
        a truecolor brand hex, the label + seconds use the muted chrome style.
        """
        now = self.elapsed()
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(DOT_GLYPH, style=_hex(dot_color(now)))
        if self.label:
            text.append("  ", style=self.muted_style)
            text.append(self.label, style=self.muted_style)
        if self.show_seconds:
            separator = " · " if self.label else " "
            text.append(f"{separator}({int(now)}s)", style=self.muted_style)
        return text

    def __rich__(self) -> Text:
        return self.render()
