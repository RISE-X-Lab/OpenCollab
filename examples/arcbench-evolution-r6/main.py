"""ARC-Bench-compatible command entry for the native r6 workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the ARC-Bench r6 collaboration workflow")
    parser.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"))
    parser.add_argument("--output-dir", default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."))
    parser.add_argument("--type", dest="task_type", default=os.environ.get("ARCBENCH_TASK_TYPE", "web"))
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument("--resume", action="store_true", help="Continue the original run and retained input evidence")
    reuse.add_argument("--fresh", action="store_true", help="Declare a new input application and preserve old reports")
    return parser.parse_args(argv)


async def execute(args):
    from arcbench_r6.model import CompetitionModel
    from arcbench_r6.settings import Settings
    from arcbench_r6.workflow import weave

    from opencollab import OpenCollab

    model = os.environ.get("MODEL") or os.environ.get("OPENCOLLAB_MODEL")
    if not model:
        raise ValueError("Set MODEL or OPENCOLLAB_MODEL to the competition model")
    workspace = Path(args.output_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    run_id = args.run_id
    spent = 0
    if args.resume:
        previous = json.loads((workspace / ".arc/checks/run-state.json").read_text())
        run_id = previous["run_id"]
        args.run_id = run_id
        spent = sum(row["tokens"] for row in previous["sessions"].values())
    client = OpenCollab(
        workspace,
        model=model,
        provider=os.environ.get("OPENCOLLAB_PROVIDER", "openai"),
        api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENCOLLAB_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENCOLLAB_BASE_URL"),
        config={
            "budget": settings.budget,
            "max_output_tokens": settings.max_output_tokens,
            "context_window": settings.context_window,
            "wire_protocol": "chat_completions",
        },
    )
    model_client = client.create_model_client()
    try:
        result = await client.workflow(
            weave,
            {
                "requirements": str(Path(args.requirement_path).resolve()),
                "task_type": args.task_type,
                "resume": args.resume,
                "fresh": args.fresh,
                "settings": asdict(settings),
            },
            agent_profile="single2",
            concurrency=1,
            limit_mode="explicit",
            budget=max(1, settings.budget - spent),
            max_steps=settings.max_steps,
            run_id=run_id,
            llm=CompetitionModel(model_client),
            trace=False,
            cleanup_timeout=settings.cleanup_seconds,
        )
        outcome = result.output if isinstance(result.output, dict) else {}
        print(json.dumps({"execution_status": result.status, "delivery": outcome}, ensure_ascii=False))
        return 0 if result.ok and outcome.get("delivery_ok") is True else 1
    finally:
        await model_client.close()


def _record_failure(args, error):
    from arc_light.reports import write_json
    from arcbench_r6.diagnostics import failure_message
    from arcbench_r6.workflow import _initial_status
    from filelock import FileLock

    workspace = Path(args.output_dir).resolve()
    (workspace / ".arc").mkdir(parents=True, exist_ok=True)
    with FileLock(str(workspace / ".arc/r6-run.lock"), timeout=0):
        _initial_status(workspace, args.run_id)
        write_json(
            workspace / ".arc/checks/outcome.json",
            {
                "run_id": args.run_id,
                "status": "failed",
                "delivery_ok": False,
                "official_score": None,
                "error_type": type(error).__name__,
                "message": failure_message(error),
            },
        )


def main(argv=None):
    args = parse_args(argv)
    args.run_id = "arc-" + uuid.uuid4().hex
    try:
        return asyncio.run(execute(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        try:
            _record_failure(args, error)
        except Exception as reporting_error:
            print(f"Run report unavailable ({type(reporting_error).__name__})", file=sys.stderr)
        print(f"ARC-Bench runner failed ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
