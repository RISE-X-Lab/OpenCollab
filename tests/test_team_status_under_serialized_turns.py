"""What ``team_status`` and ``submit`` tell an agent when turns are serialized.

Under ``serialize_turns`` exactly one agent runs at a time, and a teammate that
has been messaged cannot start until the agent now running ends its turn. Two
texts used to say otherwise:

* ``team_status`` printed ``busy`` for every aid whose driver task existed and
  had not finished. A messaged teammate's task exists at once and then waits on
  the team gate, so the sender was shown its teammate as busy while that
  teammate could not run until the sender itself stopped. 09-25, gpt-5.6-luna
  on the s2dual plain card: the Adopter read ``coder_a — busy``, waited by
  polling and sleeping inside its own turn, and in 29 of 36 runs no Coder ran
  before the Adopter's budget was gone.
* ``submit`` said only "your work on this task is finished". An agent waiting
  on a teammate is not finished, so read literally it is not the way to wait,
  although ending the turn is the only way a serialized teammate gets to run.

The tests pin the roster field that tells the two states apart, the label it
prints as, and the waiting sentence in ``submit``.
"""

from __future__ import annotations

import asyncio

from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    resume_done,
    suspend_spawning,
)

from opencollab.adapters.tools.message import TeamStatusTool
from opencollab.adapters.tools.submit import SubmitTool
from opencollab.domain.session import SessionPhase

GATE_TIMEOUT_SECONDS = 30.0


def _snapshot_inside_first_turn(*, serialize_turns: bool) -> dict[str, dict]:
    """Roster as seen from inside whichever teammate runs first, by role."""
    seen: dict[str, dict] = {}
    box: dict = {}

    def turn(role: str):
        async def step(sess: ScriptedSession) -> str:
            if not seen:
                # let the other teammate's driver get as far as it can
                for _ in range(5):
                    await asyncio.sleep(0)
                for entry in box["scheduler"].team_status_rows():
                    seen[entry["role"]] = dict(entry, me=(entry["role"] == role))
            sess.state.set_phase(SessionPhase.DONE)
            sess.state.append_message({"role": "assistant", "content": role})
            return role

        return step

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1"), ("tester", "check", "tc-2")]),
            resume_done(lambda results: "final: " + ", ".join(results)),
        ],
    )

    async def scenario() -> str:
        children = [ScriptedSession("coder", [turn("coder")]),
                    ScriptedSession("tester", [turn("tester")])]
        scheduler, _ = build_scheduler(lead, children, serialize_turns=serialize_turns)
        box["scheduler"] = scheduler
        return await asyncio.wait_for(scheduler.run("please delegate"), GATE_TIMEOUT_SECONDS)

    assert asyncio.run(scenario()) == "final: coder, tester"
    return seen


def test_a_teammate_waiting_for_the_turn_is_marked_queued_not_the_one_running():
    seen = _snapshot_inside_first_turn(serialize_turns=True)
    running = next(e for e in seen.values() if e["me"])
    waiting = next(e for e in seen.values() if not e["me"] and e["role"] != "lead")

    assert waiting["busy"] is True          # its driver task exists ...
    assert waiting["turn_queued"] is True   # ... but it cannot run yet
    assert running["turn_queued"] is False


def test_the_manifest_roster_does_not_carry_the_display_field():
    """team_snapshot is also written to the team manifest; it stays as it was."""
    seen: list = []
    lead = ScriptedSession("lead", [suspend_spawning([("coder", "do it", "tc-1")]),
                                    resume_done(lambda results: "done")])

    async def scenario():
        def step_fn(sess):
            async def step(s: ScriptedSession) -> str:
                seen.extend(box["scheduler"].team_snapshot())
                s.state.set_phase(SessionPhase.DONE)
                s.state.append_message({"role": "assistant", "content": "x"})
                return "x"
            return step
        box = {}
        scheduler, _ = build_scheduler(lead, [ScriptedSession("coder", [step_fn(None)])],
                                       serialize_turns=True)
        box["scheduler"] = scheduler
        return await asyncio.wait_for(scheduler.run("go"), GATE_TIMEOUT_SECONDS)

    asyncio.run(scenario())
    assert seen and all("turn_queued" not in e for e in seen)


def test_nothing_is_queued_when_turns_run_concurrently():
    seen = _snapshot_inside_first_turn(serialize_turns=False)

    assert all(e["turn_queued"] is False for e in seen.values())


class _Roster:
    def __init__(self, entries):
        self._entries = entries

    def team_snapshot(self):
        return self._entries


def _render(entries) -> str:
    tool = TeamStatusTool(_Roster(entries))
    return asyncio.run(tool.execute_with_runtime({}, runtime=None))


def test_team_status_prints_a_queued_teammate_as_waiting_for_the_turn():
    out = _render([
        {"aid": 0, "role": "adopter", "parent_aid": None, "phase": "running",
         "busy": True, "turn_queued": False},
        {"aid": 1, "role": "coder_a", "parent_aid": 0, "phase": "idle",
         "busy": True, "turn_queued": True},
    ])
    adopter, coder = out.splitlines()[1:3]

    assert adopter.endswith("— busy")
    assert "busy" not in coder
    assert "queued" in coder and "ends its turn" in coder


def test_team_status_is_unchanged_for_a_roster_without_the_field():
    out = _render([{"aid": 1, "role": "coder_a", "parent_aid": 0, "phase": "idle", "busy": True}])

    assert out.splitlines()[1].endswith("— busy")


def test_submit_says_it_is_also_how_to_wait_for_a_teammate():
    text = SubmitTool.description

    assert "finished" in text                          # the original meaning stays
    assert "waiting" in text and "teammate" in text    # and waiting is named
    assert "reopens your turn" in text


def test_team_status_run_by_the_running_agent_shows_its_teammate_queued():
    """End to end: the tool reads the scheduler's display rows, not the manifest roster."""
    printed: list[str] = []
    box: dict = {}

    def turn(role: str):
        async def step(sess: ScriptedSession) -> str:
            if not printed:
                for _ in range(5):
                    await asyncio.sleep(0)
                out = await TeamStatusTool(box["scheduler"]).execute_with_runtime({}, runtime=None)
                printed.append(role)
                printed.extend(out.splitlines()[1:])
            sess.state.set_phase(SessionPhase.DONE)
            sess.state.append_message({"role": "assistant", "content": role})
            return role

        return step

    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1"), ("tester", "check", "tc-2")]),
            resume_done(lambda results: "final"),
        ],
    )

    async def scenario():
        children = [ScriptedSession("coder", [turn("coder")]),
                    ScriptedSession("tester", [turn("tester")])]
        scheduler, _ = build_scheduler(lead, children, serialize_turns=True)
        box["scheduler"] = scheduler
        return await asyncio.wait_for(scheduler.run("go"), GATE_TIMEOUT_SECONDS)

    asyncio.run(scenario())
    me, lines = printed[0], printed[1:]
    other = "tester" if me == "coder" else "coder"

    assert "queued" in next(line for line in lines if f": {other} " in line)
    assert next(line for line in lines if f": {me} " in line).endswith("— busy")
