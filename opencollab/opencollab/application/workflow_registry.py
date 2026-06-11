"""Workflow registry and the ``@workflow`` decorator.

A workflow is a plain async function ``async def fn(ctx, args) -> Any``. The
``@workflow`` decorator attaches a frozen :class:`WorkflowSpec` (name,
description, phases, fn) to the function as ``__workflow_spec__`` and returns the
function unchanged, so the function stays directly callable. A :class:`Registry`
collects specs by name and rejects duplicate registrations.

Pure application layer: stdlib only (no domain, no adapters).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# A workflow function: ``async def fn(ctx, args) -> Any``.
WorkflowFn = Callable[[Any, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class WorkflowSpec:
    """Immutable metadata describing a registered workflow.

    ``phases`` is the declared list of phase titles (purely descriptive — the
    engine does not enforce them). ``fn`` is the async workflow function.
    """

    name: str
    description: str
    fn: WorkflowFn
    phases: tuple[str, ...] = field(default_factory=tuple)


def workflow(
    *,
    name: str,
    description: str,
    phases: Sequence[str] | None = None,
) -> Callable[[WorkflowFn], WorkflowFn]:
    """Decorator that tags an async function as a workflow.

    Attaches a frozen :class:`WorkflowSpec` as ``fn.__workflow_spec__`` and
    returns the function unchanged so it remains directly callable.
    """

    def decorator(fn: WorkflowFn) -> WorkflowFn:
        spec = WorkflowSpec(
            name=name,
            description=description,
            fn=fn,
            phases=tuple(phases or ()),
        )
        fn.__workflow_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator


class Registry:
    """A name -> :class:`WorkflowSpec` registry with duplicate-name rejection."""

    def __init__(self) -> None:
        self._specs: dict[str, WorkflowSpec] = {}

    def register(self, spec: WorkflowSpec) -> None:
        """Register ``spec``; raise ``ValueError`` if its name is already taken."""
        if spec.name in self._specs:
            raise ValueError(f"workflow name already registered: {spec.name!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> WorkflowSpec:
        """Return the spec registered under ``name`` or raise ``KeyError``."""
        return self._specs[name]

    def list_specs(self) -> list[WorkflowSpec]:
        """All registered specs, sorted by name."""
        return [self._specs[n] for n in sorted(self._specs)]


__all__ = ["Registry", "WorkflowFn", "WorkflowSpec", "workflow"]
