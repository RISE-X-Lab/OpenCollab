"""Run installed evolution with local model responses and real file checks."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import opencollab
from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage


class FileEditingModel:
    """Produce native file calls through the normal session executor."""

    def __init__(self):
        self.calls = []
        self.closed = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), kwargs))
        prompt = next(message["content"] for message in messages if message["role"] == "user")
        results = [message for message in messages if message["role"] == "tool"]
        if "create-alpha" in prompt and not results:
            name, arguments = "file_write", {"path": "shared.txt", "mode": "create", "content": "alpha\n"}
        elif "append-beta" in prompt and not results:
            name, arguments = "file_read", {"path": "shared.txt"}
        elif "append-beta" in prompt and len(results) == 1:
            assert "alpha" in str(results[0]["content"]), results
            name, arguments = "file_write", {
                "path": "shared.txt", "mode": "create", "overwrite": True, "content": "alpha\nbeta\n",
            }
        else:
            return LLMResponse(content="File task finished", usage=Usage(4, 2), finish_reason="stop")
        assert name in {tool["function"]["name"] for tool in tools}, tools
        return LLMResponse(tool_calls=[{
            "id": f"edit-{len(self.calls)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }], usage=Usage(4, 2), finish_reason="tool_calls")

    def close(self):
        self.closed += 1


async def exercise_evolution(workspace: Path) -> dict:
    model = FileEditingModel()
    probe = workspace / "check_file.py"
    probe.write_text(
        "from pathlib import Path\n"
        "with Path('checks-ran.txt').open('a') as stream:\n"
        "    stream.write('executed\\n')\n"
        "assert Path('shared.txt').read_text() == 'alpha\\nbeta\\n'\n"
        "print('SHARED_FILE_OK')\n",
        encoding="utf-8",
    )
    result = await OpenCollab(workspace, model="local-file-model", config={"max_output_tokens": 128}).workflow(
        "evolution", {
            "groups": [
                {"id": "alpha", "prompt": "create-alpha writes alpha to shared.txt", "resources": ["shared.txt"]},
                {"id": "beta", "prompt": "append-beta reads shared.txt and adds beta", "dependencies": ["alpha"],
                 "resources": ["shared.txt"]},
            ],
            "check_commands": [{
                "command": f"{shlex.quote(sys.executable)} check_file.py", "expected_output": "SHARED_FILE_OK",
            }],
            "config": {"main_budget": 20_000, "max_steps": 12, "max_output_tokens": 128},
        },
        llm=model, budget=30_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok, (result.status, result.reason, result.output)
    assert result.output["delivery_ok"] is True, result.output
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"
    checks = (workspace / "checks-ran.txt").read_text(encoding="utf-8").splitlines()
    assert len(checks) >= 2 and set(checks) == {"executed"}, checks
    starts = [messages for messages, _tools, _kwargs in model.calls if not any(
        message["role"] == "tool" for message in messages
    )]
    assert len(starts) == 2, starts
    prompts = [next(message["content"] for message in messages if message["role"] == "user") for messages in starts]
    assert "create-alpha" in prompts[0] and "append-beta" in prompts[1], prompts
    assert not any(message.get("content") == prompts[0] for message in starts[1]), starts[1]
    assert result.tokens == len(model.calls) * 6 > 0
    assert model.closed == 0
    return {"file": "shared.txt", "content": "alpha\nbeta\n", "checks": len(checks), "model_calls": len(model.calls),
            "tokens": result.tokens, "delivery_ok": result.output["delivery_ok"]}


def main() -> None:
    package = Path(opencollab.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert not (package.parent.parent / "examples").exists()
    cli = Path(sys.executable).with_name("opencollab")
    with tempfile.TemporaryDirectory(prefix="opencollab-installed-evolution-") as directory:
        workspace = Path(directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("OPENCOLLAB_")}
        env.pop("PYTHONPATH", None)
        listed = subprocess.run(
            [str(cli), "workflow", "list", "--workspace", str(workspace)],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
        )
        assert listed.returncode == 0, (listed.stdout, listed.stderr)
        assert "evolution" in listed.stdout, listed.stdout
        report = asyncio.run(exercise_evolution(workspace))
    print(json.dumps({"package": str(package), "execution": report}, indent=2))


if __name__ == "__main__":
    main()
