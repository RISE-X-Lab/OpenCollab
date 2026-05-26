"""ShellHookRunner — runs configured hook actions on a lifecycle event.

Phase 1 ships one executor: ``command`` runs a shell command with the event
payload as JSON on stdin and a few convenience env vars. It is observe-only — a
nonzero exit or a timeout is logged, never raised — so a misbehaving hook can
neither stall nor crash an agent (the EventBus already isolates subscriber
failures; this is belt-and-brace).

The ``prompt`` and ``agent`` executor keys are reserved: ``agent`` is the
team-coordination bridge (a thin wrapper over ``SchedulerPort.spawn``), which is
why the runner accepts a ``scheduler`` handle now even though phase 1 never uses
it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

from opencollab.application.ports import HookPort
from opencollab.domain.hooks import HookOutcome, HookSpec, match_hooks

logger = logging.getLogger(__name__)

CommandExecutor = Callable[[HookSpec, dict[str, Any]], Awaitable[None]]


class ShellHookRunner(HookPort):
    def __init__(self, specs: tuple[HookSpec, ...], *, scheduler: Any = None):
        self._specs = specs
        # Reserved for the phase-2 ``agent`` executor (event-driven spawn).
        self._scheduler = scheduler
        self._executors: dict[str, CommandExecutor] = {
            "command": self._run_command,
        }

    async def fire(self, event_name: str, payload: dict[str, Any]) -> HookOutcome:
        for spec in match_hooks(self._specs, event_name, payload.get("tool")):
            executor = self._executors.get(spec.action_type)
            if executor is None:
                raise NotImplementedError(f"Hook action type '{spec.action_type}' is not implemented.")
            await executor(spec, payload)
        return HookOutcome()

    async def _run_command(self, spec: HookSpec, payload: dict[str, Any]) -> None:
        env = {
            "OPENCOLLAB_HOOK_EVENT": str(payload.get("hook_event_name", "")),
            "OPENCOLLAB_TOOL": str(payload.get("tool", "")),
            "OPENCOLLAB_AID": str(payload.get("aid", "")),
        }
        try:
            proc = await asyncio.create_subprocess_shell(
                spec.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env},
            )
        except Exception as exc:  # spawn failure (bad shell, OS limit)
            logger.warning("hook command failed to start (%s): %s", spec.command, exc)
            return

        stdin_bytes = json.dumps(payload).encode()
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=spec.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("hook command timed out after %.1fs: %s", spec.timeout, spec.command)
            return

        if proc.returncode != 0:
            logger.warning(
                "hook command exited %s: %s\n%s",
                proc.returncode,
                spec.command,
                stderr.decode(errors="replace")[:500],
            )


__all__ = ["ShellHookRunner"]
