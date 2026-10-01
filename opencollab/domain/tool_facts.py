"""Per-call execution observations, separate from the text shown to a model."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ToolFacts:
    observed: bool = False
    exit_code: int | None = None
    timed_out: bool | None = None
    write_completed: bool | None = None
    content_changed: bool | None = None
    changed_paths: tuple[str, ...] = ()


class ToolFactsCollector:
    """Owned by one invocation; observations after close cannot affect its result."""

    def __init__(self) -> None:
        self._facts = ToolFacts()
        self._closed = False

    def record_execution(self, *, exit_code: int | None, timed_out: bool | None) -> None:
        if self._closed:
            return
        self._facts = replace(
            self._facts,
            observed=True,
            exit_code=exit_code if type(exit_code) is int else None,
            timed_out=timed_out if type(timed_out) is bool else None,
        )

    def record_write(self, *, completed: bool, changed: bool | None, path: str | None = None) -> None:
        if self._closed:
            return
        previous = self._facts
        changed = changed if type(changed) is bool else None
        paths = previous.changed_paths
        if changed is True and isinstance(path, str) and path not in paths:
            paths = (*paths, path)
        if previous.content_changed is True:
            changed = True
        self._facts = replace(
            previous,
            observed=True,
            write_completed=completed,
            content_changed=changed,
            changed_paths=paths,
        )

    def record_timeout(self) -> None:
        if not self._closed:
            self._facts = replace(self._facts, observed=True, timed_out=True)

    def close(self) -> ToolFacts:
        self._closed = True
        return self._facts
