"""Scheduler team views, topology checks, diffs, and event emission."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from opencollab.application._scheduler_constants import (
    WORKTREE_DIFF_KEEP_CHARS,
    WORKTREE_DIFF_MAX_CHARS,
)
from opencollab.application.ports import DiffCapablePort, EnvironmentPort
from opencollab.domain.events import SchedulerEvent

logger = logging.getLogger(__name__)

_DIFF_HEADER = "diff --git "
_BODY_MARKERS = ("@@", "GIT binary patch", "Binary files ")


def _git_unquote(token: str) -> str:
    """Undo the C-style quoting git applies to paths with unusual bytes.

    ``"b/\\344\\270\\255.txt"`` is how git writes a UTF-8 path; the escapes are
    per *byte*, so the round trip decodes the escapes to latin-1 code points,
    packs those back into bytes, and decodes the bytes as UTF-8. Anything that
    does not survive the trip is returned untouched — a mangled path is a
    weaker record than a quoted one, but neither may raise.
    """
    if len(token) < 2 or not token.startswith('"') or not token.endswith('"'):
        return token
    try:
        raw = token[1:-1].encode("utf-8").decode("unicode_escape").encode("latin-1")
        return raw.decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return token


def _side_path(token: str, prefix: str) -> str | None:
    """One side of a file header (``--- a/x`` / ``+++ b/x``), or None for /dev/null.

    Git appends a tab after the path when the path contains a space, so the
    tab is stripped before anything else looks at the token.
    """
    token = _git_unquote(token.split("\t", 1)[0].strip())
    if token == "/dev/null":
        return None
    return token[len(prefix):] if token.startswith(prefix) else token


def _header_line_paths(line: str) -> tuple[str | None, str | None]:
    """The (old, new) paths off a ``diff --git a/X b/Y`` line.

    Only needed where git omits the ``---``/``+++`` pair — binary payloads and
    mode-only changes. Unquoted paths containing " b/" are genuinely ambiguous
    on this line; the same-path split is tried first because that is the shape
    of every non-rename entry, and a rename carries explicit ``rename
    from``/``rename to`` lines anyway.
    """
    rest = line[len(_DIFF_HEADER):].strip()
    if rest.startswith('"'):
        closing = rest.find('" "')
        if closing == -1:
            return None, None
        return (
            _side_path(rest[: closing + 1], "a/"),
            _side_path(rest[closing + 2:], "b/"),
        )
    if not rest.startswith("a/"):
        return None, None
    body = rest[2:]
    half = (len(body) - 3) // 2
    if half > 0 and body[half: half + 3] == " b/" and body[:half] == body[half + 3:]:
        return body[:half], body[:half]
    marker = body.find(" b/")
    if marker == -1:
        return None, None
    return body[:marker], body[marker + 3:]


def _classify_block(block: list[str]) -> list[tuple[str, str]]:
    """Turn one file's diff header into ``(path, op)`` entries."""
    old_path = new_path = rename_from = rename_to = None
    added = deleted = False
    for line in block[1:]:
        if line.startswith(_BODY_MARKERS):
            break
        if line.startswith("new file mode"):
            added = True
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("rename from "):
            rename_from = _git_unquote(line[len("rename from "):].strip())
        elif line.startswith("rename to "):
            rename_to = _git_unquote(line[len("rename to "):].strip())
        elif line.startswith("--- "):
            old_path = _side_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new_path = _side_path(line[4:], "b/")
    if rename_from and rename_to:
        # A rename is reported as its two endpoints so ``op`` stays inside the
        # added/modified/deleted vocabulary the record documents.
        return [(rename_from, "deleted"), (rename_to, "added")]
    fallback_old, fallback_new = _header_line_paths(block[0])
    if deleted:
        path = old_path or fallback_old
        return [(path, "deleted")] if path else []
    path = new_path or fallback_new or old_path or fallback_old
    if not path:
        return []
    return [(path, "added" if added else "modified")]


def _parse_worktree_diff(diff: str) -> list[tuple[str, str]]:
    """Every file an *untruncated* worktree diff touches, in diff order.

    Splitting on ``diff --git `` at column zero is safe inside hunks: a body
    line is always prefixed by ``+``, ``-``, or a space.
    """
    blocks: list[list[str]] = []
    for line in diff.splitlines():
        if line.startswith(_DIFF_HEADER):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return [entry for block in blocks for entry in _classify_block(block)]


