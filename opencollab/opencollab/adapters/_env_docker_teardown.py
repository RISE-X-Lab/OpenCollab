"""Docker environment teardown and failure aggregation."""

from __future__ import annotations

from opencollab.adapters._env_process import _await_owned_operation


class DockerTeardownMixin:
    @staticmethod
    def _raise_teardown_failures(
        operation: str,
        failures: list[tuple[str, BaseException]],
    ) -> None:
        if not failures:
            return
        first_stage, first_failure = failures[0]
        add_note = getattr(first_failure, "add_note", None)
        if callable(add_note):
            add_note(f"Docker {operation} failed during {first_stage}")
            for stage, failure in failures[1:]:
                add_note(f"additional Docker {operation} failure during {stage}: {type(failure).__name__}: {failure}")
        raise first_failure

    async def abort(self) -> None:
        await super().abort()
        failures: list[tuple[str, BaseException]] = []
        if not self._attached:
            try:
                await self._remove_owned_container_or_raise(operation="abort")
            except BaseException as exc:
                failures.append(("container removal", exc))
        backing = self._backing_environment
        if backing is not None:
            try:
                await _await_owned_operation(backing.abort())
            except BaseException as exc:
                failures.append(("backing abort", exc))
            try:
                await _await_owned_operation(backing.cleanup())
            except BaseException as exc:
                failures.append(("backing cleanup", exc))
            else:
                self._backing_environment = None
        self._raise_teardown_failures("abort", failures)

    async def cleanup(self) -> None:
        if self._attached:
            return
        failures: list[tuple[str, BaseException]] = []
        try:
            await self._remove_owned_container_or_raise(operation="cleanup")
        except BaseException as exc:
            failures.append(("container removal", exc))
        backing = self._backing_environment
        if backing is not None:
            try:
                await _await_owned_operation(backing.cleanup())
            except BaseException as exc:
                failures.append(("backing cleanup", exc))
            else:
                self._backing_environment = None
        self._raise_teardown_failures("cleanup", failures)
