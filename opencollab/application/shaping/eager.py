"""Eager, always-on tool-output clearing (cache-ready front rung).

Unlike the reactive history layers (``reactive``) which no-op until an estimated
context size crosses a trigger, this layer runs *unconditionally* on every model
call. It is the cheapest, first/always-on rung at the FRONT of the shaping chain:
it bounds file_read / tool-result accumulation BEFORE the 120k-gated reactive
pipeline ever fires.

It is a read-time projection over a *copy* of the message history: ``state.messages``
and the persisted transcript stay untouched. It keeps the ``K`` most-recent
compactable tool results verbatim and replaces every OLDER compactable result's
content with a deterministic, message-derived stub. Pinned sources (identity /
team / task, ``priority >= PIN_FLOOR``) and non-compactable messages are never
touched, and the assistant ``tool_call`` <-> ``tool``-result pairing skeleton is
preserved (the message and its ``tool_call_id`` survive — only the content shrinks).

CACHE-READY PROPERTIES (unit-tested in ``tests/test_eager_tool_clear_shaper.py``):

* **Deterministic** — ``shape(x)`` is byte-identical across repeated calls and
  does NOT read any running token estimate / context-size measurement.
* **Monotonic** — as the conversation grows by one tool exchange, a result
  transitions full -> stub exactly once (when it ages past ``K``) and never back;
  every already-stubbed message stays byte-identical, so the deep prefix stays
  cacheable.
* **Idempotent** — ``shape(shape(x)) == shape(x)``.

OUT OF SCOPE: actual ``cache_control`` breakpoint wiring / enabling prompt
caching. This layer only produces the deterministic/monotonic projection that
makes such caching possible.
"""

from __future__ import annotations

from typing import Any

from opencollab.application.shaping.pipeline import is_pinned
from opencollab.application.shaping.reactive import DEFAULT_COMPACTABLE_TOOLS

# K most-recent compactable tool results kept verbatim; everything older is
# stubbed. Set ABOVE the reactive keep-recent (``DEFAULT_TOOL_CLEAR_KEEP_RECENT``)
# on purpose: this rung runs at LOW context, where the cost of dropping a still-
# needed read is a re-read loop (a multi-file task whose working set exceeds K
# can never hold all its files at once and thrashes), so it retains more. The
# reactive layer stays tighter for genuine 120k pressure. The informative stubs
# below (which name the exact slice already read) are what make a cleared result
# safe — the model can see it already read X without re-fetching it.
DEFAULT_EAGER_KEEP_RECENT = 12

# Marker prefix on every eager stub. A stub announces itself (no invisible
# compression) and names the source tool + target so a reader / the model can
# re-fetch deterministically. The full text lives in the transcript untouched.
EAGER_STUB_PREFIX = "[Old tool result cleared"


def _stub_for_call(tool_name: str, target: str | None) -> str:
    """Deterministic, byte-stable stub naming the tool, its target and (for reads)
    the exact slice already seen.

    A pure function of ``(tool_name, target)`` — no token counts, no message
    index, no randomness — so the same old message yields an identical stub on
    every turn (the monotonic / cacheable property). The wording tells the model
    it ALREADY ran this call and the output is in the transcript, so it does not
    re-issue the call merely to re-confirm what it read — the failure mode that
    burns a whole step budget on identical re-reads.
    """
    where = f" {target}" if target else ""
    return (
        f"{EAGER_STUB_PREFIX}: {tool_name}{where}]"
        " — you already ran this; the full output is in the transcript above."
        " Re-issue ONLY if you need the exact content again, not to check whether"
        " you read it."
    )


def _as_dict(arguments: Any) -> dict[str, Any] | None:
    """A tool call's arguments as a dict, or ``None``. Deterministic; never raises."""
    if isinstance(arguments, str):
        import json

        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return None
    return arguments if isinstance(arguments, dict) else None


