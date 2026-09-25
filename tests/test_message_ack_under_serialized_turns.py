"""What ``message_agent`` tells a sender when the team runs one turn at a time.

Under ``serialize_turns`` a messaged teammate cannot start until the sender ends
its turn. The general acknowledgement says "carry on with whatever you can do
meanwhile", which under serialization is the one move that keeps the teammate
from ever running. 09-25, gpt-5.6-luna on the s2dual judge card, after
``team_status`` was fixed to say "queued": the Adopter read "queued" 11 times
and made its next call itself 11 of 11 times; 15/36 runs ended with an empty
patch because no Coder ran before the Adopter's budget was gone. So a
serialized team's acknowledgement says it directly: submit now.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from scheduler_awaiting_test_support import ScriptedSession, build_scheduler

from opencollab.adapters.tools.message import _DELIVERY_NOTE, MessageAgentTool

ACK = "Message queued for aid 1."


class _Team:
    def __init__(self, *, serialized: bool | None):
        if serialized is not None:
            self.turns_serialized = serialized

    def team_snapshot(self):
        return [{"aid": 0, "role": "adopter"}, {"aid": 1, "role": "coder_a"}]

    async def send_message(self, from_aid, to_aid, summary, content):
        return ACK


def _send(team) -> str:
    tool = MessageAgentTool(team)
    params = {"to_role": "coder_a", "summary": "fix", "content": "please fix it"}
    return asyncio.run(tool.execute_with_runtime(params, runtime=SimpleNamespace(aid=0)))


def test_a_serialized_sender_is_told_to_submit_now():
    out = _send(_Team(serialized=True))

    assert out.startswith(ACK)
    assert "cannot start until you end your turn" in out
    assert "call submit now" in out.lower()
    assert "reopens your turn" in out
    assert "meanwhile" not in out        # the advice that kept luna working in-turn


def test_a_concurrent_sender_keeps_the_general_note():
    assert _send(_Team(serialized=False)) == f"{ACK} {_DELIVERY_NOTE}"


def test_a_scheduler_without_the_flag_keeps_the_general_note():
    assert _send(_Team(serialized=None)) == f"{ACK} {_DELIVERY_NOTE}"


def test_the_scheduler_reports_whether_turns_are_serialized():
    lead = ScriptedSession("lead", [])
    assert build_scheduler(lead, [], serialize_turns=True)[0].turns_serialized is True
    lead = ScriptedSession("lead", [])
    assert build_scheduler(lead, [], serialize_turns=False)[0].turns_serialized is False
