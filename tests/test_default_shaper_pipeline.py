"""The default pipeline must not touch a history that is nowhere near full.

Every rung here rewrites the message list it is given. A rewrite is cheap when
the history is genuinely too large to send and expensive when it is not: the
provider matches a request against the previous one as a prefix, so a rung that
edits a message in the middle of a short history makes everything after that
point differ from what was sent last call, and the tail is re-read at full input
price on every call for the rest of the run. An append-only history matches to
the end.

``EagerToolOutputClearShaper`` used to sit at the front of this pipeline and ran
unconditionally, which is exactly that case. Removing it broke no test, because
nothing pinned what the default pipeline is or when it is allowed to act. These
tests pin the property rather than the class list, so any future always-on rung
fails here too.

The per-result budget is unconditional by design and is not the same hazard: it
bounds one result against its own size, so a result it truncates is truncated
identically on every later call and the prefix still matches.
"""

from __future__ import annotations

import copy

from opencollab.bootstrap.container import _build_default_shaper


class _Llm:
    """Just the one thing the pipeline asks a resolved LLM for."""

    def context_window(self) -> int:
        return 200_000


class _Summarizer:
    """Held by the auto-compact rung; never reached at low context."""

    async def summarize(self, messages):  # pragma: no cover - must not run
        raise AssertionError("auto-compact fired on a small history")


# Each result is large enough that clearing it would actually shrink the
# history -- a clearing rung declines to replace content with a stub longer
# than the content, so a history of tiny results cannot show the difference --
# and small enough to stay under the per-result budget (16,000 characters) and,
# thirty deep, far under the reactive trigger for a 200k window.
_RESULT_BODY = "".join(f"    value_{n} = compute(n) + {n}\n" for n in range(60))


def _small_history(exchanges: int) -> list[dict]:
    """A history of ``exchanges`` tool exchanges, all well under every bound."""
    messages: list[dict] = [{"role": "system", "content": "you are an agent"}]
    for index in range(exchanges):
        call_id = f"call-{index}"
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": f'{{"path": "pkg/mod_{index}.py"}}',
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "file_read",
                "content": f"# pkg/mod_{index}.py\n{_RESULT_BODY}",
            }
        )
    return messages


def _default_shaper():
    return _build_default_shaper(_Llm(), _Summarizer())


def test_the_default_pipeline_leaves_a_small_history_byte_identical():
    """Thirty exchanges is deep enough to age past any keep-recent window."""
    messages = _small_history(30)
    original = copy.deepcopy(messages)

    shaped = _default_shaper().shape(messages)

    assert shaped == original
    # The input list itself is never mutated either.
    assert messages == original


def test_no_rung_reports_firing_on_a_small_history():
    """The per-rung account has to agree: nothing acted, and it says so."""
    _shaped, reports = _default_shaper().shape_with_report(_small_history(30))

    assert [report["rung"] for report in reports] == ["none"]


def test_growing_the_history_by_one_exchange_only_appends():
    """The property the provider's prefix cache is read against.

    Call ``n`` and call ``n + 1`` must agree on every message they share. A rung
    that stubs the result that has just aged out breaks this at that message's
    depth, and everything after it is paid for again.
    """
    shaper = _default_shaper()

    before = shaper.shape(_small_history(30))
    after = shaper.shape(_small_history(31))

    assert after[: len(before)] == before
