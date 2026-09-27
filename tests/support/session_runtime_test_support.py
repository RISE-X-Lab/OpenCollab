"""Call envelopes and recording doubles for session runtime tests."""

import copy

from opencollab.domain.tools import ToolProcessingResult


def tool_call(call_id="call-1", name="fake_tool", arguments='{"value": 1}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeToolExecution:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ToolProcessingResult()

    async def process(self, tool_calls):
        self.calls.append(copy.deepcopy(tool_calls))
        return self.result


class FakeTracer:
    def __init__(self):
        self.steps = []
        self.flush_count = 0

    def log_step(self, step_type, payload, tokens=0, latency=0.0):
        self.steps.append(
            {
                "step_type": step_type,
                "payload": copy.deepcopy(payload),
                "tokens": tokens,
                "latency": latency,
            }
        )

    def flush(self):
        self.flush_count += 1
