#!/usr/bin/env python3
"""Unified SWE evaluation entrypoint."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "opencollab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "opencollab"))

from opencollab.adapters.llm.types import model_context_window  # noqa: E402
from opencollab.harness.solver_backend import (  # noqa: E402
    DEFAULT_WORKFLOW_SOLVERS,
    workflow_solver_spec,
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _normalize_indices(args: argparse.Namespace) -> list[str]:
    if args.indices:
        return [part.strip() for part in args.indices.split(",") if part.strip()]
    if args.start_index is None or args.end_index is None:
        raise SystemExit("pass --indices or both --start-index and --end-index")
    if args.end_index < args.start_index:
        raise SystemExit("--end-index must be >= --start-index")
    return [str(index) for index in range(args.start_index, args.end_index + 1)]


def _has_option(arguments: list[str], option: str) -> bool:
    return any(argument == option or argument.startswith(option + "=") for argument in arguments)


def _option_value(arguments: list[str], option: str, default: str) -> str:
    for index, argument in enumerate(arguments):
        if argument.startswith(option + "="):
            return argument.split("=", 1)[1]
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1]
    return default


def _required_runtime_options(
    *,
    solver_name: str,
    requirements: tuple[tuple[str, str], ...],
    arguments: list[str],
) -> tuple[list[str], dict[str, str]]:
    delegated: list[str] = []
    values: dict[str, str] = {}
    for option, environment_key in requirements:
        cli_value = _option_value(arguments, option, "").strip()
        if cli_value:
            values[option] = cli_value
            continue
        if _has_option(arguments, option):
            raise SystemExit(f"{solver_name} requires a non-empty {option}")
        environment_value = os.environ.get(environment_key, "").strip()
        if not environment_value:
            raise SystemExit(
                f"{solver_name} requires {option} or {environment_key}"
            )
        delegated += [option, environment_value]
        values[option] = environment_value
    return delegated, values


def _without_launch_options(arguments: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in {"--detach", "--no-persistent-proxy"}:
            continue
        if argument == "--launchd-label":
            skip_next = True
            continue
        if argument.startswith("--launchd-label="):
            continue
        cleaned.append(argument)
    return cleaned


def _safe_label(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]+", ".", value.strip()).strip(".")
    return normalized or "run"


def _launchd_plist(
    *,
    label: str,
    program_arguments: list[str],
    stdout_path: Path,
    stderr_path: Path,
    keep_alive: bool = False,
) -> dict:
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(REPO_ROOT),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
    }


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)


def _launchctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def _remote_proxy_healthy(*, ssh_command: str, host: str, base_url: str) -> bool:
    probe = (
        "import sys,urllib.request;"
        "urllib.request.urlopen(sys.argv[1],timeout=5).read()"
    )
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        "python3 -c " + repr(probe) + " " + repr(base_url.rstrip("/") + "/healthz"),
    ]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _ensure_proxy_agent(*, output_dir: Path, remaining: list[str]) -> dict:
    host = _option_value(
        remaining,
        "--host",
        os.environ.get("OPENCOLLAB_SWE_HOST", ""),
    ).strip()
    if not host:
        raise RuntimeError("persistent proxy requires --host or OPENCOLLAB_SWE_HOST")
    ssh_command = _option_value(remaining, "--ssh-command", "/usr/bin/ssh")
    local_url = _option_value(remaining, "--local-proxy-base-url", "http://127.0.0.1:8878")
    remote_url = _option_value(remaining, "--remote-proxy-base-url", "http://127.0.0.1:18788")
    local = urllib.parse.urlparse(local_url)
    remote = urllib.parse.urlparse(remote_url)
    if local.hostname not in {"127.0.0.1", "localhost"} or remote.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError("persistent proxy URLs must use localhost")
    local_port = int(local.port or 8878)
    remote_port = int(remote.port or 18788)
    label = f"com.opencollab.proxy.{_safe_label(host)}.{remote_port}"
    target = f"gui/{os.getuid()}/{label}"
    if _remote_proxy_healthy(ssh_command=ssh_command, host=host, base_url=remote_url):
        return {"status": "already_healthy", "label": label, "remote_url": remote_url}

    plist_path = output_dir / f"{label}.plist"
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    payload = _launchd_plist(
        label=label,
        program_arguments=[
            *shlex.split(ssh_command),
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
            host,
        ],
        stdout_path=output_dir / "proxy.launch.stdout.log",
        stderr_path=output_dir / "proxy.launch.stderr.log",
        keep_alive=True,
    )
    payload.pop("WorkingDirectory", None)
    _write_plist(plist_path, payload)
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    if _launchctl("print", target).returncode == 0:
        _launchctl("bootout", target, check=True)
    shutil.copy2(plist_path, installed_path)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(installed_path), check=True)
    for _ in range(12):
        if _remote_proxy_healthy(ssh_command=ssh_command, host=host, base_url=remote_url):
            return {"status": "started", "label": label, "remote_url": remote_url}
        time.sleep(0.5)
    raise RuntimeError(f"persistent proxy failed health check: {remote_url}")


def _launch_detached(args: argparse.Namespace, raw_arguments: list[str], remaining: list[str]) -> int:
    run_id = args.run_id or (
        f"swe_{_safe_label(args.solver)}_{_safe_label(','.join(_normalize_indices(args)))}_"
        + time.strftime("%Y%m%d_%H%M%S")
    )
    output_dir = (args.output_dir or REPO_ROOT / "docs" / "monitoring" / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    child_arguments = _without_launch_options(raw_arguments)
    if not args.run_id:
        child_arguments += ["--run-id", run_id]
    if args.output_dir is None:
        child_arguments += ["--output-dir", str(output_dir)]
    proxy = (
        {"status": "disabled"}
        if args.no_persistent_proxy
        else _ensure_proxy_agent(output_dir=output_dir, remaining=remaining)
    )
    label = args.launchd_label or f"com.opencollab.eval.{_safe_label(run_id)}"
    target = f"gui/{os.getuid()}/{label}"
    existing = _launchctl("print", target)
    if existing.returncode == 0 and "state = running" in existing.stdout:
        print(
            json.dumps(
                {
                    "status": "already_running",
                    "run_id": run_id,
                    "label": label,
                    "output_dir": str(output_dir),
                    "proxy": proxy,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if existing.returncode == 0:
        _launchctl("bootout", target, check=True)

    plist_path = output_dir / f"{label}.plist"
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    payload = _launchd_plist(
        label=label,
        program_arguments=[sys.executable, str(Path(__file__).resolve()), *child_arguments],
        stdout_path=output_dir / "runner.launch.stdout.log",
        stderr_path=output_dir / "runner.launch.stderr.log",
    )
    _write_plist(plist_path, payload)
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plist_path, installed_path)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(installed_path), check=True)
    status = {
        "status": "started",
        "run_id": run_id,
        "label": label,
        "output_dir": str(output_dir),
        "plist": str(plist_path),
        "proxy": proxy,
    }
    (output_dir / "launch_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def _run_parallel_runner(args: argparse.Namespace, remaining: list[str]) -> int:
    spec = workflow_solver_spec(args.solver)
    historical_eval_options = [
        option
        for option in ("--eval-only", "--parent-output-dir")
        if _has_option(remaining, option)
    ]
    if historical_eval_options:
        raise SystemExit(
            "historical eval-only is a single-task maintenance API; "
            "use scripts/swe_v1_prolite_runner.py directly"
        )
    if _has_option(remaining, "--workflow"):
        raise SystemExit("--workflow is selected by --solver and cannot be overridden")
    runner = _load_module(REPO_ROOT / "scripts" / "swe_g11_parallel_runner.py", "swe_g11_parallel_runner")
    delegated = [str(REPO_ROOT / "scripts" / "swe_g11_parallel_runner.py")]
    if args.indices:
        delegated += ["--indices", args.indices]
    else:
        delegated += ["--start-index", str(args.start_index), "--end-index", str(args.end_index)]
    delegated += ["--max-workers", str(args.workers), "--workflow", spec.workflow_name]
    if not _has_option(remaining, "--max-task-starts"):
        delegated += ["--max-task-starts", str(spec.max_attempts)]
    if spec.default_budget_tokens is not None and not _has_option(remaining, "--budget"):
        delegated += ["--budget", str(spec.default_budget_tokens)]
    required_arguments, runtime_values = _required_runtime_options(
        solver_name=spec.name,
        requirements=spec.required_runtime_options,
        arguments=remaining,
    )
    delegated += required_arguments
    model = runtime_values.get("--llm-model", "")
    if model and not _has_option(remaining, "--context-window"):
        context_window = model_context_window(model)
        if context_window is not None:
            delegated += ["--context-window", str(context_window)]
    for key, option in (
        ("temperature", "--temperature"),
        ("top_p", "--top-p"),
        ("max_output_tokens", "--max-output-tokens"),
    ):
        value = spec.config_overrides.get(key)
        if value is not None and not _has_option(remaining, option):
            delegated += [option, str(value)]
    for key, value in spec.args.items():
        option = "--" + key.replace("_", "-")
        if not _has_option(remaining, option):
            delegated += [option, str(value)]
    if args.run_id:
        delegated += ["--run-id", args.run_id]
    if args.output_dir:
        delegated += ["--output-dir", str(args.output_dir)]
    delegated += remaining
    sys.argv = delegated
    return int(runner.main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SWE evaluation with a selected solver.")
    parser.add_argument("--dataset", default="swe-batch-pro-lite")
    parser.add_argument("--indices")
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--solver", default="g11", choices=tuple(DEFAULT_WORKFLOW_SOLVERS))
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--launchd-label")
    parser.add_argument("--no-persistent-proxy", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    args, remaining = parser.parse_known_args(raw_arguments)
    if args.dataset != "swe-batch-pro-lite":
        raise SystemExit(f"unsupported dataset: {args.dataset}")
    _normalize_indices(args)
    if args.detach:
        return _launch_detached(args, raw_arguments, remaining)
    return _run_parallel_runner(args, remaining)


if __name__ == "__main__":
    raise SystemExit(main())
