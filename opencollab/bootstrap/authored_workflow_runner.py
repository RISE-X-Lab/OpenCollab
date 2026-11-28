"""Execute member-authored source in the member's Python environment."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import signal
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.application.run_control import RunControl, RunEvent
from opencollab.application.structured_output import StructuredOutputTool
from opencollab.application.submit_findings import SubmitFindingsTool
from opencollab.bootstrap.authored_workflow_bridge import BridgeClient, BridgeLLM
from opencollab.bootstrap.session_factory import build_session
from opencollab.bootstrap.workflow_runtime import run_workflow


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


async def _run(request: dict[str, Any]) -> dict[str, Any]:
    artifacts = Path(request["artifacts"])
    artifacts.mkdir(parents=True, exist_ok=True)
    tokens = 0
    artifact_files = ["workflow.json"]
    _write_json(artifacts / "artifact_files.json", artifact_files)
    bridge = BridgeClient(artifacts, request["bridge"]) if "bridge" in request else None

    def build_child(**kwargs: Any) -> Any:
        forbidden = {
            tool.name for tool in kwargs["agent"].tools
            if not isinstance(tool, (StructuredOutputTool, SubmitFindingsTool))
        } - set(request["allowed_tools"])
        if forbidden:
            raise PermissionError(f"workflow requests tools unavailable to its Team member: {sorted(forbidden)}")
        for tool in kwargs["agent"].tools:
            for name, ceiling in request["tool_limits"].get(tool.name, {}).items():
                if hasattr(tool, name):
                    if name == "require_process_isolation":
                        value = bool(getattr(tool, name) or ceiling)
                    elif name == "allow_create":
                        value = bool(getattr(tool, name) and ceiling)
                    else:
                        value = min(getattr(tool, name), ceiling)
                    setattr(tool, name, value)
        control = kwargs.get("run_control") or RunControl()

        def on_event(event: RunEvent) -> None:
            nonlocal tokens
            if event.type == "usage":
                tokens += event.data["total_tokens"]
                _write_json(artifacts / "usage.json", {"tokens": tokens})
            if control.on_event is not None:
                control.on_event(event)

        kwargs["run_control"] = replace(control, on_event=on_event)
        if bridge is not None:
            kwargs["llm"] = BridgeLLM(bridge, kwargs["env"])
        session = build_session(**kwargs)
        if session.auto_save_path is not None:
            name = Path(session.auto_save_path).name
            artifact_files.extend((name, name + ".journal", name + ".journal.lock"))
            _write_json(artifacts / "artifact_files.json", artifact_files)
        return session

    async def execute() -> Any:
        workspace = Path.cwd().resolve()
        source = (workspace / request["source"]).resolve(strict=True)
        source.relative_to(workspace)
        if source.suffix != ".py":
            raise ValueError("workflow source must be a Python file")
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location(f"_oc_child_{request['id']}", source)
        if spec is None or spec.loader is None:
            raise ValueError("workflow source cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        sys.path.insert(0, str(source.parent))
        spec.loader.exec_module(module)
        flow = getattr(module, request["entry"])
        if not callable(flow) and not callable(getattr(flow, "fn", None)):
            raise TypeError("workflow entry must be callable")
        environment = LocalEnvironment(str(workspace))
        environment.process_isolated = request["process_isolated"]
        environment_deferred = False
        try:
            return await run_workflow(
                flow, request["args"], cfg=request["config"], workspace=str(workspace),
                env=environment, cleanup_environment=True, budget=request["budget"],
                max_steps=request["max_steps"], max_concurrency=request["concurrency"],
                save_dir=str(artifacts), trace=False, run_id=request["id"],
                session_builder=build_child, return_details=True, limit_mode="explicit",
                cleanup_timeout=request.get("cleanup_timeout", 2.0),
                **({"isolation": request["bridge"]["isolation"],
                    "environment_providers": {name: partial(bridge.provider, name)
                                              for name in request["bridge"]["strategies"]}}
                   if bridge is not None else {}),
            )
        except BaseException as failure:
            result = getattr(failure, "runtime_result", None)
            environment_deferred = getattr(result, "environment_cleanup_deferred", False)
            raise
        finally:
            if not environment_deferred:
                await environment.cleanup()

    owner = asyncio.create_task(execute())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, owner.cancel)

    async def watch_stop() -> None:
        while not owner.done():
            if (artifacts / "stop").exists():
                owner.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_stop())
    result: dict[str, Any] = {"tokens": tokens}
    try:
        details = await owner
        result.update(
            status="stopped" if details.stop_reason else "completed",
            output=details.output, reason=details.stop_reason,
            agent_failures=details.agent_failures,
        )
    except asyncio.CancelledError as exc:
        notes = tuple(getattr(exc, "__notes__", ()))
        result.update(status="failed" if notes else "stopped", output=None,
                      reason="cancelled", cleanup_errors=notes)
    except Exception as exc:
        result.update(status="failed", output=None, reason=f"{type(exc).__name__}: {exc}")
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    result["tokens"] = tokens
    result["artifact_files"] = [item.name for item in artifacts.iterdir() if item.is_file() and re.fullmatch(
        r"(?:workflow\.json|\d+(?:_[^/\n]+)?\.json)(?:\.journal(?:\.lock)?)?", item.name,
    )]
    try:
        _write_json(artifacts / "result.json", result)
    except (TypeError, ValueError) as exc:
        result.update(status="failed", output=None, reason=f"workflow output is not JSON serializable: {exc}")
        _write_json(artifacts / "result.json", result)
    return result


def main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    asyncio.run(_run(request))


if __name__ == "__main__":
    main()
