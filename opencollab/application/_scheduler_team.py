"""Scheduler team views, topology checks, prebuilt roster, diffs, and events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from opencollab.application._scheduler_constants import (
    SPAWN_REFUSAL_TASK_CHARS,
    WORKTREE_DIFF_KEEP_CHARS,
    WORKTREE_DIFF_MAX_CHARS,
)
from opencollab.application.ports import DiffCapablePort, EnvironmentPort
from opencollab.application.scheduler_types import TeamPrebuiltError
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.identity import role_collision_key
from opencollab.domain.scheduler import SessionControlBlock

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

    # ---- static topology: the team the config declares, built up front -------

    async def ensure_team_prebuilt(self) -> tuple[int, ...]:
        """Build every declared role as a live agent, once, before the first turn.

        Off unless the scheduler was constructed with ``prebuild_team``; then it
        is a no-op that returns ``()``, and nothing else in this block fires.

        With it on, the roster stops being an outcome of the run and becomes an
        input to it. Ordinarily the only agent that exists at startup is agent 0,
        and every teammate is created mid-run by a model calling ``spawn`` — so
        how many agents a run had, and which roles, is decided by the model and
        knowable only afterwards. Here the scheduler creates one agent per role
        in the team config before any model call, which is what makes an
        *assigned* topology exist at all (see ``_trace_assigned_topology``).

        Agent 0 is untouched: it was already created by ``create_init_process``
        with the entry role and the real workspace, and is skipped here. Each
        remaining role is built through the same path a spawn uses — a worktree
        from the pool, a session from the factory — so a prebuilt teammate and a
        spawned one differ in *why* they exist, not in what they are.

        All or nothing. A declared role that cannot be built rolls the partial
        team back and raises: a run that quietly seated four of five roles would
        record an assigned topology that its own agents contradict, which is
        worse than failing at startup before a single token is spent. Team size
        is no longer one of the ways that can happen — seating an agent reserves
        nothing from the token pool, so a team of any declared size seats, and
        each seat is bounded afterwards by ``per_agent_cap`` instead.

        Returns the aids created, in declaration order.
        """
        if not self._prebuild_team or self._team_prebuilt:
            return ()
        if self._prebuild_lock is None:
            self._prebuild_lock = asyncio.Lock()
        async with self._prebuild_lock:
            if self._team_prebuilt:
                return ()
            lead = self.table.get(0)
            if lead is None:
                raise RuntimeError(
                    "Cannot prebuild the team: agent 0 does not exist yet. "
                    "Call create_init_process() first."
                )
            entry_key = role_collision_key(lead.agent.name)
            built: list[tuple[int, Any]] = []
            try:
                for role in self._roles:
                    if role_collision_key(role) == entry_key:
                        continue
                    built.append(await self._prebuild_peer(role))
            except BaseException:
                for aid, env in reversed(built):
                    await self._rollback_failed_spawn(aid, env)
                raise
            self._team_prebuilt = True
            self._trace_assigned_topology()
            self._write_manifest()
            return tuple(aid for aid, _ in built)

    async def _prebuild_peer(self, role: str) -> tuple[int, Any]:
        """Create one declared teammate. Returns ``(aid, env)`` for rollback.

        ``parent_aid`` is 0, not ``None``. Nothing created this agent in the
        sense ``spawn`` means — a prebuilt peer has no parent that is blocked on
        its result, and it is deliberately left out of ``_spawn_origin``, so the
        join path (``_deliver_to_parent``) never routes anything for it and its
        only channel to the rest of the team is ``send_message``. But
        ``parent_aid=None`` is not free either: it is the scheduler's marker for
        "this is agent 0, the root", and the hook bridge reads exactly that to
        decide whether a completion is the whole team stopping (``Stop``) or one
        teammate finishing (``SubagentStop``). Handing a peer ``None`` would
        make each of them fire ``Stop``. 0 keeps the process tree honest — every
        agent in the run descends from the init process — while leaving the join
        semantics, which live in ``_spawn_origin``, untouched.

        No task is seeded: a prebuilt teammate exists before anyone has decided
        what it should do, and gets its work as a teammate message.
        """
        aid = self.table.allocate_aid()
        env: Any | None = None
        try:
            budget = self._reserve_child_budget(aid)
            if budget <= 0:
                raise RuntimeError(
                    f"Cannot prebuild role '{role}': a team token budget of "
                    f"{self._max_budget_tokens} leaves nothing for it."
                )
            env = await self._worktree_pool.acquire(role)
            session = self._session_factory.build_spawn_session(
                role=role,
                env=env,
                budget=budget,
                aid=aid,
                scheduler=self,
                task=None,
                context="",
            )
            session.agent.name = role
            self.table.add(
                SessionControlBlock(
                    aid=aid,
                    parent_aid=0,
                    agent=session.agent,
                    state=session.state,
                )
            )
            self._sessions[aid] = session
            await self.emit_scheduler_event(
                self._events.agent_spawned(aid, 0, role, "")
            )
        except BaseException:
            await self._rollback_failed_spawn(aid, env)
            raise
        return aid, env

    def _assigned_topology_nodes(self) -> list[dict[str, Any]]:
        """One record per seated agent: who it is and what it may do."""
        entry_aid = 0
        nodes: list[dict[str, Any]] = []
        for aid in sorted(self.table.entries):
            scb = self.table.entries[aid]
            session = self._sessions.get(aid)
            agent = getattr(session, "agent", None) or scb.agent
            tools = sorted(
                str(name)
                for tool in getattr(agent, "tools", ())
                if (name := getattr(tool, "name", None)) is not None
            )
            env = getattr(session, "env", None)
            nodes.append(
                {
                    "aid": aid,
                    "role": scb.agent.name,
                    "entry": aid == entry_aid,
                    "tools": tools,
                    # What gates this agent's risky actions. "confirm" means a
                    # permission policy is wired and something outside the agent
                    # answers yes/no; "auto" means nothing does.
                    "permission_mode": (
                        "confirm"
                        if getattr(session, "permission_policy", None) is not None
                        else "auto"
                    ),
                    "workspace": getattr(env, "workspace", None),
                    # True only for a real worktree. Under ``use_worktrees=False``
                    # every agent shares one directory, and this says so.
                    "workspace_isolated": isinstance(env, DiffCapablePort),
                }
            )
        return nodes

    def _assigned_topology_edges(self) -> list[dict[str, str]]:
        """Every edge the team config declares, as ``(from_role, to_role)`` pairs.

        Read from the same ``Topology`` object ``_topology_forbids`` consults, so
        the recorded edge set is the one actually enforced. Roles declared with
        no outgoing edge contribute no pair — they appear in the node record and
        in ``roles`` instead.
        """
        topology = self._topology
        if topology is None:
            return []
        return sorted(
            (
                {"from_role": source, "to_role": destination}
                for source, destinations in topology.edges.items()
                for destination in destinations
            ),
            key=lambda edge: (edge["from_role"], edge["to_role"]),
        )

    def _trace_assigned_topology(self) -> None:
        """Record the organization the run was *assigned*, before it runs.

        Two records, both written once at prebuild: ``assigned.topology_nodes``
        (who is seated, with which tools, under which permission mode, in which
        workspace) and ``assigned.topology_edges`` (which role may address which,
        verbatim from the team config). They are the design-time half of the
        comparison the observed records support — ``worktree_changes`` says who
        actually touched what, ``spawn_refused`` says who tried to change the
        roster — and without a prebuilt team there is no assigned value to
        compare against, which is why nothing is written when the switch is off.

        ``allow_all`` travels with the edges because an open topology declares no
        edges at all: an empty list alone would read as "nobody may talk".

        Observational end to end — a recorder that fails must not take the run
        with it.
        """
        tracer = self._tracer
        if tracer is None:
            return
        lead = self.table.get(0)
        topology = self._topology
        try:
            tracer.log_step(
                step_type="assigned.topology_nodes",
                payload={
                    "entry_role": lead.agent.name if lead is not None else None,
                    "declared_roles": list(self._roles),
                    "nodes": self._assigned_topology_nodes(),
                },
            )
            tracer.log_step(
                step_type="assigned.topology_edges",
                payload={
                    "allow_all": bool(topology is not None and topology.allow_all),
                    "declared_roles": list(self._roles),
                    "edges": self._assigned_topology_edges(),
                },
            )
        except Exception as exc:
            logger.error("assigned topology trace failed: %s", exc)

    def _live_roster(self) -> list[dict[str, Any]]:
        return [
            {"aid": aid, "role": self.table.entries[aid].agent.name}
            for aid in sorted(self.table.entries)
        ]

    def _refuse_spawn_when_prebuilt(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str,
    ) -> None:
        """In static-topology mode, refuse to create an agent — and record it.

        The ``spawn_agent`` tool is deliberately left in the tool set: removing
        it would remove the observation. A model that wants a role the team does
        not have is evidence about delegation, and it can only be collected if
        the model is still able to ask. So the ask is answered with a refusal
        that names the live roster, and the attempt is written down.

        No-op unless ``prebuild_team`` is on.
        """
        if not self._prebuild_team:
            return
        roster = self._live_roster()
        declared = {role_collision_key(name) for name in self._roles}
        requested_key = role_collision_key(role)
        seated = next(
            (
                entry
                for entry in roster
                if role_collision_key(entry["role"]) == requested_key
            ),
            None,
        )
        self._trace_spawn_refused(
            parent_aid=parent_aid,
            role=role,
            task=task,
            context=context,
            roster=roster,
            declared=requested_key in declared,
        )
        listing = ", ".join(f"{entry['role']} (aid {entry['aid']})" for entry in roster)
        head = (
            f"Not spawned: '{role}' is already on this team as aid "
            f"{seated['aid']}."
            if seated is not None
            else f"Not spawned: '{role}' is not a role on this team."
        )
        raise TeamPrebuiltError(
            f"{head} This team's roster is fixed before the run starts and no "
            f"agent can be added to it. Your teammates are already running: "
            f"{listing}. Send one of them this work with send_message instead — "
            f"nothing about your request failed, only the way you asked for it.",
            roster=tuple((entry["aid"], entry["role"]) for entry in roster),
        )

    def _trace_spawn_refused(
        self,
        *,
        parent_aid: int,
        role: str,
        task: str,
        context: str,
        roster: list[dict[str, Any]],
        declared: bool,
    ) -> None:
        """Record one refused agent-creation attempt as countable evidence.

        Every field is here to be counted, not read. ``requested_role_declared``
        is the one that carries the finding: false means the model asked for a
        role the team was never given, which is a direct observation on
        participation (who the model thinks should be in the run) and on
        delegation (what it tried to hand off). ``topology_allowed`` records
        whether the declared graph would have permitted that edge, so a refusal
        caused by the fixed roster is never confused with one a closed topology
        would have produced anyway. The task text is kept, capped, alongside its
        true length, so a long delegation is still measurable as long.

        Observational: a failed record must not overturn the refusal it
        describes, so this never raises.
        """
        tracer = self._tracer
        if tracer is None:
            return
        try:
            tracer.log_step(
                step_type="spawn_refused",
                payload={
                    "reason": "team_prebuilt",
                    "requester_aid": parent_aid,
                    "requester_role": self._role_of(parent_aid),
                    "requested_role": role,
                    "requested_role_declared": declared,
                    "topology_allowed": not self._topology_forbids(
                        self._role_of(parent_aid), role
                    ),
                    "declared_roles": list(self._roles),
                    "live_roster": roster,
                    "task": str(task)[:SPAWN_REFUSAL_TASK_CHARS],
                    "task_chars": len(task or ""),
                    "context_chars": len(context or ""),
                },
            )
        except Exception as exc:
            logger.error("spawn_refused trace failed for aid %s: %s", parent_aid, exc)

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
        with a working-tree hash taken any other way. ``diff_base`` names the
        revision the diff was measured against, which is the worktree's creation
        base until the agent adopts a teammate's commit and the environment
        moves the base onto it.

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
                    # The revision the diff was measured against. Normally the
                    # worktree's creation base; after a handoff (the agent
                    # checked out a teammate's commit) the commit it adopted, so
                    # the row states which starting point "changed" is relative
                    # to instead of leaving it to be assumed.
                    "diff_base": getattr(env, "diff_base", None),
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