def _range_label(arguments: dict[str, Any]) -> str:
    """`` lines A-B`` / `` from line A`` / `` first N lines`` / `` (whole file)``,
    derived only from integer ``offset``/``limit`` so it is byte-stable."""
    offset = arguments.get("offset")
    limit = arguments.get("limit")
    off = offset if isinstance(offset, int) else None
    lim = limit if isinstance(limit, int) else None
    if off is not None and lim is not None:
        return f" lines {off}-{off + lim - 1}"
    if off is not None:
        return f" from line {off}"
    if lim is not None:
        return f" first {lim} lines"
    return " (whole file)"


def _call_target(tool_name: str | None, arguments: Any) -> str | None:
    """Best-effort target label lifted from a tool call's arguments.

    For ``file_read`` the label carries the exact line range already seen
    (``foo.py lines 1-100``) so each page of a multi-page read gets a DISTINCT
    stub — otherwise every page of one file collapses to the same stub and the
    model cannot tell which slice it already holds. Other tools keep their plain
    path/pattern/command label. Deterministic; never raises.
    """
    parsed = _as_dict(arguments)
    if parsed is None:
        return None
    if tool_name == "file_read":
        for key in ("path", "file_path", "filename", "file"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return f"{value}{_range_label(parsed)}"
    for key in ("path", "file_path", "filename", "file", "pattern", "command"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class EagerToolOutputClearShaper:
    """Always-on, age-based, deterministic tool-output clearing.

    Keeps the ``keep_recent`` most-recent compactable tool results verbatim and
    replaces every older compactable result's content with a deterministic,
    message-derived stub. Pinned sources and non-compactable messages are never
    touched; the ``tool_call`` <-> result pairing skeleton is preserved. Runs on
    every call with no estimate / trigger dependency.
    """

    def __init__(
        self,
        *,
        compactable_tools: frozenset[str] = DEFAULT_COMPACTABLE_TOOLS,
        keep_recent: int = DEFAULT_EAGER_KEEP_RECENT,
    ):
        self.compactable_tools = compactable_tools
        self.keep_recent = max(1, keep_recent)  # never clear the most recent result

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stubs = self._stubs_for_old_results(messages)
        if not stubs:
            return messages

        out: list[dict[str, Any]] = []
        changed = False
        for message in messages:
            stub = stubs.get(message.get("tool_call_id"))
            if (
                stub is not None
                and message.get("role") == "tool"
                and not is_pinned(message)
                and message.get("content") != stub
            ):
                out.append({**message, "content": stub})
                changed = True
            else:
                out.append(message)
        return out if changed else messages

    def _stubs_for_old_results(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, str]:
        """Map ``tool_call_id`` -> deterministic stub for every compactable result
        older than the last ``keep_recent``.

        Compactable calls are scanned in issue order from the assistant
        ``tool_calls`` (the tool *name* lives on the call, not the ``role:"tool"``
        answer), so age is positional and independent of any token estimate.
        Pinned issuing turns are skipped. The last ``keep_recent`` ids are
        excluded so they stay verbatim.
        """
        # Collect raw (id, name, arguments) for every compactable call first, then
        # parse arguments + build stubs ONLY for the older calls that get cleared —
        # the last ``keep_recent`` are kept verbatim, so parsing their JSON every
        # turn would be pure waste on this always-on rung.
        compactable: list[tuple[str, str, Any]] = []  # (tool_call_id, name, arguments)
        for message in messages:
            if message.get("role") != "assistant" or is_pinned(message):
                continue
            for call in message.get("tool_calls") or ():
                name = call.get("function", {}).get("name")
                call_id = call.get("id")
                if name in self.compactable_tools and call_id:
                    compactable.append(
                        (call_id, name, call.get("function", {}).get("arguments"))
                    )
        if len(compactable) <= self.keep_recent:
            return {}
        return {
            call_id: _stub_for_call(name, _call_target(name, arguments))
            for call_id, name, arguments in compactable[: -self.keep_recent]
        }
