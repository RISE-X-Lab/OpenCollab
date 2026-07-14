"""Run one provider completion using the normal OpenCollab configuration."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from opencollab.adapters.llm import LLMClient
from opencollab.bootstrap.config import build_config

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = "Reply with one short sentence confirming provider connectivity."


class CompletionClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...


async def request_completion(
    prompt: str,
    *,
    workspace: Path = WORKSPACE,
    client_type: type[CompletionClient] = LLMClient,
) -> str:
    """Return one completion using the same configuration as the framework."""
    config = build_config(str(workspace))
    if not config.api_key:
        raise ValueError("provider API key is missing from the OpenCollab configuration")
    client = client_type(
        model=config.model,
        provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        request_timeout=config.llm_timeout,
    )
    response = await client.complete(
        [
            {"role": "system", "content": "You are a concise connectivity probe."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_output_tokens=min(config.max_output_tokens, 256),
    )
    return str(response.content or "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(asyncio.run(request_completion(args.prompt, workspace=args.workspace)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
