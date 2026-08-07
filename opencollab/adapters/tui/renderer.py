"""TUI — Rich-based terminal interface with streaming and nested spinners.

Ref:
- kimi-cli: Wire pattern — bidirectional channel between agent and UI
- opencode: processor.ts events — text_delta, tool_start, tool_end, etc.
- openclaw: Rich terminal palette with dynamic updates

Split by concern (``self`` is unchanged — mixins run on the one TUI instance):

- ``renderer_events``  — event dispatch + timeline/status/roster updates
- ``renderer_display`` — Rich renderable building + style palette
- this module          — ``TUI`` state, Live lifecycle, and turn-level API
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from opencollab.adapters.tui.brand_motion import MARK_HEX, PulseDot
from opencollab.adapters.tui.keyboard import TabKeyNavigator
from opencollab.adapters.tui.renderer_display import _RendererDisplayMixin
from opencollab.adapters.tui.renderer_events import (
    MAX_TIMELINE_BLOCKS,
    _RendererEventsMixin,
)
from opencollab.domain.session import TERMINAL_PHASES

MAX_HISTORY_BLOCKS_PER_AGENT = 400
MAX_TERMINAL_AGENT_STATES = 128
MAX_TERMINAL_AGENT_SUMMARIES = 256
_TERMINAL_RENDER_STATES = frozenset(
    {
        "idle",
        "failed",
        "cancelled",
        *(phase.value for phase in TERMINAL_PHASES),
    }
)


@dataclass
class _AgentRenderState:
    """Mutable live-render state owned by one scheduler agent."""

    current_text: str = ""
    active_tools: dict[str, dict] = field(default_factory=dict)
    status_lines: list[Text] = field(default_factory=list)
    history_blocks: list[Any] = field(default_factory=list)
    history_omitted_blocks: int = 0
    timeline_blocks: list[Any] = field(default_factory=list)
    turn_history_start: int = 0
    history_revision: int = 0
    step: int = 0
    thinking: PulseDot | None = None


@dataclass(frozen=True)
class _AgentFocusTarget:
    """One keyboard-selectable live agent."""

    aid: int
    role: str


class TUI(_RendererEventsMixin, _RendererDisplayMixin):
    """Terminal UI that renders runtime + scheduler events in real-time.

    Consumes the event stream from Session (subscribed to its event bus)
    and renders streaming text, tool execution spinners, and status updates.
    The single ``event_handler`` accepts both ``SessionRuntimeEvent`` and
    ``SchedulerEvent`` and dispatches to the appropriate handler so that the
    spawn/review lifecycle no longer overloads session tool events.
    """

    def __init__(self, console: Console | None = None, *, filter_messages: bool = False):
        self.console = console or Console()
        self._agent_states: dict[int, _AgentRenderState] = {}
        # aid -> {"role": str, "state": str} for the live team roster panel.
        self._roster: dict[int, dict] = {}
        self._terminal_agent_order: list[int] = []
        self._terminal_agent_summaries: dict[int, dict[str, Any]] = {}
        self._terminal_summary_order: list[int] = []
        self._terminal_summaries_omitted = 0
        # Brand motion (single pulsing dot). ``_motion`` is the shared,
        # seconds-less tool-execution spinner. Each agent owns its own LLM-wait
        # indicator so hidden streams remain intact while another agent is selected.
        self._motion = PulseDot(muted_style=self._STYLE_MUTED, show_seconds=False)
        self._live: Live | None = None
        self._live_paused = False
        # Retained as a compatibility input while the renderer moves to one
        # lossless per-agent view. It must never control event collection.
        self._filter_messages = filter_messages
        self._selected_aid = 0
        self._state_for(0)
        # Optional callable returning the full team roster (live agents +
        # configured "available" roles). When set, the team panel renders from
        # it so the roster stays visible during a turn, not only after a spawn.
        self._team_provider: Any | None = None
        self._keyboard_controller: Any | None = TabKeyNavigator(
            self.select_previous_agent,
            self.select_next_agent,
        )
        self._keyboard_resume_pending = False
        self._holding_for_exit = False

    def _state_for(self, aid: int) -> _AgentRenderState:
        """Return one agent's render state, creating it on first observation."""
        return self._agent_states.setdefault(aid, _AgentRenderState())

    def _append_history_block(self, state: _AgentRenderState, block: Any) -> None:
        state.history_blocks.append(block)
        overflow = len(state.history_blocks) - MAX_HISTORY_BLOCKS_PER_AGENT
        if overflow > 0:
            del state.history_blocks[:overflow]
            state.history_omitted_blocks += overflow
            state.turn_history_start = max(0, state.turn_history_start - overflow)

    def _append_timeline_block(self, state: _AgentRenderState, block: Any) -> None:
        state.timeline_blocks.append(block)
        overflow = len(state.timeline_blocks) - MAX_TIMELINE_BLOCKS
        if overflow > 0:
            del state.timeline_blocks[:overflow]

    def _track_agent_render_lifecycle(self, aid: int, state: str) -> None:
        if aid in self._terminal_summary_order:
            self._terminal_summary_order.remove(aid)
        self._terminal_agent_summaries.pop(aid, None)
        if aid in self._terminal_agent_order:
            self._terminal_agent_order.remove(aid)
        if aid != 0 and state in _TERMINAL_RENDER_STATES:
            self._terminal_agent_order.append(aid)
        while len(self._terminal_agent_order) > MAX_TERMINAL_AGENT_STATES:
            victim = next(
                (
                    candidate
                    for candidate in self._terminal_agent_order
                    if candidate != self._selected_aid
                ),
                None,
            )
            if victim is None:
                break
            self._terminal_agent_order.remove(victim)
            render_state = self._agent_states.pop(victim, None)
            roster = self._roster.pop(victim, {})
            self._terminal_agent_summaries[victim] = {
                "aid": victim,
                "role": str(roster.get("role", "agent")),
                "state": str(roster.get("state", "idle")),
                "step": render_state.step if render_state is not None else 0,
                "retained_history_blocks": (
                    len(render_state.history_blocks) if render_state is not None else 0
                ),
                "omitted_history_blocks": (
                    render_state.history_omitted_blocks
                    if render_state is not None
                    else 0
                ),
            }
            self._terminal_summary_order.append(victim)
            while len(self._terminal_summary_order) > MAX_TERMINAL_AGENT_SUMMARIES:
                forgotten = self._terminal_summary_order.pop(0)
                self._terminal_agent_summaries.pop(forgotten, None)
                self._terminal_summaries_omitted += 1

    def _event_aid(self, aid: Any) -> int:
        """Normalize legacy events without an aid to agent 0, independent of focus."""
        return aid if isinstance(aid, int) and aid >= 0 else 0

    def _agent_label(self, aid: int) -> str:
        """Return the stable display label for an agent.

        Child agents keep compact ``A<n>`` labels. Agent 0 uses its configured
        entry role, while the built-in team naturally remains ``Lead``.
        """
        if aid != 0:
            return f"A{aid}"
        role = next(
            (role for entry_aid, role, _state in self._team_entries() if entry_aid == 0),
            "lead",
        )
        label = str(role).strip().replace("_", " ").replace("-", " ")
        return label.title() or "Lead"

    @property
    def _selected_state(self) -> _AgentRenderState:
        return self._state_for(self._selected_aid)

    # Compatibility accessors keep the display mixin and narrow external tests
    # focused on the selected agent while all event data remains separately held.
    @property
    def _current_text(self) -> str:
        return self._selected_state.current_text

    @_current_text.setter
    def _current_text(self, value: str) -> None:
        state = self._selected_state
        if state.current_text != value:
            state.current_text = value
            state.history_revision += 1

    @property
    def _active_tools(self) -> dict[str, dict]:
        return self._selected_state.active_tools

    @_active_tools.setter
    def _active_tools(self, value: dict[str, dict]) -> None:
        self._selected_state.active_tools = value

    @property
    def _status_lines(self) -> list[Text]:
        return self._selected_state.status_lines

    @_status_lines.setter
    def _status_lines(self, value: list[Text]) -> None:
        self._selected_state.status_lines = value

    @property
    def _timeline_blocks(self) -> list[Any]:
        return self._selected_state.timeline_blocks

    @_timeline_blocks.setter
    def _timeline_blocks(self, value: list[Any]) -> None:
        blocks = list(value)
        self._selected_state.timeline_blocks = blocks[-MAX_TIMELINE_BLOCKS:]
        self._selected_state.history_blocks = blocks[-MAX_HISTORY_BLOCKS_PER_AGENT:]
        self._selected_state.history_omitted_blocks = max(
            0, len(blocks) - MAX_HISTORY_BLOCKS_PER_AGENT
        )
        self._selected_state.history_revision += 1

    @property
    def _step(self) -> int:
        return self._selected_state.step

    @_step.setter
    def _step(self, value: int) -> None:
        self._selected_state.step = value

    @property
    def _thinking(self) -> PulseDot | None:
        return self._selected_state.thinking

    @_thinking.setter
    def _thinking(self, value: PulseDot | None) -> None:
        self._selected_state.thinking = value

    def set_team_provider(self, provider: Any) -> None:
        """Supply a callable returning the full team roster so the live display
        shows the team continuously (matching the prompt's bottom toolbar)."""
        self._team_provider = provider

    def set_keyboard_controller(self, controller: Any) -> None:
        """Attach the turn-scoped Tab-key controller used by the CLI."""
        self._keyboard_controller = controller

    @property
    def selected_aid(self) -> int:
        return self._selected_aid

    @property
    def terminal_agent_summaries(self) -> tuple[dict[str, Any], ...]:
        """Bounded summaries for terminal agents evicted from detailed TUI state."""
        return tuple(
            dict(self._terminal_agent_summaries[aid])
            for aid in self._terminal_summary_order
        )

    @property
    def terminal_agent_summaries_omitted(self) -> int:
        """Number of oldest summaries omitted to retain a stable memory bound."""
        return self._terminal_summaries_omitted

    @property
    def selected_role(self) -> str | None:
        """Compatibility view: configured-only roles are not input targets."""
        return None

    @property
    def selected_history_cache_key(self) -> tuple[int, int, int]:
        """Stable Prompt Toolkit cache key for the current history rendering."""
        state = self._selected_state
        return self._selected_aid, state.history_revision, self.console.width

    def _focus_targets(self) -> list[_AgentFocusTarget]:
        """Return live agents in stable aid order.

        Configured-but-unspawned roles remain visible in the roster, but without
        an aid they cannot own history or receive user input.
        """
        live_roles: dict[int, str] = {}

        for aid, role, state in self._team_entries():
            role = str(role)
            if not isinstance(aid, int) or aid < 0:
                continue
            if (
                aid == 0
                or aid in self._agent_states
                or state not in _TERMINAL_RENDER_STATES
            ):
                live_roles.setdefault(aid, role)

        for aid in self._agent_states:
            fallback_role = "lead" if aid == 0 else "agent"
            role = str(self._roster.get(aid, {}).get("role", fallback_role))
            live_roles.setdefault(aid, role)

        return [
            _AgentFocusTarget(aid=aid, role=live_roles[aid])
            for aid in sorted(live_roles)
        ]

    def _focus_is_selected(self, target: _AgentFocusTarget) -> bool:
        return self._selected_aid == target.aid

    def _select_focus(self, target: _AgentFocusTarget) -> int:
        changed = not self._focus_is_selected(target)
        self._selected_aid = target.aid
        if changed:
            self._refresh()
        return self._selected_aid

    def select_agent(self, aid: int) -> int:
        """Select an existing agent and return the resulting aid."""
        target = next(
            (candidate for candidate in self._focus_targets() if candidate.aid == aid),
            None,
        )
        return self._selected_aid if target is None else self._select_focus(target)

    def select_relative_agent(self, offset: int) -> int:
        """Move through live team members with wraparound."""
        targets = self._focus_targets()
        if not targets:
            return self._selected_aid
        index = next(
            (
                position
                for position, target in enumerate(targets)
                if self._focus_is_selected(target)
            ),
            0,
        )
        return self._select_focus(targets[(index + offset) % len(targets)])

    def select_previous_agent(self) -> int:
        return self.select_relative_agent(-1)

    def select_next_agent(self) -> int:
        return self.select_relative_agent(1)

    def record_user_message(self, aid: int, content: str) -> None:
        """Add one user turn to the target's redrawable transcript."""
        state = self._state_for(aid)
        self._flush_current_text_to_timeline(state)
        block = self._user_block(content)
        self._append_history_block(state, block)
        self._append_timeline_block(state, block)
        state.history_revision += 1

    def _refresh(self) -> None:
        """Re-render the current state."""
        if self._live and not self._live_paused:
            self._live.update(self._build_live_display())

    def start_live(self) -> None:
        """Start the Live display context."""
        self._live_paused = False
        # The turn begins by waiting on the model — show the animated wait bar so
        # first-token latency doesn't read as frozen. step_start refines it with
        # the step counter; text/tool progress clears it.
        label = self._agent_label(self._selected_aid)
        self._selected_state.thinking = self._new_thinking_bar(f"{label} thinking…")
        self._live = Live(
            self._build_live_display(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
            vertical_overflow="crop",
        )
        self._live.start()
        if self._keyboard_controller is not None:
            self._keyboard_controller.start()

    async def hold_live(self) -> bool:
        """Keep a completed TTY Live view open until the user presses ``q``.

        Returns ``False`` when no interactive keyboard controller is active, so
        redirected and other non-TTY one-shot runs never wait for input.
        """
        controller = self._keyboard_controller
        set_quit_callback = getattr(controller, "set_quit_callback", None)
        if (
            self._live is None
            or controller is None
            or not getattr(controller, "active", False)
            or not callable(set_quit_callback)
        ):
            return False

        released = asyncio.Event()
        set_quit_callback(released.set)
        self._holding_for_exit = True
        try:
            self._refresh()
            await released.wait()
            return True
        finally:
            set_quit_callback(None)
            self._holding_for_exit = False

    def suspend_live(self) -> bool:
        """Temporarily stop Live updates while preserving render state."""
        if not self._live or self._live_paused:
            return False
        if self._keyboard_controller is not None:
            self._keyboard_resume_pending = self._keyboard_controller.stop()
        self._live.stop()
        self._live = None
        self._live_paused = True
        return True

    def resume_live(self, was_suspended: bool) -> None:
        """Resume Live after suspend_live was used."""
        if not was_suspended or not self._live_paused:
            return
        self._live = Live(
            self._build_live_display(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
            vertical_overflow="crop",
        )
        self._live_paused = False
        self._live.start()
        if self._keyboard_resume_pending and self._keyboard_controller is not None:
            self._keyboard_controller.start()
        self._keyboard_resume_pending = False

    def stop_live(self, *, persist: bool = False, aid: int | None = None) -> None:
        """Stop the live HUD while retaining each bounded recent transcript.

        Interactive transcripts stay inside the redrawable Prompt Toolkit view
        with an explicit marker when older blocks were omitted. Full history
        remains owned by the persistent transcript/trace. One-shot callers opt
        into ``persist`` to print the target's settled turn to ordinary terminal
        scrollback.
        """
        set_quit_callback = getattr(self._keyboard_controller, "set_quit_callback", None)
        if callable(set_quit_callback):
            set_quit_callback(None)
        self._holding_for_exit = False
        if self._keyboard_controller is not None:
            self._keyboard_controller.stop()
        self._keyboard_resume_pending = False
        settled = None
        if self._live:
            if persist:
                settled = self._build_settled_display(
                    aid=self._selected_aid if aid is None else aid
                )
            self._live.stop()
            self._live = None
        if settled is not None:
            self.console.print(settled)
        self._live_paused = False
        for state in self._agent_states.values():
            self._flush_current_text_to_timeline(state)
            state.status_lines.clear()

    def print_welcome(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        workspace: str | None = None,
        budget: int | None = None,
        interactive: bool = True,
    ) -> None:
        """Compact 'Calm HUD' banner: wordmark + tagline, then an optional
        aligned key/value metadata line. All fields are optional so a bare
        ``print_welcome()`` still renders a clean two-line banner."""
        import os

        from rich.console import Group
        from rich.rule import Rule

        title = Text.assemble(
            ("◆ ", MARK_HEX),
            ("OpenCollab", self._STYLE_HEADING),
            ("  multi-agent dev", self._STYLE_MUTED),
        )
        tagline_text = (
            "Type a message · Ctrl+C interrupts · 'exit' quits"
            if interactive
            else "Running issue · Ctrl+C interrupts"
        )
        tagline = Text(tagline_text, style=self._STYLE_MUTED)

        fields: list[tuple[str, str]] = []
        if provider or model:
            fields.append(("model", f"{provider + ':' if provider else ''}{model or '?'}"))
        if workspace:
            home = os.path.expanduser("~")
            cwd = os.path.abspath(str(workspace))
            if home != "~" and cwd.startswith(home):
                cwd = "~" + cwd[len(home):]
            fields.append(("cwd", cwd))
        if budget is not None:
            fields.append(("budget", f"{budget:,} tok"))

        body: list[Any] = [title, tagline]
        if fields:
            meta = Text(no_wrap=True, overflow="ellipsis")
            for i, (key, val) in enumerate(fields):
                if i:
                    meta.append("   ", style=self._STYLE_MUTED)  # 3-space gutter
                meta.append(f"{key} ", style=self._STYLE_KEY)
                meta.append(val, style=self._STYLE_SUBTLE)
            body += [Rule(style=self._STYLE_MUTED), meta]

        self.console.print(Panel.fit(
            Group(*body),
            border_style=self._STYLE_MUTED,
            padding=(0, 2),
        ))

    def print_stats(self, tokens: int, steps: int) -> None:
        """Quiet hairline-led receipt under a completed turn."""
        self.console.print()  # one breathing line
        self.console.print(Text.assemble(
            ("─ ", self._STYLE_MUTED),
            (f"{tokens:,}", self._STYLE_SUBTLE), (" tokens", self._STYLE_MUTED),
            ("  ·  ", self._STYLE_MUTED),
            (f"{steps}", self._STYLE_SUBTLE), (" steps", self._STYLE_MUTED),
        ))

    def print_turn_divider(self) -> None:
        """Hairline rule between turns (Rule is exempt from the glyph walk)."""
        from rich.rule import Rule

        self.console.print(Rule(style=self._STYLE_MUTED))

    def reset(self) -> None:
        """Reset live chrome for the next turn while retaining agent history."""
        for state in self._agent_states.values():
            self._flush_current_text_to_timeline(state)
            state.active_tools.clear()
            state.status_lines.clear()
            state.timeline_blocks.clear()
            state.turn_history_start = len(state.history_blocks)
            state.step = 0
            state.thinking = None
        self._state_for(0)
