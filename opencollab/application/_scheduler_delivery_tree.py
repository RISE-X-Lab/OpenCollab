"""Delivery-tree snapshots — which seat was working when a line arrived.

What a team run is graded on is one tree: the repository agent 0 was given.
Teammates work in worktrees of their own (``WorktreePool``, rooted outside the
repository on purpose), so nothing a teammate writes is in the graded tree
until agent 0 puts it there. "Who wrote the delivered patch" therefore cannot
be answered from the per-agent ``worktree_changes`` rows alone: those say what
each seat produced in its own directory, not what reached the tree that was
graded.

This records the graded tree itself, at the two boundaries a team run has:

* ``turn_start`` — taken inside the turn gate, immediately before a session's
  ``run_loop``. Under ``serialize_turns`` exactly one seat runs at a time, so
  consecutive ``turn_start`` snapshots bracket one seat's working period: every
  line present at turn *n+1* and absent at turn *n* arrived while the seat that
  held turn *n* was the only one running. The last turn needs no closing
  snapshot — the delivered patch is that closing snapshot, extracted by the
  harness. Without ``serialize_turns`` turns overlap and the bracket is only
  approximate; the flag is recorded by ``_trace_assigned_topology`` so a reader
  can tell which reading applies.
* ``message_sent`` — taken when one seat's message to another has been queued.
  This is the handoff itself, and it is the boundary the "was the work already
  done before anyone was asked" question is asked at: a workflow that sequences
  the same three roles probes its tree between the analyze and implement
  phases, and a message is where a team decides that boundary for itself.
  A refused message writes no snapshot: nothing crossed.

The snapshot is the tree's *diff text*, not a hash of it, because the quantity
wanted is per line and a hash cannot be subtracted from another hash. Repeats
are collapsed: a snapshot whose diff is byte-identical to one already recorded
stores ``unchanged_since`` and no text, which is the common case (a teammate's
turn cannot change the graded tree at all, so most turns repeat the previous
state exactly).

Every read is guarded and a failure is written down rather than raised: this is
observational, and a run must not fail because a ``git diff`` did not come back.
A boundary whose probe failed leaves a row with ``diff: None`` and
``probe_error``, so "the recorder broke" and "the tree did not change" stay
distinguishable.

Off unless the scheduler was built with a probe, which is how every existing
caller (the TUI, the CLI, every test) keeps its exact previous behaviour: no
probe, no git call, no rows.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# How much of one boundary's diff text is kept. Line-level attribution needs the
# text, so it is stored; the cap stops one pathological tree from writing
# megabytes into every run record. ``truncated`` says when it bit.
DELIVERY_DIFF_SNAPSHOT_CHARS = 20_000


def _delivery_diff_files(text: str) -> list[str]:
    """Every path a working-tree diff touches, in diff order, without repeats."""
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2 and parts[1] not in paths:
                paths.append(parts[1])
    return paths


class SchedulerDeliveryTreeMixin:
    """Records the graded tree at seat boundaries, when a probe is wired."""

    @property
    def delivery_tree_snapshots(self) -> tuple[dict[str, Any], ...]:
        """Every boundary recorded so far, oldest first. Empty when off."""
        return tuple(self._delivery_tree_snapshots)

    async def snapshot_delivery_tree(
        self,
        at: str,
        *,
        aid: int,
        to_aid: int | None = None,
    ) -> None:
        """Record the graded tree at one boundary. No-op without a probe."""
        probe = self._delivery_tree_probe
        if probe is None:
            return
        record: dict[str, Any] = {
            "at": at,
            "aid": aid,
            "role": self._traced_role(aid),
        }
        if to_aid is not None:
            record["to_aid"] = to_aid
            record["to_role"] = self._traced_role(to_aid)
        try:
            text = await probe.diff()
        except Exception as exc:  # noqa: BLE001 — observation must never fail a run
            record["diff"] = None
            record["probe_error"] = f"{type(exc).__name__}: {exc}"
            self._delivery_tree_snapshots.append(record)
            logger.error("delivery-tree snapshot at %s failed: %s", at, exc)
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        record["chars"] = len(text)
        record["sha256"] = digest
        already = self._delivery_tree_shas.get(digest)
        if already is not None:
            # The tree is in a state already stored in full. Naming that row is
            # the whole content of this one; repeating the text would multiply
            # the same diff by the number of turns that did not change it.
            record["unchanged_since"] = already
            self._delivery_tree_snapshots.append(record)
            return
        self._delivery_tree_shas[digest] = len(self._delivery_tree_snapshots)
        record["files"] = _delivery_diff_files(text)
        record["diff"] = text[:DELIVERY_DIFF_SNAPSHOT_CHARS]
        record["truncated"] = len(text) > DELIVERY_DIFF_SNAPSHOT_CHARS
        self._delivery_tree_snapshots.append(record)


__all__ = ["DELIVERY_DIFF_SNAPSHOT_CHARS", "SchedulerDeliveryTreeMixin"]
