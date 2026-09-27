"""Session preparation for worktree delivery and trace tests."""

from __future__ import annotations

from opencollab.domain.session import SessionPhase, SessionState


class _ChildSession:
    def __init__(self, role: str, result: str, env):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = env
        self._result = result

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return self._result


class _LeadSession:
    def __init__(self):
        self.agent = type("_Agent", (), {"name": "lead"})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = None

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _Factory:
    def __init__(self, child: _ChildSession):
        self._child = child

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        self._child.state.aid = aid
        return self._child
