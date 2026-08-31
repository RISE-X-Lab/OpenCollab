"""Inter-agent (teammate) messaging for the Scheduler.

A teammate message is queued for async delivery and surfaces to the recipient
as a normal user turn wrapped in an XML envelope. If the recipient is idle it is
scheduled in the background; if it is running or awaiting delegated work the
message stays in an out-of-history inbox until the session can safely accept
another user turn.

Every decision ``send_message`` takes is recorded: ``message_refused`` for each
rule that stops a message, ``message_sent`` for one that is queued. Together
they are the only place a run says how much traffic each declared topology edge
actually carried, and how much was attempted and stopped.

A message is checked twice, and both gates write the same row. The rules run
once at send time against the roster the sender sees, and again when the inbox
drains against whatever roster is live then — a route legal when it was queued
can be forbidden by the topology a reload rebuilt. The second gate wrote only a
scheduler event, which lands in ``events.jsonl`` and therefore only in a run
that opted into one, so by default the message was dropped and the trajectory
recorded nothing. Both now write ``message_refused``; ``restored`` says which
gate, so the rows add up on the edge while still separating "the model reached
for an edge it does not have" from "the reload could no longer route this".

``MessagingMixin`` is composed into ``Scheduler`` and relies on the
``_sessions`` / ``_tasks`` / ``_locks`` / ``_message_inbox`` maps and the
``_role_of`` / ``_autosave_session`` / ``emit_scheduler_event`` / ``_drive_agent``
helpers defined on ``Scheduler``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from opencollab.application._scheduler_constants import (
    MAX_TEAMMATE_DELIVERY_BYTES,
    MAX_TEAMMATE_INBOX_BYTES,
    MAX_TEAMMATE_INBOX_MESSAGES,
    MAX_TEAMMATE_MESSAGE_BYTES,
    MESSAGE_TRACE_COMMIT_REFS,
    MESSAGE_TRACE_SUMMARY_CHARS,
)
from opencollab.application.scheduler_types import QueuedTeammateMessage
from opencollab.domain.identity import role_collision_key
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)

# A whole lowercase-hex word of git object-id length. Git accepts an
# abbreviation from seven characters up, and a full object id is forty, so this
# is every shape a sha can be written in and nothing longer — a sha256 content
# digest is sixty-four and does not match.
_COMMIT_REF_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")


def _commit_refs(*parts: str) -> list[str]:
    """Every distinct git-object-shaped token in a message, in order.

    Candidates, not confirmed commits: any lowercase-hex word of the right
    length matches, and the record says so by carrying them next to nothing
    that claims otherwise. They earn their meaning by being joined against
    ``worktree_changes.commits``, which lists the shas an agent actually made —
    a token that matches one of those was a commit, and one that matches none
    was a hex word.

    This exists because the sha *is* the payload of a git handoff: the teams
    that hand work over do it by committing and sending the id, so a record of
    the message that keeps its size and drops its object ids cannot answer
    whether the work crossed that edge. Both worktrees of a repository can see
    each other's commits (``git worktree list`` names them), so a tester holding
    its teammate's sha is not by itself evidence that the teammate sent it.
    """
    seen: dict[str, None] = {}
    for part in parts:
        for match in _COMMIT_REF_RE.finditer(part or ""):
            seen.setdefault(match.group(0), None)
    return list(seen)


class MessagingMixin:
    """Queue, format, and drain teammate messages between agents."""

    async def send_message(self, from_aid: int, to_aid: int, summary: str, content: str) -> str:
        """Queue a teammate message for async delivery and return immediately.

        The recipient sees the message as a normal user turn with an XML
        envelope. If the recipient is idle, it is scheduled in the background;
        if it is running or awaiting delegated work, the message stays in an
        out-of-history inbox until the session can safely accept another user
        turn.

        Every outcome is recorded. Each rule that stops a message writes one
        ``message_refused`` row naming the rule, and a queued message writes one
        ``message_sent`` row; both carry the same fields, so a run's traffic on
        one topology edge and its refusals on that edge are counted the same
        way. Recording never changes the decision: the ``refuse`` helper writes
        the row and then returns the very error string the branch returned
        before, and a tracer that fails is swallowed.
        """

        def refuse(reason: str, error: str, **observed: Any) -> str:
            """Record one refusal, then hand the model the string it always got.

            Every early return in this method goes through here, so a rule
            cannot be enforced without leaving a countable record that it was.
            """
            self._trace_message_decision(
                "message_refused",
                reason=reason,
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                **observed,
            )
            return error

        if to_aid == from_aid:
            return refuse("self_message", "Error: an agent cannot message itself.")
        if self._shutting_down:
            return refuse(
                "scheduler_shutting_down", "Error: scheduler is shutting down."
            )
        if self._sessions.get(from_aid) is None or self.table.get(from_aid) is None:
            return refuse(
                "unknown_sender", f"Error: no sending agent with aid {from_aid}."
            )
        target = self._sessions.get(to_aid)
        if target is None:
            return refuse("unknown_recipient", f"Error: no agent with aid {to_aid}.")
        if self._topology_forbids(self._role_of(from_aid), self._role_of(to_aid)):
            return refuse(
                "topology_forbidden",
                f"Error: role '{self._role_of(from_aid)}' is not permitted to "
                f"message '{self._role_of(to_aid)}' under the team topology.",
            )

        lock = self._locks.setdefault(to_aid, asyncio.Lock())
        delivered_events = []
        async with lock:
            if self._shutting_down:
                return refuse(
                    "scheduler_shutting_down", "Error: scheduler is shutting down."
                )
            message_id = uuid.uuid4().hex
            from_role = self._role_of(from_aid)
            to_role = self._role_of(to_aid)
            xml = self._format_teammate_message(
                from_aid,
                summary,
                content,
                message_id=message_id,
            )
            message_bytes = self._encoded_size(xml)
            observed: dict[str, Any] = {
                "message_bytes": message_bytes,
                "message_id": message_id,
            }
            if message_bytes > MAX_TEAMMATE_MESSAGE_BYTES:
                return refuse(
                    "message_too_large",
                    "Error: teammate message exceeds the "
                    f"{MAX_TEAMMATE_MESSAGE_BYTES}-byte limit.",
                    **observed,
                )
            inbox = self._message_inbox.get(to_aid, [])
            # Read once, before the append, so an accepted send and a refused
            # one describe the same thing: the queue the decision was taken
            # against.
            observed["inbox_messages"] = len(inbox)
            observed["inbox_bytes"] = self._inbox_size(inbox)
            if observed["inbox_messages"] >= MAX_TEAMMATE_INBOX_MESSAGES:
                return refuse(
                    "inbox_message_limit",
                    f"Error: teammate inbox for aid {to_aid} is full (backpressure).",
                    **observed,
                )
            if observed["inbox_bytes"] + message_bytes > MAX_TEAMMATE_INBOX_BYTES:
                return refuse(
                    "inbox_byte_limit",
                    f"Error: teammate inbox for aid {to_aid} exceeds the "
                    f"{MAX_TEAMMATE_INBOX_BYTES}-byte limit (backpressure).",
                    **observed,
                )
            target.state.queue_pending_user_message(
                {
                    "role": "user",
                    "content": xml,
                    "message_content": content,
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "from_role": from_role,
                    "to_role": to_role,
                    "summary": summary,
                    "message_id": message_id,
                    "delivery_status": "pending",
                }
            )
            sent_at = str(target.state.pending_user_messages[-1]["timestamp"])
            message = QueuedTeammateMessage(
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                xml=xml,
                sent_at=sent_at,
                message_id=message_id,
                from_role=from_role,
                to_role=to_role,
            )
            inbox.append(message)
            self._message_inbox[to_aid] = inbox
            self._trace_message_decision(
                "message_sent",
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                **observed,
            )
            self._autosave_session(to_aid)
            delivered_events = await self._drain_message_inbox_locked(to_aid)
        # The graded tree at the moment this handoff was made. Taken after the
        # queue mutation is committed and outside the recipient's lock, so a
        # probe that hangs or is cancelled cannot leave a half-queued message;
        # and taken here rather than at the recipient's first turn because what
        # the question asks is what the *sender* had already done before it
        # asked anyone. A refused message reaches none of this: nothing crossed.
        await self.snapshot_delivery_tree(
            "message_sent", aid=from_aid, to_aid=to_aid
        )
        # Scheduler events are observational and may re-enter send_message. Emit
        # only after releasing the per-target lock, and isolate sink failures so
        # durable queue mutation and the drive task cannot be rolled back halfway.
        await self._safe_emit_scheduler_event(
            self._events.agent_message_sent(
                from_aid, to_aid, self._role_of(to_aid), summary
            )
        )
        for event in delivered_events:
            await self._safe_emit_scheduler_event(event)
        return f"Message queued to aid {to_aid}."

    def _traced_role(self, aid: int) -> str | None:
        """The agent's role for a trace record, or ``None`` when no agent exists.

        ``_role_of`` answers ``"?"`` for an aid with no agent, which is the
        right thing to show a model and the wrong thing to store: a recipient
        that does not exist has no role, and ``"?"`` would be counted as one.
        The aid stays as given — it is what the sender addressed.
        """
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else None

    def _trace_message_decision(
        self,
        step_type: str,
        *,
        from_aid: int,
        to_aid: int,
        summary: str,
        content: str,
        reason: str | None = None,
        message_bytes: int | None = None,
        message_id: str | None = None,
        inbox_messages: int | None = None,
        inbox_bytes: int | None = None,
        restored: bool = False,
    ) -> None:
        """Record one ``send_message`` outcome. Observation only.

        The six rules in ``send_message`` used to leave nothing behind: a model
        that tried to contact a role the topology forbids, and a collaboration
        message dropped by inbox backpressure, both ended as a sentence handed
        back to the model and nothing on disk. The first is the direct
        observation the role-boundary axis is decided on; the second is read
        downstream as "this agent did not communicate", which is the opposite of
        what happened.

        ``reason`` is the rule that said no, as a short enumerated token, so
        refusals are counted by cause rather than parsed out of prose. It is
        absent from a ``message_sent`` row, which by construction has no cause.

        ``from_aid`` / ``from_role`` and ``to_aid`` / ``to_role`` are four
        separate fields: the aid is what the sender addressed and is always
        recorded as given, while the role is the roster's answer for that aid
        and is ``None`` when there is no agent behind it. Two aids and two roles
        are what let a row be attributed to a declared topology edge.

        ``commit_refs`` is the one thing taken from the message body itself:
        the git-object-shaped tokens in it, listed rather than summarized. The
        body is not stored — it is already in the recipient's transcript, and a
        second prose copy in a file meant for counting would only be a copy that
        can disagree. What could not be recovered from anywhere countable is the
        payload of a git handoff, which is a commit id and nothing else, so that
        is what the row keeps. ``commit_refs_found`` is the untruncated count.

        Sizes are recorded twice over, because the two are limited separately:
        ``content_bytes`` is the payload the model wrote, and ``message_bytes``
        is the XML envelope actually measured against
        ``MAX_TEAMMATE_MESSAGE_BYTES``. The envelope is ``None`` on the branches
        that refuse before one is built — an honest gap, not a zero. Each limit
        is recorded beside the reading it bounds, so a row is readable without
        the constants file that produced it.

        ``restored`` separates the two moments a message can be refused. At send
        time the rules run against the roster the sender is looking at; on the
        restore path they run again, against whatever roster came back, and a
        route that was legal when it was queued can be forbidden by the topology
        now. Both are the same observation on the same axis and are counted in
        the same units, so they share a row shape — but a run that lost a
        message to a reload is not a run whose model reached for a forbidden
        edge, and one boolean is what keeps the two apart.

        Guarded end to end: a record that cannot be built must never overturn
        the decision it describes.
        """
        tracer = self._tracer
        if tracer is None:
            return
        try:
            inbox = self._message_inbox.get(to_aid, [])
            refs = _commit_refs(summary, content)
            payload: dict[str, Any] = {"reason": reason} if reason is not None else {}
            payload.update(
                {
                    "from_aid": from_aid,
                    "from_role": self._traced_role(from_aid),
                    "to_aid": to_aid,
                    "to_role": self._traced_role(to_aid),
                    "summary": str(summary)[:MESSAGE_TRACE_SUMMARY_CHARS],
                    "summary_chars": len(summary or ""),
                    "content_chars": len(content or ""),
                    "content_bytes": self._encoded_size(content or ""),
                    "commit_refs": refs[:MESSAGE_TRACE_COMMIT_REFS],
                    "commit_refs_found": len(refs),
                    "message_bytes": message_bytes,
                    "message_id": message_id,
                    "inbox_messages": (
                        len(inbox) if inbox_messages is None else inbox_messages
                    ),
                    "inbox_bytes": (
                        self._inbox_size(inbox) if inbox_bytes is None else inbox_bytes
                    ),
                    "max_message_bytes": MAX_TEAMMATE_MESSAGE_BYTES,
                    "max_inbox_messages": MAX_TEAMMATE_INBOX_MESSAGES,
                    "max_inbox_bytes": MAX_TEAMMATE_INBOX_BYTES,
                    "restored": restored,
                }
            )
            tracer.log_step(step_type=step_type, payload=payload)
        except Exception as exc:  # noqa: BLE001 — observability is non-authoritative
            logger.error(
                "%s trace failed for aid %s to aid %s: %s",
                step_type,
                from_aid,
                to_aid,
                exc,
            )

    async def _append_user_turn_txn(
        self, aid: int, session: Any, message: str, prior_lease: tuple[int, int] | None
    ) -> None:
        """Append ``message`` as a user turn under a rollback transaction.

        The single transaction shared by the external user-turn loop
        (``_run_turn_exclusive``) and the message-inbox drain
        (``_drain_message_inbox_locked``). The caller has already reserved the turn lease and captured
        ``prior_lease``; this owns only the atomic part: mark the delivery task,
        snapshot the turn, try the append, and on ANY failure roll the turn back
        byte-identical (``restore_user_turn``) and release-then-restore the lease
        before re-raising, always popping the delivery-task marker in
        ``finally``. Each caller keeps its own preamble (reserve) and postscript
        (drive-task / shutting-down handling).
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            self._message_delivery_tasks[aid] = current_task
        checkpoint = session.state.checkpoint_user_turn()
        try:
            await session.add_user_message(message)
        except BaseException:
            session.state.restore_user_turn(checkpoint)
            self._autosave_session(aid)
            self._release_turn_lease(aid)
            if not self._shutting_down:
                self._restore_turn_lease(aid, prior_lease)
            raise
        finally:
            if self._message_delivery_tasks.get(aid) is current_task:
                self._message_delivery_tasks.pop(aid, None)

    def _restore_message_inbox(self, aid: int, state: object) -> None:
        """Rebuild scheduler-owned delivery records from a durable sidecar."""
        pending = getattr(state, "pending_user_messages", None)
        if not isinstance(pending, list) or not pending:
            return
        committed_ids = self._message_ids_in_history(state)
        restored: list[QueuedTeammateMessage] = []
        for item in list(pending):
            if not isinstance(item, dict) or not item.get("content"):
                continue
            if item.get("delivery_status") == "rejected":
                continue
            xml = str(item["content"])
            message_id = str(item.get("message_id") or self._message_id_from_xml(xml) or "")
            if message_id and message_id in committed_ids:
                state.discard_pending_user_message_id(message_id)
                continue
            if message_id and not item.get("message_id"):
                item["message_id"] = message_id
            restored.append(
                QueuedTeammateMessage(
                    from_aid=self._restored_aid(item.get("from_aid"), default=-1),
                    to_aid=self._restored_aid(item.get("to_aid"), default=aid),
                    summary=str(item.get("summary") or "restored teammate message"),
                    content=str(
                        item.get("message_content")
                        or self._message_content_from_xml(xml)
                    ),
                    xml=xml,
                    sent_at=str(item.get("timestamp") or ""),
                    message_id=message_id,
                    from_role=str(item.get("from_role") or ""),
                    to_role=str(item.get("to_role") or ""),
                    restored=True,
                )
            )
        if restored:
            self._message_inbox[aid] = restored

    @staticmethod
    def _restored_aid(value: object, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _message_content_from_xml(xml: str) -> str:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return xml
        content = "".join(root.itertext())
        if content.startswith("\n") and content.endswith("\n"):
            return content[1:-1]
        return content

    @staticmethod
    def _message_id_from_xml(xml: str) -> str | None:
        try:
            return ET.fromstring(xml).attrib.get("message_id")
        except ET.ParseError:
            return None

    @staticmethod
    def _message_ids_in_history(state: object) -> set[str]:
        message_ids: set[str] = set()
        for message in getattr(state, "messages", ()):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                root = ET.fromstring(content)
            except ET.ParseError:
                continue
            for element in root.iter():
                message_id = element.attrib.get("message_id")
                if message_id:
                    message_ids.add(message_id)
        return message_ids

    @staticmethod
    def _format_teammate_message(
        from_aid: int,
        summary: str,
        content: str,
        *,
        message_id: str = "",
    ) -> str:
        sender = f"A{from_aid}"
        message_id_attr = f" message_id={quoteattr(message_id)}" if message_id else ""
        return (
            f"<teammate-message teammate_id={quoteattr(sender)} "
            f"summary={quoteattr(summary)}{message_id_attr}>\n"
            f"{escape(content)}\n"
            "</teammate-message>"
        )

    @staticmethod
    def _format_teammate_message_batch(messages: list[QueuedTeammateMessage]) -> str:
        envelopes = []
        for message in messages:
            sender = f"A{message.from_aid}"
            envelopes.append(
                f"<teammate-message teammate_id={quoteattr(sender)} "
                f"summary={quoteattr(message.summary)} "
                f"sent_at={quoteattr(message.sent_at)}"
                f"{f' message_id={quoteattr(message.message_id)}' if message.message_id else ''}>\n"
                f"{escape(message.content)}\n"
                "</teammate-message>"
            )
        return (
            f'<teammate-messages count="{len(messages)}">\n'
            + "\n".join(envelopes)
            + "\n</teammate-messages>"
        )

    @staticmethod
    def _encoded_size(value: str) -> int:
        return len(value.encode("utf-8"))

    @classmethod
    def _inbox_size(cls, inbox: list[QueuedTeammateMessage]) -> int:
        return sum(cls._encoded_size(message.xml) for message in inbox)

    @classmethod
    def _bounded_message_batch(
        cls, inbox: list[QueuedTeammateMessage]
    ) -> list[QueuedTeammateMessage]:
        """Select the largest FIFO prefix whose rendered prompt is bounded."""
        batch: list[QueuedTeammateMessage] = []
        for message in inbox:
            candidate = [*batch, message]
            delivery = (
                candidate[0].xml
                if len(candidate) == 1
                else cls._format_teammate_message_batch(candidate)
            )
            if cls._encoded_size(delivery) > MAX_TEAMMATE_DELIVERY_BYTES:
                break
            batch = candidate
        return batch

    async def _drain_message_inbox(self, aid: int, *, allow_current_task: bool = False) -> None:
        lock = self._locks.setdefault(aid, asyncio.Lock())
        async with lock:
            events = await self._drain_message_inbox_locked(
                aid, allow_current_task=allow_current_task
            )
        for event in events:
            await self._safe_emit_scheduler_event(event)

    async def _drain_ready_message_inboxes(self) -> None:
        """Retry durable messages when another turn has returned budget headroom."""
        ready_aids = [
            aid for aid in list(self._message_inbox) if self._message_inbox.get(aid)
        ]
        await asyncio.gather(
            *(self._drain_message_inbox(aid) for aid in ready_aids)
        )

    async def _drain_message_inbox_locked(
        self,
        aid: int,
        *,
        allow_current_task: bool = False,
    ) -> list[object]:
        inbox = self._message_inbox.get(aid)
        if not inbox:
            return []
        session = self._sessions.get(aid)
        scb = self.table.get(aid)
        if session is None or scb is None:
            return []
        events = []
        retained = []
        rejected = False
        for message in inbox:
            route_error = self._message_route_error(aid, message)
            if route_error is None:
                retained.append(message)
                continue
            reason, detail = route_error
            rejected = True
            self._mark_message_rejected(session.state, message, detail)
            # The scheduler event alone was invisible: it goes to events.jsonl,
            # which a run writes only when OPENCOLLAB_EVENTS_FILE is set, so a
            # default run dropped the message and recorded nothing a reader
            # would ever see. This row lands in the trajectory every run, in the
            # same shape as a send-time refusal, so the two can be added up.
            self._trace_message_decision(
                "message_refused",
                reason=reason,
                from_aid=message.from_aid,
                to_aid=aid,
                summary=message.summary,
                content=message.content,
                message_id=message.message_id or None,
                restored=True,
            )
            events.append(
                self._events.agent_message_rejected_on_restore(
                    message.from_aid,
                    aid,
                    detail,
                )
            )
        if rejected:
            inbox[:] = retained
            self._autosave_session(aid)
        if not inbox:
            return events
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return events
        if scb.state.phase is SessionPhase.AWAITING_EVENTS or not scb.state.pending_events.is_empty():
            return events
        messages = self._bounded_message_batch(inbox)
        if not messages or self._shutting_down:
            return events
        prior_lease = self._current_turn_lease(aid)
        if not self._reserve_message_budget(aid):
            return events

        delivery = (
            messages[0].xml
            if len(messages) == 1
            else self._format_teammate_message_batch(messages)
        )
        await self._append_user_turn_txn(aid, session, delivery, prior_lease)

        # Commit the durable dequeue immediately after the history append. A
        # shutdown may stop the follow-on driver, but it must never leave the
        # already-appended message in a sidecar that restore would re-deliver.
        del inbox[: len(messages)]
        for message in messages:
            if message.message_id:
                session.state.discard_pending_user_message_id(message.message_id)
            else:
                session.state.discard_pending_user_message(message.xml)
        self._autosave_session(aid)

        if self._shutting_down:
            self._release_turn_lease(aid)
            return events

        self._start_agent_task(aid, session)
        return events + [
            (
                self._events.agent_message_delivered(
                    message.from_aid,
                    message.to_aid,
                    message.summary,
                    len(message.content),
                )
            )
            for message in messages
        ]

    def _message_route_error(
        self,
        aid: int,
        message: QueuedTeammateMessage,
    ) -> tuple[str, str] | None:
        """Validate a durable message against the current roster and topology.

        Returns ``(reason, detail)`` or ``None``. ``reason`` is a short
        enumerated token in the same vocabulary ``send_message`` refuses in, so
        a refusal on this path can be counted rather than parsed out of prose;
        ``detail`` is the sentence a person reads, and names the two values that
        stopped being equal. The pair exists because the two audiences differ:
        the count needs a closed set, and the operator needs the specifics.
        """
        if message.to_aid != aid:
            return (
                "restored_target_changed",
                f"restored target aid changed from {message.to_aid} to {aid}",
            )
        if message.from_aid == aid:
            return (
                "restored_self_message",
                "restored route would deliver a message to its sender",
            )
        sender = self.table.get(message.from_aid)
        target = self.table.get(aid)
        if sender is None or self._sessions.get(message.from_aid) is None:
            return (
                "restored_sender_gone",
                f"restored sender aid {message.from_aid} no longer exists",
            )
        if target is None or self._sessions.get(aid) is None:
            return (
                "restored_target_gone",
                f"restored target aid {aid} no longer exists",
            )

        current_from_role = sender.agent.name
        current_to_role = target.agent.name
        if message.from_role:
            if role_collision_key(message.from_role) != role_collision_key(
                current_from_role
            ):
                return (
                    "restored_sender_role_changed",
                    f"restored sender role changed from {message.from_role!r} "
                    f"to {current_from_role!r}",
                )
        elif message.restored and self._topology is not None:
            return (
                "restored_sender_role_missing",
                "restored message has no durable sender role identity",
            )
        if message.to_role:
            if role_collision_key(message.to_role) != role_collision_key(
                current_to_role
            ):
                return (
                    "restored_target_role_changed",
                    f"restored target role changed from {message.to_role!r} "
                    f"to {current_to_role!r}",
                )
        elif message.restored and self._topology is not None:
            return (
                "restored_target_role_missing",
                "restored message has no durable target role identity",
            )
        if self._topology_forbids(current_from_role, current_to_role):
            return (
                "restored_topology_forbidden",
                f"restored route {current_from_role!r} to {current_to_role!r} "
                "is forbidden by the current team topology",
            )
        return None

    @staticmethod
    def _mark_message_rejected(
        state: object,
        message: QueuedTeammateMessage,
        reason: str,
    ) -> None:
        """Move one pending sidecar record into a durable rejected state."""
        pending = getattr(state, "pending_user_messages", ())
        for item in pending:
            if not isinstance(item, dict):
                continue
            same_message = (
                bool(message.message_id)
                and item.get("message_id") == message.message_id
            ) or (
                not message.message_id
                and item.get("content") == message.xml
                and item.get("from_aid") == message.from_aid
            )
            if not same_message:
                continue
            item["delivery_status"] = "rejected"
            item["rejection_reason"] = reason
            return
