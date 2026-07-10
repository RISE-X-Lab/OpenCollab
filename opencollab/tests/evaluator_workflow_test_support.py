from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import threading
import uuid
from typing import Any

import pytest
from opencollab.adapters.env import Environment, ExecResult, LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import build_session as real_build_session
from opencollab.harness import evaluator
from opencollab.harness import swe_checkpoint as checkpoint_mod
from opencollab.harness.evaluator import EvalResult, EvalTask, run_eval_task
from opencollab.harness.swe_checkpoint import WorktreeCheckpoint
from opencollab.harness.workflows import generate_review_fix

__all__ = [
    "Any",
    "CheckpointEnv",
    "Environment",
    "EvalResult",
    "EvalTask",
    "ExecResult",
    "FakeEnv",
    "FakeSession",
    "LLMResponse",
    "LocalEnvironment",
    "ScriptedCtx",
    "SessionStore",
    "Usage",
    "WorktreeCheckpoint",
    "_token_bearing_factory",
    "asyncio",
    "checkpoint_mod",
    "contextlib",
    "evaluator",
    "generate_review_fix",
    "hashlib",
    "is_worktree_diff_cmd",
    "json",
    "os",
    "pytest",
    "real_build_session",
    "run",
    "run_eval_task",
    "seed_checkpoint",
    "subprocess",
    "threading",
    "uuid",
]


def run(coro):
    return asyncio.run(coro)


def is_worktree_diff_cmd(cmd: str) -> bool:
    return "git diff --cached --binary HEAD" in cmd


class FakeEnv(Environment):
    def __init__(self, diff="diff --git a/x b/x\n+new\n"):
        self.diff = diff
        self.cleaned_up = False
        self.cmds = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


class CheckpointEnv(FakeEnv):
    def __init__(self, diff="diff --git a/x b/x\n+checkpoint\n", diff_outputs=None):
        super().__init__(diff=diff)
        self.writes: list[tuple[str, str]] = []
        self.diff_outputs = list(diff_outputs or [])

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd):
            stdout = self.diff_outputs.pop(0) if self.diff_outputs else self.diff
            return ExecResult(returncode=0, stdout=stdout, stderr="")
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def write_file(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        await self.write_file(path, content)
        return path


def seed_checkpoint(
    checkpoint: WorktreeCheckpoint,
    patch: str,
    *,
    submission_eligible: bool = True,
    status: str = "written",
) -> None:
    checkpoint.patch_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.patch_path.write_text(patch, encoding="utf-8")
    checkpoint.meta_path.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_worktree_checkpoint.v1",
                "status": status,
                "patch_bytes": len(patch.encode("utf-8", errors="surrogatepass")),
                "patch_sha256": checkpoint_mod._patch_sha(patch),
                "submission_eligible": submission_eligible,
                "preserved_previous_patch": status == "failed",
            }
        ),
        encoding="utf-8",
    )


class FakeSession:
    """Duck-typed workflow session that records a fixed token count."""

    def __init__(self, *, env: Any, tokens: int, reply: str = "ok") -> None:
        self.env = env
        self.used_tokens = tokens
        self.step_count = 1
        self.reply = reply
        self.messages: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.messages.append(content)

    async def run_loop(self) -> str:
        return self.reply


@contextlib.contextmanager
def _token_bearing_factory(env: Any, tokens: int = 7):
    """Patch the eval session factory so workflow agents report fixed tokens/steps.

    Mirrors the inline patch in ``test_workflow_mode_aggregates_tokens_across_sessions``
    so abnormal-exit tests can assert metrics survived (each agent -> 1 session,
    ``tokens`` tokens, 1 step).
    """
    import opencollab.harness.evaluator as evaluator_mod

    original = evaluator_mod._build_eval_session_factory

    def patched_factory(*args, **kwargs):
        factory = original(*args, **kwargs)

        def build(
            *,
            prompt,
            budget,
            tools=None,
            isolation=False,
            label=None,
            tool_choice=None,
            thinking=None,
        ):
            return FakeSession(env=env, tokens=tokens)

        factory.build_workflow_session = build  # type: ignore[attr-defined]
        return factory

    evaluator_mod._build_eval_session_factory = patched_factory
    try:
        yield
    finally:
        evaluator_mod._build_eval_session_factory = original


class ScriptedCtx:
    """A minimal WorkflowContext stand-in scripting agent() replies."""

    def __init__(self, env: Any, replies: list[Any]) -> None:
        self.env = env
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append({"prompt": prompt, "schema": schema, "label": label})
        return self._replies.pop(0)

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        pass
