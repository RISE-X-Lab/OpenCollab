"""Exercise Reconstruct with a subprocess adapter that consumes JSON request fields."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.builtin_workflows import ReconstructionTask, register_task
from opencollab.tools import builtin_tools

SOURCE = """import json
import sys

def solve(items):
    return sum(items)

if __name__ == "__main__":
    print(json.dumps([solve(items) for items in json.loads(sys.argv[1])]))
"""


class Model:
    async def complete(self, messages, **kwargs):
        if any(message["role"] == "tool" for message in messages):
            return LLMResponse(content="Implemented the sum.", usage=Usage(4, 2), finish_reason="stop")
        return LLMResponse(
            tool_calls=[{
                "id": "write-candidate",
                "type": "function",
                "function": {
                    "name": "file_write",
                    "arguments": json.dumps({"path": "candidate.py", "mode": "create", "content": SOURCE}),
                },
            }],
            usage=Usage(4, 2),
            finish_reason="tool_calls",
        )


async def execute(candidate, samples, workspace):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(candidate.relative_to(workspace)), json.dumps(samples),
        cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


@pytest.mark.parametrize("consume_fields", [False, True])
async def test_reconstruct_evaluator_cannot_rewrite_observed_examples(tmp_path: Path, consume_fields: bool) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    observed = []
    original = [{"items": [1, 3]}, {"items": [3, 5]}]

    async def evaluate(candidate, inputs):
        # The subprocess takes positional item lists. Consume the input field to
        # separate it from request metadata while building batch arguments.
        samples = [
            row.pop("items", []) if consume_fields else row.get("items", [])
            for row in inputs
        ]
        outputs = await execute(candidate, samples, workspace)
        observed.append(outputs)
        return [{"ok": True, "output": output} for output in outputs]

    task = ReconstructionTask(
        input_type="object", input_constraints="The items field is a list of integers.",
        function_name="solve", workspace=workspace, oracle=lambda row: sum(row["items"]),
        evaluate=evaluate, random_inputs=lambda count, exclude: original,
        tools=builtin_tools("file_write"),
    )
    result = await OpenCollab(workspace, model="scripted-local-model").workflow(
        "reconstruct", {"task": register_task(task), "random_share": 1, "early_stop": 1},
        llm=Model(), budget=60_000, limit_mode="explicit", trace=False,
    )
    assert result.ok, result.reason
    delivered = workspace / "candidate.py"
    assert delivered.read_text(encoding="utf-8") == SOURCE
    held_out = await execute(delivered, [[7, 11]], workspace)
    print(json.dumps({
        "consume_fields": consume_fields,
        "status": result.status,
        "examples": result.output["examples"],
        "promotions": result.output["promotions"],
        "score": result.output["final_score"],
        "evaluations": observed,
        "held_out": held_out,
    }))
    assert held_out == [18]
    assert observed == [[4, 8], [4, 8]]
    assert result.output["examples"] == [[original[0], 4, "random"], [original[1], 8, "random"]]
    assert result.output["promotions"] == 1
    assert result.output["final_score"] == 0.997
