#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import runpy

import docker

_original_from_env = docker.from_env


def positive_timeout_seconds(value: object, *, name: str) -> float:
    raw = str(value).strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {raw!r}")
    return timeout


def docker_api_timeout_from_env() -> float | None:
    for name in ("OPENCOLLAB_DOCKER_API_TIMEOUT", "DOCKER_CLIENT_TIMEOUT"):
        value = os.environ.get(name)
        if value is not None and value.strip():
            return positive_timeout_seconds(value, name=name)
    return None


def _from_env_with_timeout(*args, **kwargs):
    if "timeout" not in kwargs:
        timeout_value = docker_api_timeout_from_env()
        if timeout_value is not None:
            kwargs["timeout"] = timeout_value
    return _original_from_env(*args, **kwargs)


def main() -> None:
    docker.from_env = _from_env_with_timeout
    runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")


if __name__ == "__main__":
    main()
