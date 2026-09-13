"""Command-line entry point for the Execution Server."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the OpenCollab Execution Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend", choices=("docker", "os"))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    if args.backend is not None:
        os.environ["EXECSERVER_BACKEND"] = args.backend

    uvicorn.run(
        "opencollab.adapters.execution.service:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
