import asyncio
import gc
import json
import os
import subprocess
import sys

import pytest
from opencollab.adapters.cli import eval as eval_cli
from opencollab.adapters.env import Environment, ExecResult, LocalEnvironment
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.harness import evaluator
from opencollab.harness.evaluator import (
    EvalResult,
    EvalTask,
    run_eval_batch,
    run_eval_task,
    save_results,
)
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    metric_submission_integrity,
)

__all__ = [
    "CapturingLLMClient",
    "Environment",
    "EvalResult",
    "EvalTask",
    "ExecResult",
    "FakeEnv",
    "FakeLLMClient",
    "InjectFakeEnv",
    "LLMResponse",
    "LocalEnvironment",
    "SUBMISSION_INTEGRITY_PROVEN",
    "Usage",
    "asyncio",
    "container",
    "eval_cli",
    "evaluator",
    "gc",
    "is_worktree_diff_cmd",
    "json",
    "metric_submission_integrity",
    "os",
    "pytest",
    "run",
    "run_eval_batch",
    "run_eval_task",
    "save_results",
    "subprocess",
    "sys",
]


def run(coro):
    return asyncio.run(coro)


def is_worktree_diff_cmd(cmd: str) -> bool:
    return "git diff --cached --binary HEAD" in cmd


class FakeLLMClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


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


class CapturingLLMClient:
    """Fake LLM client that records every kwarg passed to ``complete``.

    Accepts ``**kwargs`` so a forwarded ``top_p`` (or ``thinking`` etc.) does
    not raise — lets a test assert the sampling knob reaches the provider call.
    """

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        CapturingLLMClient.last_kwargs = {"temperature": temperature, **kwargs}
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


class InjectFakeEnv(Environment):
    """Env that faithfully models git's per-path revert of injected test files.

    Models the real driver's contamination surface: the submitted patch is
    extracted with ``git add -A && git diff --cached`` (``staged_diff`` here), so
    any injected test edit still in the tree at extraction time LEAKS. Each
    injected path can be a tracked modification (revertible with
    ``git checkout --``) or a brand-new untracked file (which ``git checkout``
    canNOT remove — it errors rc=1 — and only ``git clean -fq`` deletes). A path
    is excluded from the extracted diff only once it has been BOTH checked out and
    cleaned per-path, matching the production exclusion. This exposes the new-file
    leak the old always-succeeds fake hid.
    """

    def __init__(self, src_path="src/app.py", mod_path=None, new_path=None):
        self.src_path = src_path
        self.mod_path = mod_path  # injected tracked-file modification (or None)
        self.new_path = new_path  # injected brand-new untracked file (or None)
        # A path is "present in the working tree" (and thus leaks into the staged
        # diff) until reverted. Tracked mods clear on checkout; new files clear
        # only on clean (checkout errors on them).
        self.checked_out: set[str] = set()
        self.cleaned: set[str] = set()
        self.cmds: list[str] = []
        self.cleaned_up = False

    async def write_file(self, path: str, content: str) -> None:
        pass

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{id(self):x}{suffix}"
        await self.write_file(path, content)
        return path

    async def remove_file(self, path: str) -> None:
        return None

    def _leaks(self, path: str | None, *, untracked: bool) -> bool:
        if not path:
            return False
        if untracked:
            return path not in self.cleaned  # only `git clean` removes it
        return path not in self.checked_out  # `git checkout` reverts it

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        checkout_prefix = "git --literal-pathspecs checkout -- "
        clean_prefix = "git --literal-pathspecs clean -fq -- "
        status_prefix = "git --literal-pathspecs status --porcelain=v1 -z -- "
        if cmd.startswith(checkout_prefix):
            path = cmd[len(checkout_prefix) :].strip().strip("'\"")
            # git checkout errors (rc=1) on an untracked/new path and reverts
            # nothing; it restores a tracked modification.
            if path == self.new_path:
                return ExecResult(
                    returncode=1,
                    stdout="",
                    stderr=f"error: pathspec '{path}' did not match any file(s) known to git",
                )
            self.checked_out.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith(clean_prefix):
            path = cmd[len(clean_prefix) :].strip().strip("'\"")
            self.cleaned.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith(status_prefix):
            path = cmd[len(status_prefix) :].strip().strip("'\"")
            dirty = self._leaks(path, untracked=path == self.new_path)
            return ExecResult(
                returncode=0,
                stdout=f"?? {path}\n" if dirty else "",
                stderr="",
            )
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            parts = [f"diff --git a/{self.src_path} b/{self.src_path}\n+fix\n"]
            if self._leaks(self.mod_path, untracked=False):
                parts.append(f"diff --git a/{self.mod_path} b/{self.mod_path}\n+assert thing\n")
            if self._leaks(self.new_path, untracked=True):
                parts.append(f"diff --git a/{self.new_path} b/{self.new_path}\nnew file mode 100644\n+brand new test\n")
            return ExecResult(returncode=0, stdout="".join(parts), stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True
