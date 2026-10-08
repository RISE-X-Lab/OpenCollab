"""Failure notifications for unanswered teammate messages."""

from __future__ import annotations

import asyncio
import logging
import uuid
from xml.sax.saxutils import escape, quoteattr

from opencollab.application._scheduler_constants import (
    MAX_TEAMMATE_INBOX_BYTES,
    MAX_TEAMMATE_INBOX_MESSAGES,
    MAX_TEAMMATE_MESSAGE_BYTES,
)
from opencollab.application.ports import RecoverableChangesPort
from opencollab.application.scheduler_types import QueuedTeammateMessage
from opencollab.domain.identity import validate_role_identity
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)
_FAILED_PHASES = {SessionPhase.STOPPED, SessionPhase.ERROR}


def _notice_xml(aid: int, role: str, message_id: str, content: str) -> str:
    return (
        f"<team-notice about={quoteattr(f'A{aid}')} "
        f"role={quoteattr(role)} message_id={quoteattr(message_id)}>\n"
        f"{escape(content)}\n</team-notice>"
    )


class SchedulerStopNoticeMixin:
    """Use each pair's current unanswered exchange to notify waiting senders."""

    def _record_teammate_exchange(self, from_aid: int, to_aid: int, message_id: str) -> None:
        # A reverse message answers the current outstanding message. Once that
        # exchange closes, either peer can initiate the next one.
        answered = self._unanswered.get(from_aid, {}).pop(to_aid, None)
        if answered is None:
            self._unanswered.setdefault(to_aid, {})[from_aid] = message_id

    def _notice_recovery_location(self, aid: int, *, retain: bool) -> str | None:
        env = getattr(self._sessions.get(aid), "env", None)
        if isinstance(env, RecoverableChangesPort):
            return env.retain_changes() if retain else env.recovery_location
        return None

    async def notify_unanswered_senders(
        self, aid: int, reason: str, *, partial_result: str | None = None,
    ) -> None:
        stopped = self.table.get(aid)
        if stopped is None or stopped.state.phase not in _FAILED_PHASES:
            return
        reason = stopped.state.terminal_reason or reason
        waiting = self._unanswered.get(aid, {})
        if not waiting or self._shutting_down:
            return
        try:
            role = validate_role_identity(stopped.agent.name)
        except ValueError as exc:
            logger.error("stop notice role is invalid for aid %s: %s", aid, exc)
            return
        for to_aid, unanswered_id in tuple(waiting.items()):
            lock = self._locks.setdefault(to_aid, asyncio.Lock())
            async with lock:
                # Spend an exchange only after its notice is in the pending sidecar.
                # Cancellation while waiting for this lock leaves it retryable;
                # another notifier or a reply may have settled it meanwhile.
                outstanding = self._unanswered.get(aid, {})
                if outstanding.get(to_aid) != unanswered_id:
                    continue
                if stopped.state.phase not in _FAILED_PHASES:
                    return
                target = self._sessions.get(to_aid)
                scb = self.table.get(to_aid)
                if self._shutting_down:
                    return
                if target is None or scb is None or scb.state.phase in _FAILED_PHASES:
                    outstanding.pop(to_aid, None)
                    continue
                message_id = uuid.uuid4().hex
                displayed_reason = reason[:MAX_TEAMMATE_MESSAGE_BYTES // 16]
                suffix = " [truncated]" if displayed_reason != reason else ""
                content = (
                    f"{role} (A{aid}) has stopped and will not answer your last "
                    f"message. Reason recorded by the runtime: {displayed_reason}{suffix}"
                )
                extra = partial_result
                if extra is None:
                    location = self._notice_recovery_location(aid, retain=False)
                    if location:
                        extra = f"[Partial changes retained at {location}]"
                xml = _notice_xml(aid, role, message_id, content + (f"\n\n{extra}" if extra else ""))
                if self._encoded_size(xml) > MAX_TEAMMATE_MESSAGE_BYTES:
                    location = self._notice_recovery_location(aid, retain=True)
                    extra = (
                        f"[Full partial changes retained at {location}]" if location else
                        "[Partial result exceeds the teammate message limit.]"
                    )
                    limit = MAX_TEAMMATE_MESSAGE_BYTES // 16
                    if len(extra) > limit:
                        extra = extra[:limit] + " [truncated]"
                    xml = _notice_xml(aid, role, message_id, content + f"\n\n{extra}")
                if extra:
                    content += f"\n\n{extra}"
                inbox = self._message_inbox.setdefault(to_aid, [])
                if (
                    len(inbox) >= MAX_TEAMMATE_INBOX_MESSAGES
                    or self._inbox_size(inbox) + self._encoded_size(xml) > MAX_TEAMMATE_INBOX_BYTES
                ):
                    # Retry after existing messages drain, using the same
                    # outstanding exchange rather than exceeding inbox bounds.
                    if partial_result:
                        self._notice_recovery_location(aid, retain=True)
                    continue
                to_role = self._role_of(to_aid)
                target.state.queue_pending_user_message({
                    "role": "user", "content": xml, "message_content": content,
                    "from_aid": aid, "to_aid": to_aid, "from_role": role,
                    "to_role": to_role, "summary": f"{role} stopped",
                    "message_id": message_id, "delivery_status": "pending", "kind": "stop_notice",
                })
                inbox.append(QueuedTeammateMessage(
                    from_aid=aid, to_aid=to_aid, summary=f"{role} stopped",
                    content=content, xml=xml,
                    sent_at=str(target.state.pending_user_messages[-1]["timestamp"]),
                    message_id=message_id, from_role=role, to_role=to_role, kind="stop_notice",
                ))
                outstanding.pop(to_aid, None)
                self._trace_stop_notice(aid, to_aid, reason, message_id, unanswered_id)
                self._autosave_session(to_aid)
                events = await self._drain_message_inbox_locked(to_aid)
            for event in events:
                await self._safe_emit_scheduler_event(event)

    def _trace_stop_notice(
        self, aid: int, to_aid: int, reason: str, message_id: str, unanswered_id: str,
    ) -> None:
        if self._tracer is None:
            return
        try:
            self._tracer.log_step(
                step_type="stop_notice",
                payload={
                    "aid": aid, "role": self._role_of(aid), "to_aid": to_aid,
                    "to_role": self._traced_role(to_aid), "reason": reason,
                    "message_id": message_id, "unanswered_message_id": unanswered_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - observational trace
            logger.error("stop_notice trace failed for aid %s to aid %s: %s", aid, to_aid, exc)
