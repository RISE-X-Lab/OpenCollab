#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy

import docker


_original_from_env = docker.from_env


def _from_env_with_timeout(*args, **kwargs):
    if "timeout" not in kwargs:
        timeout_value = os.environ.get("OPENCOLLAB_DOCKER_API_TIMEOUT")
        if timeout_value is None:
            timeout_value = os.environ.get("DOCKER_CLIENT_TIMEOUT")
        if timeout_value:
            try:
                kwargs["timeout"] = int(timeout_value)
            except ValueError as exc:
                raise ValueError(
                    "OPENCOLLAB_DOCKER_API_TIMEOUT must be an integer number of seconds"
                ) from exc
    return _original_from_env(*args, **kwargs)


docker.from_env = _from_env_with_timeout

runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
