"""State and read-tool preparation for scout evidence tests."""

from opencollab.domain.session import TurnEnforcementState


class _ReadStub:
    name = "file_read"

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


class _FakeState:
    def __init__(self, used_tokens=0, scout_ledger=None, messages=None):
        self.used_tokens = used_tokens
        self.wind_down_done = False
        self.wind_down_token_mark = 0
        self.messages = messages if messages is not None else []
        self.turn = TurnEnforcementState(
            scout_ledger=scout_ledger if scout_ledger is not None else []
        )
