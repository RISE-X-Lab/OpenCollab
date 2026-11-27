"""Queue teammate messages as XML-wrapped user turns for the Scheduler.

Idle recipients are scheduled immediately. Running recipients and those
awaiting delegated work retain messages in an inbox until delivery is safe.
Send and delivery checks record ``message_sent`` or ``message_refused``.
``restored`` distinguishes restore-time refusals from send-time decisions.

``MessagingMixin`` uses Scheduler session, task, lock and inbox state, plus
its role, autosave, event and agent-driving helpers.
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

# Match whole lowercase-hex Git object ids and 7-40 character abbreviations.
# A 64-character SHA-256 content digest stays outside this pattern.
_COMMIT_REF_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")

def _commit_refs(*parts: str) -> list[str]:
    """Return distinct Git-object-shaped tokens in message order.

    Tokens are candidate references, not validated commits. Join them against
    ``worktree_changes.commits`` to identify handoffs. Shared Git object access
    alone does not prove that an agent sent a commit to its teammate.
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
            # Accepted and refused sends describe the same queue state before appending.
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
            self._record_teammate_exchange(from_aid, to_aid, message_id)
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
        await self.snapshot_delivery_tree("message_sent", aid=from_aid, to_aid=to_aid)
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
        """Record a message decision without changing delivery behavior.

        ``reason`` classifies refusals and is absent for accepted sends. Sender and
        recipient aids retain the requested identities, while roles resolve through
        the roster and may be ``None``. ``commit_refs`` contains bounded candidate
        Git references, with the full count in ``commit_refs_found``.

        Record the bounded summary and payload sizes rather than duplicating the
        message body. ``content_bytes`` measures the payload, while ``message_bytes``
        measures its XML envelope against a separate limit. The latter is ``None``
        when refusal precedes envelope construction. Each reading retains its limit.

        ``restored`` distinguishes restore-time refusal from the original send
        checks, since reloaded topology may invalidate a previously queued route.
        Trace failures are logged without overturning the recorded decision.
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
        checkpoint_turn = getattr(session, "_checkpoint_user_turn", None)
        rollback = checkpoint_turn() if callable(checkpoint_turn) else None
        checkpoint = session.state.checkpoint_user_turn() if rollback is None else None
        try:
            await session.add_user_message(message)
        except BaseException:
            if rollback is None:
                session.state.restore_user_turn(checkpoint)
            else:
                rollback()
            self._autosave_session(aid)
            self._release_turn_lease(aid)
            if not self._shutting_down:
                self._restore_turn_lease(aid, prior_lease)
            raise
        finally:
            if self._message_delivery_tasks.get(aid) is current_task:
                self._message_delivery_tasks.pop(aid, None)

    def _set_cancelled_turn_inbox_hold(self, aid: int, *, held: bool) -> None:
        """Keep the inbox hold in its accepted messages' durable metadata."""
        if held:
            self._cancelled_turn_inbox_holds.add(aid)
        else:
            self._cancelled_turn_inbox_holds.discard(aid)
        session = self._sessions.get(aid)
        if session is None:
            return
        changed = False
        status = "held_after_cancel" if held else "pending"
        for message in session.state.pending_user_messages:
            if not isinstance(message, dict) or message.get("delivery_status") == "rejected":
                continue
            if held or message.get("delivery_status") == "held_after_cancel":
                if message.get("delivery_status") != status:
                    message["delivery_status"] = status
                    changed = True
        if changed:
            self._autosave_session(aid)

    def _restore_message_inbox(self, aid: int, state: object) -> None:
        """Rebuild scheduler-owned delivery records from a durable sidecar."""
        if (
            getattr(state, "phase", None) is SessionPhase.STOPPED
            and getattr(state, "terminal_reason", None)
            in {"interrupted by user", "parent turn interrupted by user"}
        ):
            self._cancelled_turn_inbox_holds.add(aid)
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
            if item.get("delivery_status") == "held_after_cancel":
                self._cancelled_turn_inbox_holds.add(aid)
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
                    kind=str(item.get("kind") or "teammate"),
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
            if message.kind == "stop_notice":
                envelopes.append(message.xml)
                continue
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
        current = asyncio.current_task()
        for aid in tuple(self._unanswered):
            stopped = self.table.get(aid)
            if stopped is not None and stopped.state.phase in {SessionPhase.STOPPED, SessionPhase.ERROR}:
                driver = self._tasks.get(aid)
                # The driver sends the terminal reply after collecting its evidence.
                if driver is not None and driver is not current and not driver.done():
                    continue
                await self.notify_unanswered_senders(aid, stopped.state.terminal_reason or stopped.state.phase.value)

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
            waiting = self._unanswered.get(aid, {})
            if waiting.get(message.from_aid) == message.message_id:
                waiting.pop(message.from_aid, None)
            # The trajectory records refusals even when OPENCOLLAB_EVENTS_FILE is unset.
            # Its row matches send-time refusals so both can be counted together.
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
        cancel_event = self._turn_cancel_events.get(aid)
        if cancel_event is not None and cancel_event.is_set() and scb.state.phase.is_terminal():
            # A completed step still belongs to the cancelled public turn.
            # Keep accepted work for the next public run's existing hold release.
            self._cancelled_turn_inbox_holds.add(aid)
        if aid in self._cancelled_turn_inbox_holds:
            self._set_cancelled_turn_inbox_hold(aid, held=True)
            return events
        # An external turn can already own the append while its driver does
        # not exist yet. Keep accepted messages queued until that append and
        # the following turn finish, just as for an existing driver.
        delivery = self._message_delivery_tasks.get(aid)
        if delivery is not None and not delivery.done():
            return events
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return events
        if (
            scb.state.phase is SessionPhase.AWAITING_EVENTS
            or not scb.state.pending_events.is_empty()
            or scb.state.pending_external_user_turn is not None
        ):
            return events
        if getattr(getattr(session, "runner", None), "pending_cleanup_tasks", ()):
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
        if message.kind == "stop_notice":
            if sender.state.phase not in {SessionPhase.STOPPED, SessionPhase.ERROR}:
                return ("restored_notice_sender_active", "notice refers to an active teammate")
            try:
                envelope = ET.fromstring(message.xml)
            except ET.ParseError:
                return ("restored_notice_invalid", "notice envelope is malformed")
            if envelope.tag != "team-notice" or envelope.attrib.get("about") != f"A{message.from_aid}":
                return ("restored_notice_invalid", "notice envelope has a different subject")
            return None
        if message.kind != "teammate":
            return ("restored_message_kind_invalid", "message kind is unknown")
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
