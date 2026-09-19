"""Run one agent through the thin loop and print every step.

    uv run python scripts/thin_run.py "task" --workspace .          # real model, configs/.env
    uv run python scripts/thin_run.py "task" --workspace . --fake   # scripted model, no API key

The real run wires the same pieces the CLI uses: ``build_config`` for the
provider settings, ``LLMClient`` for the model, the ``coding`` tool preset, a
``LocalEnvironment`` rooted at the workspace, and the workspace sandbox for
commands. The fake run swaps only the model for a two-reply script: one
``file_read`` call, then a plain answer.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from types import SimpleNamespace
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import LLMClient
from opencollab.application.thin_run import ThinRun
from opencollab.bootstrap.config import build_config, missing_api_key
from opencollab.bootstrap.programmatic import resolve_tools
from opencollab.bootstrap.runtime_context import build_workspace_safety_policy
from opencollab.domain.agent import Agent

# ``{workspace}`` is filled with the absolute workspace path at run time.
SYSTEM_PROMPT = (
    "You are a software engineer working inside the workspace at {workspace}. All paths are "
    "relative to it; do not search outside it. Use the tools to inspect and change files. When "
    "the task is finished, reply with a plain-text summary of what you did and what you "
    "verified, and make no tool call."
)


class PrintingRun(ThinRun):
    """The thin loop with one print after each model reply and each tool result."""

    async def query(self):
        response = await super().query()
        names = [call["function"]["name"] for call in response.tool_calls or []]
        print(f"[step {self.steps}] model: {(response.content or '')[:100]!r} tool_calls={names}")
        return response

    async def execute(self, tool_call: dict[str, Any]) -> str:
        output = await super().execute(tool_call)
        print(f"[step {self.steps}] {tool_call['function']['name']} -> {output[:100]!r}")
        return output


class ScriptedLLM:
    """Replies in order; the shape is what ``LLMPort.complete`` returns."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        if not self.replies:
            raise RuntimeError("scripted model has no reply left")
        return self.replies.pop(0)


def scripted_reply(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> Any:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="stop",
        usage=SimpleNamespace(input_tokens=10, total_tokens=15),
        reasoning=None,
        provider_items=[],
        provider_model=None,
    )


def fake_llm(task: str, path: str) -> ScriptedLLM:
    read = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "file_read", "arguments": json.dumps({"path": path, "limit": 5})},
    }
    return ScriptedLLM(
        [
            scripted_reply(tool_calls=[read]),
            scripted_reply(content=f"Read {path}. Task noted: {task[:60]}"),
        ]
    )


def real_llm(cfg: Any) -> LLMClient:
    return LLMClient(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        provider=cfg.provider,
        wire_protocol=cfg.wire_protocol,
        max_retries=cfg.llm_max_retries,
        request_timeout=cfg.llm_timeout,
        connect_timeout=cfg.llm_connect_timeout,
        first_event_timeout=cfg.llm_first_event_timeout,
        stream_idle_timeout=cfg.llm_stream_idle_timeout,
        context_window=cfg.context_window,
        provider_error_time_budget=cfg.provider_error_time_budget,
    )


async def main(args: argparse.Namespace) -> int:
    cfg = build_config(args.workspace)
    if args.fake:
        llm: Any = fake_llm(args.task, args.fake_path)
    else:
        if missing_api_key(cfg.provider, cfg.api_key, cfg.base_url):
            print("No API key: set OPENCOLLAB_API_KEY (see configs/.env.example) or pass --fake.")
            return 2
        llm = real_llm(cfg)
    environment = LocalEnvironment(args.workspace)
    agent = Agent(
        name="solo",
        system_prompt=SYSTEM_PROMPT.format(workspace=os.path.abspath(args.workspace)),
        tools=list(resolve_tools("coding")),
        model=cfg.model,
        provider=cfg.provider,
        wire_protocol=cfg.wire_protocol,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        max_tokens_per_step=cfg.max_output_tokens,
        context_window=cfg.context_window,
        thinking=cfg.thinking,
        thinking_params=cfg.thinking_params,
        reasoning_effort=cfg.reasoning_effort,
    )
    loop = PrintingRun(llm, agent, environment, step_limit=args.step_limit, token_limit=cfg.budget)
    # Same command sandbox the CLI wires: blocks destructive shell commands.
    loop.tools.safety_policy = build_workspace_safety_policy(environment)
    try:
        reason, final_text = await loop.run(args.task)
    finally:
        await environment.cleanup()
        close = getattr(llm, "close", None)
        if callable(close):
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome
    print(f"\n(reason, final_text) = {(reason, final_text)!r}")
    print(f"steps = {loop.steps}, total tokens = {loop.tokens}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one agent through the thin loop.")
    parser.add_argument("task")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--fake", action="store_true", help="use the two-reply scripted model")
    parser.add_argument("--fake-path", default="README.md", help="file the scripted model reads")
    parser.add_argument("--step-limit", type=int, default=100)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args(sys.argv[1:]))))
