"""Instruction integrity is checked before a provider response reaches tools."""
import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.adapters.llm.responses_provider import (
    _build_request_kwargs,
    _check_instructions_echo,
)
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT


def test_native_system_prompt_is_explicit_in_responses_request():
    request = _build_request_kwargs(
        "gpt-5.6-luna",
        [{"role": "system", "content": SINGLE2_SYSTEM_PROMPT},
         {"role": "user", "content": "Original role and task instructions"}],
        None, 1.0, reasoning_effort="max",
    )
    assert request["instructions"] == SINGLE2_SYSTEM_PROMPT
    assert request["reasoning"]["effort"] == "max"


def test_changed_instructions_are_archived_and_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO", "1")
    monkeypatch.setenv("OPENCOLLAB_INSTRUCTIONS_AUDIT_DIR", str(tmp_path))
    response = SimpleNamespace(id="resp_test", model="gpt-5.6-luna", instructions="different instructions")
    with pytest.raises(ResponsesProtocolError, match="instructions differ"):
        _check_instructions_echo(response, {"model":"gpt-5.6-luna", "instructions":SINGLE2_SYSTEM_PROMPT})
    record = json.loads((tmp_path/"resp_test.json").read_text())
    assert record["instructions_match"] is False
    assert record["validation_stage"] == "before_solver_delivery"


def test_matching_native_instructions_are_accepted(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO", "1")
    response = SimpleNamespace(id="resp_test", instructions=SINGLE2_SYSTEM_PROMPT)
    _check_instructions_echo(response, {"instructions":SINGLE2_SYSTEM_PROMPT})


def test_native_user_only_summary_keeps_system_instructions_empty(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO", "1")
    request = _build_request_kwargs(
        "gpt-5.6-luna", [{"role":"user", "content":"Summarize the recorded work"}],
        None, 0.0, reasoning_effort="max",
    )
    assert request["instructions"] == ""
    _check_instructions_echo(SimpleNamespace(instructions=None), request)
    with pytest.raises(ResponsesProtocolError, match="instructions differ"):
        _check_instructions_echo(SimpleNamespace(instructions="injected prompt"), request)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_instruction_rewrite_is_rejected_before_returning_tools(monkeypatch, streaming):
    from opencollab.adapters.llm.responses_provider import complete_responses
    from tests.support.responses_provider_test_support import FakeStream, completed_response, function_item, ns

    monkeypatch.setenv("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO", "1")
    item = function_item("call_1", "bash", '{"command":"run task"}')
    response = completed_response(output=[item])
    response.instructions = "changed role instructions"

    async def create(**kwargs):
        assert kwargs["instructions"] == "original role instructions"
        if not streaming:
            return response
        return FakeStream([
            ns(type="response.output_item.done", output_index=0, item=item),
            ns(type="response.completed", response=response),
        ])

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ResponsesProtocolError, match="instructions differ"):
        await complete_responses(
            client, "gpt-fake", [
                {"role": "system", "content": "original role instructions"},
                {"role": "user", "content": "execute task"},
            ],
            None, 0.0, max_retries=0, stream=streaming,
        )
