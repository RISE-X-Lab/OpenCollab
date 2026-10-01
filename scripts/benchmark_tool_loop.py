#!/usr/bin/env python3
"""Measure tool-loop handling in explicit source trees using local workspaces."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compare", type=Path, help="Second source tree to measure in alternating subprocesses.")
    parser.add_argument("--iterations", type=int, default=200, help="Samples per pure tool-processing case.")
    parser.add_argument("--tool-iterations", type=int, default=10, help="Samples per real local-tool case.")
    parser.add_argument("--max-write-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--repeats", type=int, default=3, help="Independent subprocess runs per source tree.")
    parser.add_argument("--output", type=Path, help="Write measurements here instead of stdout.")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser


def _percentile(values, quantile):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


async def _measure(name, setup, iterations, *, tool_count=1, read_module=None):
    await setup(warmup=True)
    wall_samples, cpu_samples = [], []
    executions, command_executions, blocks = [], [], []
    for _ in range(iterations):
        operation = await setup(warmup=False)
        cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
        result, command_count = await operation()
        wall_samples.append((time.perf_counter_ns() - wall_start) / 1_000)
        cpu_samples.append((time.process_time_ns() - cpu_start) / 1_000)
        executions.append(len(result.evidence_signals))
        command_executions.append(command_count)
        blocks.append(len(result.loop_detections))
    operation = await setup(warmup=False)
    tracemalloc.start()
    try:
        await operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    target_read_bytes = None
    if read_module is not None:
        operation = await setup(warmup=False)
        original_os = read_module.os

        class ReadCountingOS:
            read_bytes = 0

            def __getattr__(self, name):
                return getattr(original_os, name)

            def read(self, fd, size):
                content = original_os.read(fd, size)
                self.read_bytes += len(content)
                return content

        counter = ReadCountingOS()
        read_module.os = counter
        try:
            await operation()
            target_read_bytes = counter.read_bytes
        finally:
            read_module.os = original_os
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "scenario": name,
        "samples": iterations,
        "batch_tool_count": tool_count,
        "median_batch_us": statistics.median(wall_samples),
        "p95_batch_us": _percentile(wall_samples, 0.95),
        "median_per_call_us": statistics.median(wall_samples) / tool_count,
        "median_cpu_us": statistics.median(cpu_samples),
        "peak_processing_allocation_bytes": peak,
        "process_peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
        "dispatched_calls": executions,
        "subprocess_exec_calls": command_executions,
        "observed_target_read_bytes": target_read_bytes,
        "loop_blocks": blocks,
    }


async def _run_child(args):
    source = args.source.resolve()
    if not (source / "opencollab").is_dir():
        raise ValueError("--source must contain the opencollab package")
    sys.path.insert(0, str(source))
    from opencollab.adapters import safe_anchored_files
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.adapters.storage import SessionStore
    from opencollab.adapters.tools.bash import BashTool
    from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
    from opencollab.bootstrap import build_session
    from opencollab.domain.session import SessionState
    from tests.support.session_characterization_test_support import FakeAgent, FakeLLMClient
    from tests.support.tool_execution_test_support import build_use_case, tool_call

    class ProbeTool:
        name = "probe"

        def __init__(self, fact_aware):
            self.calls = 0
            self.fact_aware = fact_aware
            self.loop_workspace_observer = fact_aware

        async def execute_with_runtime(self, params, runtime):
            self.calls += 1
            observations = getattr(runtime, "observations", None)
            if self.fact_aware and observations is not None:
                observations.record_execution(exit_code=0, timed_out=False)
            return f"observation {params['index']}"

    class CountingEnvironment(LocalEnvironment):
        def __init__(self, workspace):
            super().__init__(str(workspace))
            self.exec_count = 0

        async def exec_cmd(self, cmd, timeout=120.0):
            self.exec_count += 1
            return await super().exec_cmd(cmd, timeout=timeout)

    class CountingStore(SessionStore):
        def __init__(self):
            super().__init__()
            self.writes = 0
            self.write_ns = 0

        def _record(self, method, *values, **kwargs):
            started = time.perf_counter_ns()
            try:
                return method(*values, **kwargs)
            finally:
                self.writes += 1
                self.write_ns += time.perf_counter_ns() - started

        def append_snapshot_delta(self, *values, **kwargs):
            return self._record(super().append_snapshot_delta, *values, **kwargs)

        def save(self, *values, **kwargs):
            return self._record(super().save, *values, **kwargs)

        def checkpoint_snapshot(self, *values, **kwargs):
            return self._record(super().checkpoint_snapshot, *values, **kwargs)

    results = []
    for fact_aware in (False, True):
        for window_size, batch_size in ((0, 1), (200, 1), (200, 32)):
            tool = ProbeTool(fact_aware)
            template = SessionState(messages=[])
            priming, _ = build_use_case(state=template, agent=FakeAgent(tools=[tool]))
            for index in range(window_size):
                result = await priming.process([tool_call("probe", json.dumps({"index": index}), f"seed-{index}")])
                result.apply_to(template)
            calls = [tool_call("probe", json.dumps({"index": 1000 + index}), str(index)) for index in range(batch_size)]

            async def setup_mock(*, warmup):
                state = copy.deepcopy(template)
                state.advance_step()
                executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[tool]))

                async def invoke():
                    result = await executor.process(calls)
                    result.apply_to(state)
                    return result, None

                if warmup:
                    await invoke()
                return invoke

            flavor = "facts" if fact_aware else "legacy"
            results.append(await _measure(
                f"mock-{flavor}-window-{window_size}-batch-{batch_size}",
                setup_mock, args.iterations, tool_count=batch_size,
            ))

    with tempfile.TemporaryDirectory(prefix="opencollab-loop-benchmark-") as directory:
        workspace = Path(directory)
        environment = CountingEnvironment(workspace)
        native_tools = [BashTool(), FileReadTool(), FileWriteTool()]
        try:
            (workspace / "read.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            real_cases = [
                ("local-bash", "bash", {"command": "printf 'ok\\n'"}),
                ("local-file-read", "file_read", {"path": "read.txt", "offset": 1, "limit": 3}),
            ]
            for size in (0, 4096, args.max_write_bytes):
                filename = f"write-{size}.txt"
                content = "x" * size
                (workspace / filename).write_text(content, encoding="utf-8")
                real_cases.append((f"local-noop-write-{size}", "file_write", {
                    "path": filename, "mode": "create", "content": content, "overwrite": True,
                }))
            for name, tool_name, params in real_cases:
                call = tool_call(tool_name, json.dumps(params), name)

                async def setup_local(*, warmup):
                    state = SessionState(messages=[])
                    state.advance_step()
                    executor, _ = build_use_case(
                        state=state, agent=FakeAgent(tools=native_tools), environment=environment,
                    )

                    async def invoke():
                        before = environment.exec_count
                        result = await executor.process([call])
                        result.apply_to(state)
                        if result.loop_detections or result.messages_to_append[0]["content"].startswith("Error"):
                            raise RuntimeError(f"Unexpected failure in {name}")
                        return result, environment.exec_count - before

                    if warmup:
                        await invoke()
                    return invoke

                read_module = safe_anchored_files if name.startswith("local-noop-write-") else None
                measurement = await _measure(name, setup_local, args.tool_iterations, read_module=read_module)
                measurement["write_payload_bytes"] = len(params.get("content", "").encode("utf-8"))
                results.append(measurement)

            stores, snapshot_paths = [], []
            session_serial = 0

            async def setup_revalidation(*, warmup):
                nonlocal session_serial
                session_serial += 1
                path = workspace / f"session-{session_serial}.json"
                store = CountingStore()
                session = build_session(
                    agent=FakeAgent(tools=native_tools), llm=FakeLLMClient(), env=environment,
                    store=store, auto_save_path=str(path),
                )
                (workspace / "observed.txt").write_text("FAIL\n", encoding="utf-8")

                async def apply(calls):
                    session.state.advance_step()
                    for call in calls:
                        call.setdefault("type", "function")
                    session.state.append_message({"role": "assistant", "content": None, "tool_calls": calls})
                    result = await session.tool_execution.process(calls)
                    result.apply_to(session.state)
                    return result

                for index in range(2):
                    await apply([tool_call("bash", '{"command":"cat observed.txt"}', f"before-{index}")])
                await apply([tool_call("file_write", json.dumps({
                    "path": "observed.txt", "mode": "create", "content": "PASS\n", "overwrite": True,
                }), "edit")])
                session.save(str(path))
                writes_before, save_time_before = store.writes, store.write_ns

                async def invoke():
                    before = environment.exec_count
                    result = await apply([tool_call("bash", '{"command":"cat observed.txt"}', "retest")])
                    stores.append((store, writes_before, save_time_before))
                    snapshot_paths.append(path)
                    await session.aclose()
                    return result, environment.exec_count - before

                if warmup:
                    await invoke()
                return invoke

            revalidation = await _measure("local-revalidation-with-save", setup_revalidation, args.tool_iterations)
            measured_stores = stores[1:-1]
            measured_paths = snapshot_paths[1:-1]
            revalidation["extra_save_calls"] = [store.writes - prior for store, prior, _ in measured_stores]
            revalidation["extra_save_us"] = [(store.write_ns - prior) / 1_000 for store, _, prior in measured_stores]
            revalidation["snapshot_and_journal_bytes"] = [
                sum(p.stat().st_size for p in (path, Path(f"{path}.journal")) if p.exists())
                for path in measured_paths
            ]
            results.append(revalidation)
        finally:
            await environment.cleanup()
    return {
        "source": str(source),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "model_calls": 0,
        "scenarios": results,
    }


def _run_parent(args):
    sources = [args.source] + ([args.compare] if args.compare is not None else [])
    runs = []
    script = Path(__file__).resolve()
    for repeat in range(args.repeats):
        order = sources if repeat % 2 == 0 else list(reversed(sources))
        for source in order:
            command = [
                sys.executable, str(script), "--child", "--source", str(source.resolve()),
                "--iterations", str(args.iterations), "--tool-iterations", str(args.tool_iterations),
                "--max-write-bytes", str(args.max_write_bytes),
            ]
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
            run = json.loads(completed.stdout)
            run["repeat"] = repeat
            runs.append(run)
    return {"measurement_version": 1, "runs": runs}


def main():
    args = _parser().parse_args()
    if min(args.iterations, args.tool_iterations, args.repeats) < 1 or args.max_write_bytes < 0:
        raise SystemExit("Sample counts must be positive and write size must be non-negative.")
    result = asyncio.run(_run_child(args)) if args.child else _run_parent(args)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(f"Measurements saved to {args.output}")


if __name__ == "__main__":
    main()