class SchedulerTeamMixin:
    @property
    def lead_session(self) -> Any:
        """Agent 0's session (the interactive entry)."""
        return self._lead_session

    def _role_of(self, aid: int) -> str:
        scb = self.table.get(aid)
        return scb.agent.name if scb is not None else "?"

    def _topology_forbids(self, src_role: str, dst_role: str) -> bool:
        """True when a team topology is configured and forbids ``src_role`` → ``dst_role``.

        The single deny decision shared by the two verbs — spawn
        (``_check_topology`` raises ``PermissionError``) and ``send_message``
        (returns an error-string tool result). Each caller keeps its own role
        resolution and its own response shape; only the boolean is shared, so
        ``allows`` is consulted exactly one way for both. Takes role *strings*,
        not aids: a spawn target has no aid yet.
        """
        return self._topology is not None and not self._topology.allows(src_role, dst_role)

    def _check_topology(self, src_aid: int, dst_role: str, *, verb: str) -> None:
        """Raise ``PermissionError`` if the topology forbids src → dst_role."""
        src_role = self._role_of(src_aid)
        if self._topology_forbids(src_role, dst_role):
            raise PermissionError(f"Role '{src_role}' is not permitted to {verb} '{dst_role}' under the team topology.")

    def team_snapshot(self) -> list[dict[str, Any]]:
        """Read-only roster of every tracked (live) agent, ordered by aid."""
        snapshot: list[dict[str, Any]] = []
        for aid in sorted(self.table.entries):
            scb = self.table.entries[aid]
            task = self._tasks.get(aid)
            snapshot.append(
                {
                    "aid": aid,
                    "role": scb.agent.name,
                    "parent_aid": scb.parent_aid,
                    "phase": scb.state.phase.value,
                    "busy": task is not None and not task.done(),
                }
            )
        return snapshot

    def team_roster(self) -> list[dict[str, Any]]:
        """Full configured team for the prompt toolbar: every live agent plus
        each configured role that has no live agent yet (``aid=None``, phase
        ``"available"``). Unlike ``team_snapshot`` (live agents only, used to
        message teammates by aid), this surfaces the team the user defined in
        the team config before anything has spawned.
        """
        live = self.team_snapshot()
        live_roles = {entry["role"] for entry in live}
        available = [
            {
                "aid": None,
                "role": role,
                "parent_aid": None,
                "phase": "available",
                "busy": False,
            }
            for role in self._roles
            if role not in live_roles
        ]
        return live + available

    def agent_step_count(self, aid: int) -> int:
        """Return the cumulative step count for one registered agent."""
        session = self._sessions.get(aid)
        if session is None or self.table.get(aid) is None:
            raise ValueError(f"No agent with aid {aid}.")
        return int(getattr(session, "step_count", session.state.step_count))

    async def _append_worktree_diff(self, env: EnvironmentPort, result: str) -> str:
        """If env is a worktree, append its diff to the result."""
        if not isinstance(env, DiffCapablePort):
            return result
        diff = await env.get_diff()
        if not diff:
            return result
        if len(diff) > WORKTREE_DIFF_MAX_CHARS:
            diff = (
                diff[:WORKTREE_DIFF_KEEP_CHARS]
                + f"\n\n... [{len(diff) - WORKTREE_DIFF_MAX_CHARS} chars truncated] ...\n\n"
                + diff[-WORKTREE_DIFF_KEEP_CHARS:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def _trace_worktree_changes(self, aid: int, role: str, env: EnvironmentPort) -> None:
        """Record one agent's worktree changes as a structured trace row.

        The copy of the diff that reaches the parent is prose — fenced markdown
        with its middle cut out past ``WORKTREE_DIFF_MAX_CHARS`` — so it cannot
        serve as the "which agent touched which file" evidence. This writes that
        evidence to a separate destination, built from the *untruncated* diff:
        one entry per file with the sha256 of the file's post-change content
        (``read_file``, hence ``None`` for a deletion), plus the whole diff's
        sha and length, plus whether the parent's copy was cut. An agent that
        changed nothing still gets a row, with an empty ``files`` list: absence
        of a row would otherwise read the same as a recorder that broke.

        ``content_sha`` hashes file *content*, not diff text, so it lines up
        with a working-tree hash taken any other way.

        Purely observational, unlike ``_append_worktree_diff``: a missing diff
        is a technical failure that fails the agent, but a missing record here
        must not. Every read is guarded on its own — binary content, a revoked
        environment, and a file removed between diff and read are all expected —
        and the whole thing is guarded again so nothing escapes to the caller.
        """
        tracer = self._tracer
        if tracer is None or not isinstance(env, DiffCapablePort):
            return
        try:
            diff = await env.get_diff()
            files: list[dict[str, Any]] = []
            for path, op in _parse_worktree_diff(diff):
                entry: dict[str, Any] = {"path": path, "op": op, "content_sha": None}
                if op != "deleted":
                    try:
                        content = await env.read_file(path)
                        entry["content_sha"] = hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest()
                    except Exception as exc:
                        entry["sha_error"] = type(exc).__name__
                files.append(entry)
            tracer.log_step(
                step_type="worktree_changes",
                payload={
                    "aid": aid,
                    "role": role,
                    "files": files,
                    "diff_sha": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                    "diff_chars": len(diff),
                    "truncated_in_result": len(diff) > WORKTREE_DIFF_MAX_CHARS,
                },
            )
        except Exception as exc:
            logger.error("worktree_changes trace failed for aid %s: %s", aid, exc)

    async def emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit a pre-built scheduler event via the event sink.

        Events are built through ``self._events`` (a ``SchedulerEventFactory``)
        so the orchestration vocabulary lives in one place; the sink is a
        required ``EventPublisherPort``, so emission is a single ``emit``.
        """
        await self._event_sink.emit(event)

    async def _safe_emit_scheduler_event(self, event: SchedulerEvent) -> None:
        """Emit an observational lifecycle event without changing scheduler state."""
        try:
            await self.emit_scheduler_event(event)
        except Exception as exc:
            logger.error("scheduler event %s failed: %s", event.type, exc)
