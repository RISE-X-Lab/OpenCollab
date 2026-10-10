"""Agent-accessible verification with coordinator-owned execution."""

from __future__ import annotations

import asyncio
import json
from threading import Event

from .coordinator_tool import CoordinatorTool
from .evidence import source_digest, summarize


class AcceptanceTool(CoordinatorTool):
    name = "run_acceptance"
    description = (
        "Run coordinator-owned checks on an isolated app/database copy. Use shared after navigation/auth changes, "
        "requirements for named requirement IDs, all only when ready. Returns real failures, not a model verdict. "
        "During grouped implementation all is scoped to started groups; final checks remain complete. "
        "Do not edit source concurrently. Identical source/scope reuses evidence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["shared", "requirements", "all"]},
            "requirement_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
        "required": ["scope", "requirement_ids"],
        "additionalProperties": False,
    }
    default_timeout = 660

    def __init__(self, progress, remaining_seconds):
        self.progress, self.remaining_seconds = (progress, remaining_seconds)

    async def execute_with_runtime(self, params, runtime):
        from .delivery import verify
        from .stability import cache_signature

        progress = self.progress
        if progress.acceptance_running:
            return "A coordinator check is already running. Wait before checking or changing source."
        scope, ids = (params["scope"], sorted(set(params["requirement_ids"])))
        if scope == "requirements" and (not ids or any((key not in progress.rows for key in ids))):
            return "Error: supply current requirement IDs for requirements scope."
        if scope != "requirements" and ids:
            return "Error: requirement_ids must be empty for shared/all scope."
        requested_scope = scope
        shared_ids = None
        deferred = []
        if progress.schedule and progress.verification_phase == "main":
            ready = sorted((key for key, row in progress.execution.items() if row["status"] != "not_started"))
            deferred = sorted(set(progress.rows) - set(ready))
            if scope == "all" and deferred:
                scope, ids = ("requirements", ready)
                if not ids:
                    return json.dumps(
                        {
                            "ok": False,
                            "unverified": True,
                            "deferred_ids": deferred,
                            "reason": "No group has started; full acceptance runs after implementation.",
                        }
                    )
            elif scope == "requirements" and set(ids) - set(ready):
                return "Error: unstarted groups are deferred; check current or earlier requirement IDs."
            elif scope == "shared":
                shared_ids = ready
        seconds = min(600, self.remaining_seconds() - 90)
        if seconds < 60:
            return "Insufficient time for a coordinator check; acceptance remains unverified."
        digest = source_digest(progress.workspace)
        base_key = (
            scope,
            tuple(ids),
            tuple(shared_ids or ()),
            digest,
            requested_scope,
            tuple(deferred),
            progress.verification_phase,
        )
        key = (*base_key, cache_signature(progress.workspace, scope, ids))
        if key in progress.acceptance_cache:
            return json.dumps(dict(progress.acceptance_cache[key], cached=True), ensure_ascii=False)
        progress.acceptance_running = True
        progress.active_tools["coordinator"] = self.name
        progress.acceptance_runs += 1
        output = progress.workspace / ".arc/checks/focused" / str(progress.acceptance_runs)
        cancel_event = Event()
        try:
            task = asyncio.create_task(
                asyncio.to_thread(
                    verify,
                    progress.workspace,
                    timeout=seconds,
                    scope=scope,
                    requirement_ids=ids,
                    output_dir=output,
                    cancel_event=cancel_event,
                    shared_requirement_ids=shared_ids,
                    confirm_stability=scope == "all" and progress.verification_phase == "final",
                )
            )
            try:
                report = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel_event.set()
                await task
                raise
            result = summarize(report)
            result.update(requested_scope=requested_scope, deferred_ids=deferred, full_acceptance=scope == "all")
            result["report_path"] = str(output / "verification.json")
            if source_digest(progress.workspace) != digest:
                result.update(ok=False, stale=True, reason="Source changed while checking; rerun after edits finish.")
                return json.dumps(result, ensure_ascii=False)
            progress.record_acceptance(report, digest)
            if not any((c["kind"] == "infrastructure" or c["kind"] == "skipped" for c in result["failures"])):
                progress.acceptance_cache[(*base_key, cache_signature(progress.workspace, scope, ids))] = result
            return json.dumps(result, ensure_ascii=False)
        finally:
            progress.acceptance_running = False
            progress.active_tools.pop("coordinator", None)
