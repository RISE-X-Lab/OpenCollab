import asyncio

from opencollab.tui.session_adapter import TuiPermissionPolicy


def run(coro):
    return asyncio.run(coro)


class FakeRender:
    def __init__(self):
        self.events = []

    def suspend_live(self) -> bool:
        self.events.append("suspend")
        return True

    def resume_live(self, was_suspended: bool) -> None:
        self.events.append(("resume", was_suspended))


def test_tui_permission_policy_suspends_resumes_and_parses_yes():
    render = FakeRender()
    answers = iter(["yes\n"])

    async def fake_read(prompt: str) -> str:
        assert prompt.endswith("[y/N] ")
        return next(answers)

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is True
    assert render.events == ["suspend", ("resume", True)]


def test_tui_permission_policy_parses_no():
    render = FakeRender()

    async def fake_read(prompt: str) -> str:
        return "n"

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is False
    assert render.events == ["suspend", ("resume", True)]


def test_tui_permission_policy_resumes_on_eof():
    render = FakeRender()

    async def fake_read(prompt: str) -> str:
        raise EOFError

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is False
    assert ("resume", True) in render.events
