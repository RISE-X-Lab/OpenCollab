"""Progress-based soft allowance policy using the public OC run-control API."""

from __future__ import annotations

import json

from opencollab.sdk import BudgetDecision, RunControl


class SessionPolicy:
    def __init__(self, progress, phase, settings, state, soft, hard):
        self.progress, self.phase, self.settings, self.state = progress, phase, settings, state
        self.soft, self.hard = soft, hard
        self.last_extended_version = 0
        self.last_notice_cap = None
        self.tokens = self.steps = self.compactions = self.shaping_events = 0
        self.path = progress.workspace / ".arc/checks/session-metrics.jsonl"

    def record(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": self.phase, **data}, ensure_ascii=False) + "\n")

    def event(self, event):
        self.tokens, self.steps = event.used_tokens, event.steps
        data = dict(event.data)
        self.record(
            {
                "event": event.type,
                "run_id": event.run_id,
                "session_id": event.session_id,
                "tokens": event.used_tokens,
                "steps": event.steps,
                **data,
            }
        )
        if event.session_id:
            self.state.observe(self.phase, event.session_id, event.used_tokens, event.steps)
        if event.type == "usage":
            with (self.path.parent / "wire-usage.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"phase": self.phase, "run_id": event.run_id, "session_id": event.session_id, **data})
                    + "\n"
                )
        if event.type == "context_shaping":
            reports = data.get("reports", [data])
            fired = [r for r in reports if r.get("rung") != "none"]
            self.shaping_events += len(fired)
            self.compactions += sum(
                r.get("rung") in {"tool_output_clear", "old_history_snip", "auto_compact"} for r in fired
            )

    def decide(self, snapshot):
        cap = snapshot.soft_budget_tokens
        if cap is None:
            return BudgetDecision(None)
        self.tokens, self.steps = snapshot.used_tokens, snapshot.steps
        headroom = max(snapshot.minimum_output_tokens, self.settings.max_output_tokens)
        approaching = cap - self.tokens < snapshot.reserved_input_tokens + headroom + min(200_000, max(1, cap // 10))
        if not approaching:
            return BudgetDecision(cap)
        evidence = self.progress.recent_progress() and self.progress.progress_version > self.last_extended_version
        ceiling = min(self.hard, snapshot.hard_budget_tokens or self.hard)
        if evidence and cap < ceiling:
            new_cap = min(ceiling, max(cap + 1_000_000, self.tokens + snapshot.reserved_input_tokens + headroom))
            self.last_extended_version = self.progress.progress_version
            self.progress.emit(
                {
                    "diag": "budget_extended",
                    "phase": self.phase,
                    "old_cap": cap,
                    "new_cap": new_cap,
                    "hard_cap": ceiling,
                    "tokens": self.tokens,
                    "same_session": True,
                }
            )
            return BudgetDecision(new_cap)
        prompt = None
        if self.last_notice_cap != cap:
            self.last_notice_cap = cap
            prompt = (
                "Budget is near its current limit. Finish the current operation and focused verification; "
                "record checkpoint states and next actions now. Do not start another audit or redesign. "
                "Only recent source changes or new check evidence can unlock available contingency budget."
            )
        return BudgetDecision(cap, final_prompt=prompt)

    def control(self):
        return RunControl(
            initial_soft_budget_tokens=self.soft,
            decide_budget=self.decide,
            on_event=self.event,
            history_trigger_tokens=self.settings.history_trigger_tokens,
            tool_cancellation_cleanup_timeout=self.settings.tool_cleanup_seconds,
        )
