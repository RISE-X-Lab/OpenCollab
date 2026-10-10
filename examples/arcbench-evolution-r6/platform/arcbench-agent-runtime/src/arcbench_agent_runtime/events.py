from __future__ import annotations

import time
from typing import Any

from .context import RuntimePaths
from .jsonio import append_jsonl


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class EventClient:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._requirement_state_writer = None

    def set_requirement_state_writer(self, writer) -> None:
        self._requirement_state_writer = writer

    def _emit_requirement_state(self, node_id: str, phase: str, status: str, message: str | None = None) -> None:
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            return
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "requirement_state",
                "node_id": normalized_node_id,
                "phase": str(phase or "").strip(),
                "status": str(status or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )
        if self._requirement_state_writer is not None:
            state = {
                ("design", "running"): "DESIGNING",
                ("design", "completed"): "DESIGNED",
                ("design", "failed"): "FAILED",
                ("implement", "running"): "IMPLEMENTING",
                ("implement", "completed"): "IMPLEMENTED",
                ("implement", "failed"): "FAILED",
                ("test", "passed"): "PASSED",
                ("test", "failed"): "FAILED",
            }.get((str(phase or "").strip(), str(status or "").strip()))
            if state:
                self._requirement_state_writer(normalized_node_id, state, str(phase or "").strip())

    def mark_design_started(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "running", message)

    def mark_design_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "completed", message)

    def mark_design_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "design", "failed", message)

    def mark_implementation_started(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "running", message)

    def mark_implementation_done(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "completed", message)

    def mark_implementation_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "implement", "failed", message)

    def mark_test_passed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "passed", message)

    def mark_test_failed(self, node_id: str, message: str | None = None) -> None:
        self._emit_requirement_state(node_id, "test", "failed", message)

    def _emit_runner_state(self, state: str, message: str | None = None) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "runner_state",
                "state": str(state or "").strip(),
                "timestamp": utc_timestamp(),
                "message": message,
            },
        )

    def mark_run_started(self, message: str | None = None) -> None:
        self._emit_runner_state("running", message)

    def mark_run_completed(self, message: str | None = None) -> None:
        self._emit_runner_state("completed", message)

    def mark_run_failed(self, message: str | None = None) -> None:
        self._emit_runner_state("failed", message)

    def mark_run_paused(self, message: str | None = None) -> None:
        self._emit_runner_state("paused", message)

    def mark_run_resumed(self, message: str | None = None) -> None:
        self._emit_runner_state("resumed", message)

    def _emit_traceability_event(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized.setdefault("timestamp", utc_timestamp())
        append_jsonl(self.paths.runner_events_path, normalized)

    def _emit_refresh_signal(
        self,
        *,
        reason: str,
        submission: bool = False,
        logs: bool = False,
        commit_history: bool = False,
        traceability_selected: bool = False,
        traceability_all: bool = False,
        preview: bool = False,
    ) -> None:
        append_jsonl(
            self.paths.runner_events_path,
            {
                "type": "signal",
                "reason": str(reason or "").strip() or "arcbench_agent_runtime",
                "timestamp": utc_timestamp(),
                "refresh": {
                    "submission": bool(submission),
                    "logs": bool(logs),
                    "commit_history": bool(commit_history),
                    "traceability_selected": bool(traceability_selected),
                    "traceability_all": bool(traceability_all),
                    "preview": bool(preview),
                },
            },
        )

    def notify_traceability_changed(self, reason: str) -> None:
        self._emit_refresh_signal(
            reason=reason,
            submission=True,
            traceability_selected=True,
            traceability_all=True,
        )

    def notify_commit_history_changed(self, reason: str, *, preview: bool = False) -> None:
        self._emit_refresh_signal(
            reason=reason,
            commit_history=True,
            preview=preview,
        )

    def read_demo_test_status_payload(self) -> dict[str, Any]:
        return {"tests": {}, "requirements": {}}

    def write_demo_test_status_payload(self, payload: dict[str, Any]) -> None:
        return None

    def set_demo_test_status(self, test_id: str, status: str | None) -> None:
        normalized_test_id = str(test_id or "").strip()
        if not normalized_test_id:
            return
        self.notify_traceability_changed("demo_test_status_updated")

    def set_demo_test_statuses(self, status_by_test_id: dict[str, str | None]) -> None:
        if not status_by_test_id:
            return
        self.notify_traceability_changed("demo_test_statuses_updated")

    def clear_demo_test_statuses(self, test_ids: list[str]) -> None:
        if not test_ids:
            return
        self.notify_traceability_changed("demo_test_statuses_cleared")

    def set_demo_requirement_status(self, req_id: str, status: str | None) -> None:
        normalized_req_id = str(req_id or "").strip()
        if not normalized_req_id:
            return
        self.notify_traceability_changed("demo_requirement_status_updated")
