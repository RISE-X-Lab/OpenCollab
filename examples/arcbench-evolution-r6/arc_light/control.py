"""Per-workflow progress and explicit handoff records."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .coordinator_tool import CoordinatorTool
from .reports import write_json

STATES = ("pending", "in_progress", "implemented_unverified", "verified_by_agent", "failed", "environment_blocked")

CHECK_COMMAND = re.compile(
    "\\b(?:pytest|unittest|vitest|playwright|curl)\\b|\\bnpm\\s+(?:run\\s+)?(?:test[^\\s]*|build|start)\\b|\\bnode\\s+(?:--check|\\S*(?:test|check|probe|e2e|smoke)\\S*)",
    re.I,
)


class RunProgress:
    def __init__(self, workspace: Path, index: list[dict], emit):
        self.workspace, self.emit = (workspace, emit)
        self.path = workspace / ".arc/checks/implementation-ledger.json"
        focus = [r for r in index if r["atomic"] and r.get("evolution_change")]
        self.rows = {
            r["id"]: {"state": "pending", "files": [], "evidence": "", "next_action": ""}
            for r in focus or [r for r in index if r["atomic"]]
        }
        self.tool_count = 0
        self.last_progress_tool = -1000
        self.progress_version = 0
        self.last_tool = None
        self.active_tools: dict[str, str] = {}
        self.check_fingerprints: set[str] = set()
        self.observations = []
        self.changed_files: set[str] = set()
        self.acceptance = {}
        self.acceptance_cache = {}
        self.acceptance_running = False
        self.acceptance_runs = 0
        self.verification_phase = "main"
        self.execution = {key: {"status": "not_started"} for key in self.rows}
        self.group_runs = []
        self.schedule = []
        self.started = time.monotonic()
        self.persist()

    def persist(self):
        write_json(
            self.path,
            {
                "evidence_kind": "agent_handoff_not_acceptance",
                "requirements": self.rows,
                "observed_checks": self.observations[-20:],
                "changed_files": sorted(self.changed_files),
                "coordinator_acceptance": self.acceptance,
                "verification_phase": self.verification_phase,
                "scheduled_execution": self.execution,
                "schedule": self.schedule,
                "group_runs": self.group_runs,
            },
        )

    def begin_group(self, group, allocation):
        for key in group["ids"]:
            self.execution[key] = {"status": "running", "group": group["number"]}
        self.group_runs.append(
            {"number": group["number"], "ids": group["ids"], "allocation": allocation, "status": "running"}
        )
        self.persist()

    def finish_group(self, group, info, changed_files):
        if changed_files:
            self.changed_files.update(changed_files)
            for evidence in self.acceptance.values():
                evidence["stale"] = True
        status = "returned_unverified" if info.get("status") == "completed" else "interrupted_unverified"
        for key in group["ids"]:
            self.execution[key] = {
                "status": status,
                "group": group["number"],
                "reason": info.get("reason"),
                "changed_files": changed_files,
            }
        self.group_runs[-1].update(
            status=status, tokens=info.get("tokens", 0), steps=info.get("steps"), changed_files=changed_files
        )
        self.persist()

    def observed(self, name, params, result, changed):
        self.tool_count += 1
        self.last_tool = name
        if changed:
            self.changed_files.update(changed)
            self.progress_version += 1
            self.last_progress_tool = self.tool_count
            for row in self.acceptance.values():
                row["stale"] = True
        command = str(params.get("command", ""))
        if name == "bash" and CHECK_COMMAND.search(command) and str(result).strip():
            fingerprint = hashlib.sha256((command + "\n" + str(result)).encode()).hexdigest()
            if fingerprint not in self.check_fingerprints:
                self.check_fingerprints.add(fingerprint)
                self.progress_version += 1
                self.last_progress_tool = self.tool_count
                self.observations.append(
                    {
                        "tool_number": self.tool_count,
                        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                        "result_sha256": hashlib.sha256(str(result).encode()).hexdigest(),
                        "verdict": "executed; success not inferred from text",
                    }
                )
        self.persist()

    def record_acceptance(self, report, digest):
        from .evidence import summarize

        summary = summarize(report)
        selected = set(report.get("requirement_ids", []))
        for requirement, evidence in self.acceptance.items():
            if evidence.get("source_digest") != digest or (
                not report.get("ok")
                and (not report.get("scenario_coverage", {}).get("scenarios"))
                and (report.get("scope", "all") != "requirements" or requirement in selected)
            ):
                evidence["stale"] = True
        evidence = {
            "scope": summary["scope"],
            "coverage": summary["coverage"],
            "checks": [(c["step"], c.get("ok")) for c in report.get("checks", [])],
            "scenarios": [
                (c["name"], c.get("status"), c.get("blocked_by"))
                for c in report.get("browser", {}).get("prerequisites", [])
            ],
        }
        fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        self.tool_count += 1
        if fingerprint not in self.check_fingerprints:
            self.check_fingerprints.add(fingerprint)
            self.progress_version += 1
            self.last_progress_tool = self.tool_count
            self.observations.append(
                {"tool_number": self.tool_count, "kind": "coordinator_acceptance", "source_digest": digest, **evidence}
            )
        for requirement in self.rows:
            scenarios = [
                c
                for c in report.get("scenario_coverage", {}).get("scenarios", [])
                if requirement in c.get("source_ids", [])
            ]
            if scenarios:
                statuses = {c["status"] for c in scenarios}
                state = (
                    "passed"
                    if statuses == {"passed"}
                    else "failed"
                    if "failed" in statuses
                    else "blocked"
                    if "blocked" in statuses
                    else "unverified"
                )
                self.acceptance[requirement] = {
                    "state": state,
                    "stale": False,
                    "source_digest": digest,
                    "scenario_count": len(scenarios),
                }
        stability = report.get("stability", {})
        if stability and (not stability.get("ok")):
            failed_ids = {key for card in stability.get("failures", []) for key in card.get("source_ids", [])}
            for check in stability.get("checks", []):
                if check.get("consecutive_passes", 0) < stability["required_consecutive_passes"]:
                    for key in check.get("source_ids", []):
                        if key in self.acceptance:
                            self.acceptance[key].update(
                                state="failed" if key in failed_ids else "unverified", stability_pending=True
                            )
        self.persist()
        self.emit(
            {
                "diag": "coordinator_acceptance",
                "scope": summary["scope"],
                "ok": summary["ok"],
                "coverage": summary["coverage"],
            }
        )

    def recent_progress(self):
        return self.progress_version > 0 and self.tool_count - self.last_progress_tool <= 16

    def handoff(self):
        from .stability import read_history

        history = read_history(self.workspace)
        risks = {name: row for name, row in history["checks"].items() if row.get("last_failure")}
        return json.dumps(
            {
                "evidence_kind": "agent_handoff_not_acceptance",
                "requirements": self.rows,
                "coordinator_acceptance": self.acceptance,
                "scheduled_execution": self.execution,
                "changed_files": sorted(self.changed_files)[-40:],
                "stability_failure_history": risks,
                "observed_checks": self.observations[-6:],
            },
            ensure_ascii=False,
        )


class CheckpointTool(CoordinatorTool):
    name = "checkpoint"
    description = (
        "Record concise progress for current requirement IDs. This preserves handoff across "
        "compaction or interruption; it does not certify acceptance. Update after a feature or blocker."
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"type": "string", "enum": list(STATES)},
                        "files": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                        "evidence": {"type": "string", "maxLength": 900},
                        "next_action": {"type": "string", "maxLength": 500},
                    },
                    "required": ["id", "state", "files", "evidence", "next_action"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    def __init__(self, progress):
        self.progress = progress

    async def execute_with_runtime(self, params, runtime):
        items = params["items"]
        for item in items:
            if item["id"] not in self.progress.rows or item["state"] not in STATES:
                return "Error: unknown requirement ID or state; no checkpoint changed."
            if item["state"] == "verified_by_agent" and (not item["evidence"].strip()):
                return "Error: verification needs the actual command/path and observed result."
        for item in items:
            self.progress.rows[item["id"]] = {key: value for key, value in item.items() if key != "id"}
        self.progress.persist()
        counts = {state: sum((r["state"] == state for r in self.progress.rows.values())) for state in STATES}
        self.progress.emit(
            {"diag": "checkpoint", "counts": counts, "ids": [item["id"] for item in items], "acceptance": "unverified"}
        )
        return "Checkpoint saved (self-report, not acceptance). Continue the next concrete action."
