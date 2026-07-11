"""Composable entry point and compatibility facade for the V1 remote runner."""

from __future__ import annotations

import functools
import inspect
import json
import sys
import types
from collections.abc import MutableMapping
from contextlib import contextmanager
from typing import Any

from opencollab.harness import (
    swe_v1_remote_artifacts,
    swe_v1_remote_commands,
    swe_v1_remote_core,
    swe_v1_remote_evaluation,
    swe_v1_remote_generation,
    swe_v1_remote_records,
    swe_v1_remote_state,
)

_RUNTIME_MODULES = (
    swe_v1_remote_core,
    swe_v1_remote_records,
    swe_v1_remote_commands,
    swe_v1_remote_generation,
    swe_v1_remote_artifacts,
    swe_v1_remote_evaluation,
)


def _clone_function(
    function: types.FunctionType,
    namespace: MutableMapping[str, Any],
) -> types.FunctionType:
    wrapped = getattr(function, "__wrapped__", None)
    if inspect.isfunction(wrapped):
        cloned_wrapped = types.FunctionType(
            wrapped.__code__,
            namespace,
            wrapped.__name__,
            wrapped.__defaults__,
            wrapped.__closure__,
        )
        cloned_wrapped.__kwdefaults__ = wrapped.__kwdefaults__
        cloned = contextmanager(cloned_wrapped)
        functools.update_wrapper(cloned, function)
        return cloned
    cloned = types.FunctionType(
        function.__code__,
        namespace,
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    cloned.__kwdefaults__ = function.__kwdefaults__
    cloned.__annotations__ = dict(getattr(function, "__annotations__", {}))
    functools.update_wrapper(cloned, function)
    return cloned


def install_compat(
    namespace: MutableMapping[str, Any],
    config: dict[str, Any] | None = None,
) -> None:
    """Install a namespace-compatible runner backed by importable modules."""
    if config is None:
        config = json.loads(sys.stdin.read())
    if not isinstance(config, dict):
        raise ValueError("remote runner configuration must be a JSON object")
    swe_v1_remote_state.configure(config)
    state_values = {name: getattr(swe_v1_remote_state, name) for name in swe_v1_remote_state.state_names()}
    for module in _RUNTIME_MODULES:
        for name, value in state_values.items():
            setattr(module, name, value)
        for name, value in vars(module).items():
            if not name.startswith("__"):
                namespace[name] = value
    namespace.update(state_values)
    for module in _RUNTIME_MODULES:
        for name, value in vars(module).items():
            if inspect.isfunction(value) and value.__module__ == module.__name__:
                namespace[name] = _clone_function(value, namespace)


install_into = install_compat


def run_from_stdin() -> int:
    namespace = globals()
    install_compat(namespace)
    namespace["initialize_runner_ownership"]()
    return int(namespace["main"]())


if __name__ == "__main__":
    raise SystemExit(run_from_stdin())


__all__ = ["install_compat", "install_into", "run_from_stdin"]
