"""The thin single-agent loop against a scripted model and a scripted tool."""

from __future__ import annotations

import json

from session_run_test_support import FakeLLM, llm_response, run, tool_call

from opencollab.application.thin_run import ThinRun
from opencollab.domain.agent import Agent


class EchoTool:
    name = "echo"
    description = "Echo the value back."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    default_timeout = None
    disable_outer_timeout = False

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": self.parameters}}

    async def execute_with_runtime(self, params, runtime):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("echo broke")
        return f"echo: {params['value']}"


def _agent(tool=None):
    return Agent(name="solo", system_prompt="be brief", tools=[tool] if tool else [])


def _call(value="7"):
    return tool_call(name="echo", arguments=json.dumps({"value": value}))


def test_plain_answer_completes_without_a_tool_call():
    llm = FakeLLM([llm_response(content="the answer")])
    loop = ThinRun(llm, _agent(), None)

    assert run(loop.run("question")) == ("completed", "the answer")
    assert len(llm.calls) == 1
    assert [m["role"] for m in loop.messages] == ["system", "user", "assistant"]
    assert loop.messages[1] == {"role": "user", "content": "question"}


def test_tool_call_is_executed_appended_as_tool_message_then_model_is_called_again():
    tool = EchoTool()
    llm = FakeLLM([llm_response(tool_calls=[_call("7")]), llm_response(content="done")])
    loop = ThinRun(llm, _agent(tool), None)

    reason, text = run(loop.run("go"))

    assert (reason, text) == ("completed", "done")
    assert tool.calls == [{"value": "7"}]
    assert loop.messages[3] == {"role": "tool", "tool_call_id": "call-1", "content": "echo: 7"}
    assert len(llm.calls) == 2
    assert llm.calls[1]["messages"][3]["content"] == "echo: 7"
    assert llm.calls[0]["tools"] == [tool.to_openai_schema()]


def test_step_limit_stops_before_the_third_query():
    tool = EchoTool()
    llm = FakeLLM([llm_response(tool_calls=[_call()]), llm_response(tool_calls=[_call()])])
    loop = ThinRun(llm, _agent(tool), None, step_limit=2)

    reason, text = run(loop.run("loop forever"))

    assert "step" in reason
    assert reason == "step limit reached: 2 steps"
    assert text == ""
    assert len(llm.calls) == 2
    assert len(tool.calls) == 2


def test_tool_exception_becomes_a_string_and_the_loop_continues():
    tool = EchoTool(fail=True)
    llm = FakeLLM([llm_response(tool_calls=[_call()]), llm_response(content="recovered")])
    loop = ThinRun(llm, _agent(tool), None)

    reason, text = run(loop.run("go"))

    assert (reason, text) == ("completed", "recovered")
    observed = loop.messages[3]
    assert observed["role"] == "tool"
    assert observed["content"] == "Tool execution error: RuntimeError: echo broke"
    assert len(llm.calls) == 2


def test_token_limit_stops_the_next_query():
    tool = EchoTool()
    llm = FakeLLM(
        [
            llm_response(tool_calls=[_call()], total_tokens=60),
            llm_response(tool_calls=[_call()], total_tokens=60),
        ]
    )
    loop = ThinRun(llm, _agent(tool), None, token_limit=100)

    reason, _text = run(loop.run("go"))

    assert "token" in reason
    assert reason == "token limit reached: 120 tokens used"
    assert loop.tokens == 120
    assert len(llm.calls) == 2


def test_a_call_that_crosses_the_token_limit_stops_before_its_tools_run():
    tool = EchoTool()
    llm = FakeLLM([llm_response(tool_calls=[_call()], total_tokens=100), llm_response(content="never")])
    loop = ThinRun(llm, _agent(tool), None, token_limit=100)

    reason, text = run(loop.run("go"))

    assert reason == "token limit reached: 100 tokens used"
    assert text == ""
    assert tool.calls == []
    assert len(llm.calls) == 1


class RecordingTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, *, step_type, payload, tokens=0, latency=0.0):
        self.steps.append((step_type, payload, tokens))

    def flush(self):
        return None


def test_tracer_gets_one_llm_call_row_per_query_and_one_tool_exec_row_per_call():
    tool = EchoTool()
    tracer = RecordingTracer()
    llm = FakeLLM([llm_response(tool_calls=[_call("7")], total_tokens=9), llm_response(content="done")])
    loop = ThinRun(llm, _agent(tool), None, tracer=tracer)

    run(loop.run("go"))

    assert [step_type for step_type, _p, _t in tracer.steps] == ["llm_call", "tool_exec", "llm_call"]
    first_call, tool_exec = tracer.steps[0], tracer.steps[1]
    assert first_call[1]["step"] == 1 and first_call[2] == 9
    assert tool_exec[1] == {"step": 1, "tool": "echo", "args": {"value": "7"}, "output_chars": len("echo: 7")}
