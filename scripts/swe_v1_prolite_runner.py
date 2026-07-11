#!/usr/bin/env python3
# ruff: noqa: F401, F403, F405
"""Run one bounded SWE v1 pro-lite slice and publish its evaluation report."""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import types

from scripts import swe_v1_prolite_common as _common
from scripts import swe_v1_prolite_config as _config
from scripts import swe_v1_prolite_controller as _controller
from scripts import swe_v1_prolite_process as _process
from scripts import swe_v1_prolite_report as _report
from scripts.swe_v1_prolite_common import *  # noqa: F403
from scripts.swe_v1_prolite_config import *  # noqa: F403
from scripts.swe_v1_prolite_controller import *  # noqa: F403
from scripts.swe_v1_prolite_process import *  # noqa: F403
from scripts.swe_v1_prolite_report import *  # noqa: F403


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Run validation-council on a SWE-batch-pro-lite slice and evaluate it.")
    )
    parser.add_argument("--host", default=DEFAULT_HOST)  # noqa: F405
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)  # noqa: F405
    parser.add_argument("--remote-runtime-repo", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--base-run-dir", default="")
    parser.add_argument("--start-index", type=int, default=26)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--workflow", default="validation-council-solve")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)  # noqa: F405
    parser.add_argument(
        "--session-prefix",
        default=DEFAULT_SESSION_PREFIX,  # noqa: F405
    )
    parser.add_argument(
        "--image-repository",
        default=DEFAULT_IMAGE_REPOSITORY,  # noqa: F405
    )
    parser.add_argument(
        "--remote-proxy-base-url",
        default=DEFAULT_REMOTE_PROXY_BASE_URL,  # noqa: F405
    )
    parser.add_argument(
        "--local-proxy-base-url",
        default=DEFAULT_LOCAL_PROXY_BASE_URL,  # noqa: F405
    )
    parser.add_argument(
        "--proxy-env-file",
        type=Path,  # noqa: F405
        default=DEFAULT_PROXY_ENV_FILE,  # noqa: F405
    )
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--swe-timeout", type=int, default=14_400)
    parser.add_argument("--task-wall-timeout", type=int, default=15_300)
    parser.add_argument("--eval-timeout", type=int, default=7_200)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--checkpoint-interval", type=int, default=300)
    parser.add_argument("--max-task-starts", type=int, default=1)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON)  # noqa: F405
    parser.add_argument(  # noqa: F405
        "--markdown-output",
        type=Path,  # noqa: F405
        default=DEFAULT_REPORT_MD,  # noqa: F405
    )
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.start_index < 1:
        parser.error("--start-index must be >= 1")
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.limit > MAX_TASKS_PER_RUN:  # noqa: F405
        parser.error(f"--limit must be <= {MAX_TASKS_PER_RUN}")  # noqa: F405
    if args.max_task_starts < 0:
        parser.error("--max-task-starts must be >= 0")
    positive_values = {
        "--budget": args.budget,
        "--max-steps": args.max_steps,
        "--swe-timeout": args.swe_timeout,
        "--task-wall-timeout": args.task_wall_timeout,
        "--eval-timeout": args.eval_timeout,
        "--llm-timeout": args.llm_timeout,
        "--total-timeout": args.total_timeout,
    }
    for option, value in positive_values.items():
        if value <= 0:
            parser.error(f"{option} must be > 0")
    if args.checkpoint_interval < 0:
        parser.error("--checkpoint-interval must be >= 0")
    if args.run_id:
        try:
            args.run_id = validate_run_id(args.run_id)  # noqa: F405
        except ValueError as exc:
            parser.error(str(exc))

    required = {
        "--host or OPENCOLLAB_SWE_HOST": args.host,
        "--remote-root or OPENCOLLAB_SWE_REMOTE_ROOT": args.remote_root,
        "--model-name or OPENCOLLAB_SWE_MODEL_NAME": args.model_name,
        "--session-prefix or OPENCOLLAB_SWE_SESSION_PREFIX": args.session_prefix,
        "--image-repository or OPENCOLLAB_SWE_IMAGE_REPOSITORY": args.image_repository,
        ("--remote-proxy-base-url or OPENCOLLAB_REMOTE_PROXY_BASE_URL"): args.remote_proxy_base_url,
    }
    for option, value in required.items():
        if not str(value or "").strip():
            parser.error(f"{option} is required")
    if not args.no_ensure_remote_proxy and not str(args.local_proxy_base_url or "").strip():
        parser.error(
            "--local-proxy-base-url or OPENCOLLAB_LOCAL_PROXY_BASE_URL is required when remote proxy setup is enabled"
        )

    configure_run_paths(args)  # noqa: F405
    try:
        summary = run_remote(args)  # noqa: F405
    except KeyboardInterrupt:
        return 130
    write_local_report(  # noqa: F405
        summary,
        args.json_output,
        args.markdown_output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"done", "dry_run"} else 1


_COMPATIBILITY_MODULES = (
    _common,
    _process,
    _config,
    _controller,
    _report,
)


class _CompatibilityModule(types.ModuleType):
    """Propagate legacy module monkeypatches to the extracted implementation."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule


if __name__ == "__main__":
    raise SystemExit(main())
