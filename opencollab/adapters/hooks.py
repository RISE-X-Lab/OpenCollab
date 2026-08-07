"""Lifecycle hooks executed through the shared subprocess supervisor."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from typing import Any

from opencollab.adapters._env_process import ProcessCleanupError, run_process
from opencollab.application.ports import HookPort
from opencollab.domain.hooks import HookOutcome, HookSpec, match_hooks

logger = logging.getLogger(__name__)
CommandExecutor = Callable[[HookSpec, dict[str, Any]], Awaitable[None]]


class ShellHookRunner(HookPort):
    """Run configured shell hooks without letting failures stop the agent."""

    def __init__(
        self,
        specs: tuple[HookSpec, ...],
        *,
        scheduler: Any = None,
        workspace: str | os.PathLike[str] | None = None,
    ):
        self._specs = specs
        self._scheduler = scheduler
        self._workspace = (
            os.path.abspath(os.fspath(workspace))
            if workspace is not None
            else None
        )
        self.cleanup_quiesced = True
        self._executors: dict[str, CommandExecutor] = {"command": self._run_command}

    async def fire(self, event_name: str, payload: dict[str, Any]) -> HookOutcome:
        """Run every hook bound to this event, then always allow (observe-only).

        Phase-1 wiring is observe-only: matched command hooks run for their side
        effects but the returned ``HookOutcome`` is always the default allow. The
        ``HookOutcome.allow`` deny seam is forward-declared (so phase-2 PreToolUse
        blocking needs no signature change) but unbuilt — hooks cannot currently
        block a tool call.
        """
        for spec in match_hooks(self._specs, event_name, payload.get("tool")):
            executor = self._executors.get(spec.action_type)
            if executor is None:
                raise NotImplementedError(f"Hook action type '{spec.action_type}' is not implemented.")
            await executor(spec, payload)
        return HookOutcome()

    async def _run_command(self, spec: HookSpec, payload: dict[str, Any]) -> None:
        command = spec.command.rstrip()
        if command.endswith("&") and not command.endswith("&&"):
            logger.warning("background hook commands are unsupported: %s", spec.command)
            return
        try:
            timeout = float(spec.timeout)
        except (TypeError, ValueError):
            logger.warning("hook command has invalid timeout %r: %s", spec.timeout, spec.command)
            return
        if not math.isfinite(timeout) or timeout <= 0:
            logger.warning("hook command has invalid timeout %r: %s", spec.timeout, spec.command)
            return
        try:
            stdin_bytes = json.dumps(payload).encode()
        except (TypeError, ValueError) as exc:
            logger.warning("hook payload could not be serialized (%s): %s", spec.command, exc)
            return
        environment = {
            **os.environ,
            "OPENCOLLAB_HOOK_EVENT": str(payload.get("hook_event_name", "")),
            "OPENCOLLAB_TOOL": str(payload.get("tool", "")),
            "OPENCOLLAB_AID": str(payload.get("aid", "")),
        }
        try:
            result = await run_process(
                spec.command,
                shell=True,
                timeout=timeout,
                input_bytes=stdin_bytes,
                env=environment,
                cwd=self._workspace,
            )
        except asyncio.TimeoutError:
            logger.warning("hook command timed out after %.2fs: %s", timeout, spec.command)
            return
        except ProcessCleanupError as exc:
            self.cleanup_quiesced = False
            logger.warning("hook command cleanup failed (%s): %s", spec.command, exc)
            raise
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as exc:
            logger.warning("hook command failed to start (%s): %s", spec.command, exc)
            return
        if result.returncode != 0:
            logger.warning("hook command exited %s: %s", result.returncode, spec.command)


__all__ = ["ShellHookRunner"]
