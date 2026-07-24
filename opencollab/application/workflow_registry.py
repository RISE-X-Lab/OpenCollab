"""Workflow registry and the ``@workflow`` decorator.

A workflow is a plain async function ``async def fn(ctx, args) -> Any``. The
``@workflow`` decorator attaches a frozen :class:`WorkflowSpec` (name,
description, phases, fn) to the function as ``__workflow_spec__`` and returns the
function unchanged, so the function stays directly callable. A :class:`Registry`
collects specs by name and rejects duplicate registrations.

Pure application layer: stdlib only (no domain, no adapters).
"""

from __future__ import annotations

import inspect
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
    fn: WorkflowFn | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    phases: Sequence[str] | None = None,
) -> Callable[[WorkflowFn], WorkflowFn] | WorkflowFn:
    """Tag an async function as a workflow, with optional metadata.

    Attaches a frozen :class:`WorkflowSpec` as ``fn.__workflow_spec__`` and
    returns the function unchanged so it remains directly callable. Both
    ``@workflow`` and ``@workflow(name=..., description=...)`` are supported.
    """

    def decorator(target: WorkflowFn) -> WorkflowFn:
        doc = inspect.getdoc(target)
        spec = WorkflowSpec(
            name=name or target.__name__,
            description=description or (doc.splitlines()[0] if doc else target.__name__),
            fn=target,
            phases=tuple(phases or ()),
        )
        target.__workflow_spec__ = spec  # type: ignore[attr-defined]
        return target

    return decorator(fn) if fn is not None else decorator


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
