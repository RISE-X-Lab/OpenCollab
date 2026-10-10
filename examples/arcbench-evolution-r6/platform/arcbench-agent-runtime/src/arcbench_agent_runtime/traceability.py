from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import RuntimePaths
from .events import EventClient
from .jsonio import read_json, write_json_atomic

TABLE_NAMES = (
    "requirements",
    "scenarios",
    "interfaces",
    "tests",
    "call_edges",
    "node_states",
    "node_contracts",
)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _as_optional_str(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _as_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "passed"}:
        return True
    if normalized in {"0", "false", "no", "failed"}:
        return False
    return None


def _edge_key(source_req_id: str, target_req_id: str, from_interface_id: str, to_interface_id: str) -> str:
    return "::".join(
        [
            str(source_req_id or "").strip(),
            str(target_req_id or "").strip(),
            str(from_interface_id or "").strip(),
            str(to_interface_id or "").strip(),
        ]
    )


@dataclass(frozen=True)
class RequirementRecord:
    req_id: str
    name: str = ""
    description: str = ""
    visual_reference: list[str] | None = None
    scenarios: list[dict[str, Any]] | None = None
    parent_id: str | None = None
    children_ids: list[str] | None = None
    dependencies: list[str] | None = None


@dataclass(frozen=True)
class ScenarioRecord:
    scenario_id: str
    req_id: str
    name: str
    steps: list[dict[str, str]]


@dataclass(frozen=True)
class InterfaceRecord:
    interface_id: str
    req_ids: list[str]
    type: str
    content: str
    file_path: str | None = None
    first_line: str | None = None
    implemented: bool = False
    callers: list[str] | None = None
    callees: list[str] | None = None


@dataclass(frozen=True)
class TestRecord:
    test_id: str
    req_id: str
    type: str
    file_path: str | None = None
    first_line: str | None = None
    interface_ids: list[str] | None = None
    passed: bool | None = None
    scenario_id: str | None = None


class TraceabilityStore:
    """Keyed JSON traceability store.

    Each table is stored as one JSON object under `.arc/traceability/<table>.json`.
    This keeps current-state reads and overwrites simple, deterministic, and git-trackable.
    """

    def __init__(self, paths: RuntimePaths, events: EventClient) -> None:
        self.paths = paths
        self.events = events

    @property
    def root(self) -> Path:
        return self.paths.traceability_dir

    def table_path(self, table_name: str) -> Path:
        if table_name not in TABLE_NAMES:
            raise ValueError(f"Unknown traceability table: {table_name}")
        return self.root / f"{table_name}.json"

    def _read_table(self, table_name: str) -> dict[str, Any]:
        payload = read_json(self.table_path(table_name), {})
        return payload if isinstance(payload, dict) else {}

    def _write_table(self, table_name: str, rows: dict[str, Any]) -> None:
        write_json_atomic(self.table_path(table_name), dict(sorted(rows.items())))

    def init_db(self, *, reset: bool = False) -> None:
        self.init_store(reset=reset)

    def init_store(self, *, reset: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for table_name in TABLE_NAMES:
            path = self.table_path(table_name)
            if reset or not path.exists():
                write_json_atomic(path, {})
        self.events.notify_traceability_changed("traceability_store_initialized")

    def export_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "requirements": self.list_requirements(),
            "scenarios": self.list_scenarios(),
            "interfaces": self.list_interfaces(),
            "tests": self.list_tests(),
            "call_edges": self.list_call_edges(),
            "node_states": self.list_node_states(),
            "node_contracts": self.list_node_contracts(),
        }

    def store_requirement_tree(self, requirement_tree: dict[str, Any]) -> None:
        """Persist a nested ARC requirements tree into current-state tables.

        Only requirements and scenarios are replaced here. Generated interfaces,
        tests, node states, and contracts remain owned by their phase-specific
        SDK calls and by git history.
        """

        requirements: dict[str, Any] = {}
        scenarios: dict[str, Any] = {}

        def walk(node: dict[str, Any], parent_id: str | None = None) -> None:
            if not isinstance(node, dict):
                return
            req_id = str(node.get("id") or node.get("req_id") or "").strip()
            if not req_id:
                return
            children = [child for child in _as_list(node.get("children")) if isinstance(child, dict)]
            children_ids = [
                str(child.get("id") or child.get("req_id") or "").strip()
                for child in children
                if str(child.get("id") or child.get("req_id") or "").strip()
            ]
            node_scenarios = [dict(item) for item in _as_list(node.get("scenarios")) if isinstance(item, dict)]
            requirements[req_id] = {
                "req_id": req_id,
                "id": req_id,
                "name": str(node.get("name") or "").strip(),
                "description": str(node.get("description") or "").strip(),
                "visual_reference": _as_str_list(node.get("visual_reference")),
                "scenarios": node_scenarios,
                "parent_id": _as_optional_str(parent_id),
                "children_ids": children_ids,
                "dependencies": _as_str_list(node.get("dependencies")),
            }
            for scenario in node_scenarios:
                scenario_id = str(scenario.get("id") or scenario.get("scenario_id") or "").strip()
                if not scenario_id:
                    continue
                scenarios[scenario_id] = {
                    "scenario_id": scenario_id,
                    "id": scenario_id,
                    "name": str(scenario.get("name") or "").strip() or scenario_id,
                    "req_id": req_id,
                    "steps": _as_list(scenario.get("steps")),
                }
            for child in children:
                walk(child, req_id)

        walk(requirement_tree)
        self._write_table("requirements", requirements)
        self._write_table("scenarios", scenarios)
        self.events.notify_traceability_changed("requirement_tree_stored")

    def _get_row(self, table_name: str, key: str) -> dict[str, Any] | None:
        row = self._read_table(table_name).get(str(key or "").strip())
        return dict(row) if isinstance(row, dict) else None

    def _upsert_row(self, table_name: str, key: str, row: dict[str, Any]) -> None:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise ValueError("Traceability row key is required")
        rows = self._read_table(table_name)
        rows[normalized_key] = row
        self._write_table(table_name, rows)

    def _delete_row(self, table_name: str, key: str) -> None:
        rows = self._read_table(table_name)
        rows.pop(str(key or "").strip(), None)
        self._write_table(table_name, rows)

    def get_requirement(self, req_id: str) -> dict[str, Any] | None:
        return self._get_row("requirements", req_id)

    def list_requirements(self) -> list[dict[str, Any]]:
        return list(self._read_table("requirements").values())

    def upsert_requirement(
        self,
        *,
        req_id: str,
        name: str = "",
        description: str = "",
        visual_reference: list[str] | None = None,
        scenarios: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
        children_ids: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> None:
        normalized_req_id = str(req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("req_id is required")
        normalized_scenarios = [dict(item) for item in _as_list(scenarios) if isinstance(item, dict)]
        self._upsert_row(
            "requirements",
            normalized_req_id,
            {
                "req_id": normalized_req_id,
                "name": str(name or "").strip(),
                "description": str(description or "").strip(),
                "visual_reference": _as_str_list(visual_reference),
                "scenarios": normalized_scenarios,
                "parent_id": _as_optional_str(parent_id),
                "children_ids": _as_str_list(children_ids),
                "dependencies": _as_str_list(dependencies),
            },
        )
        scenarios_table = self._read_table("scenarios")
        for scenario_id, scenario in list(scenarios_table.items()):
            if isinstance(scenario, dict) and scenario.get("req_id") == normalized_req_id:
                scenarios_table.pop(scenario_id, None)
        for scenario in normalized_scenarios:
            scenario_id = str(scenario.get("id") or scenario.get("scenario_id") or "").strip()
            if not scenario_id:
                continue
            scenarios_table[scenario_id] = {
                "scenario_id": scenario_id,
                "name": str(scenario.get("name") or "").strip() or scenario_id,
                "req_id": normalized_req_id,
                "steps": _as_list(scenario.get("steps")),
            }
        self._write_table("scenarios", scenarios_table)
        self.events.notify_traceability_changed("requirements_updated")

    def update_requirement_fields(self, req_id: str, **fields: Any) -> None:
        current = self.get_requirement(req_id)
        if current is None:
            raise ValueError(f"Requirement not found: {req_id}")
        merged = {**current, **fields}
        self.upsert_requirement(
            req_id=req_id,
            name=str(merged.get("name") or "").strip(),
            description=str(merged.get("description") or "").strip(),
            visual_reference=_as_str_list(merged.get("visual_reference")),
            scenarios=_as_list(merged.get("scenarios")),
            parent_id=merged.get("parent_id"),
            children_ids=_as_str_list(merged.get("children_ids")),
            dependencies=_as_str_list(merged.get("dependencies")),
        )

    def delete_requirement(self, req_id: str) -> None:
        normalized_req_id = str(req_id or "").strip()
        self._delete_row("requirements", normalized_req_id)
        for table_name, field_name in (
            ("scenarios", "req_id"),
            ("tests", "req_id"),
            ("node_states", "req_id"),
            ("node_contracts", "req_id"),
        ):
            rows = self._read_table(table_name)
            rows = {
                key: row
                for key, row in rows.items()
                if not isinstance(row, dict) or row.get(field_name) != normalized_req_id
            }
            self._write_table(table_name, rows)
        call_edges = self._read_table("call_edges")
        call_edges = {
            key: row
            for key, row in call_edges.items()
            if not isinstance(row, dict)
            or (row.get("source_req_id") != normalized_req_id and row.get("target_req_id") != normalized_req_id)
        }
        self._write_table("call_edges", call_edges)
        self.events.notify_traceability_changed("requirement_deleted")

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        return self._get_row("scenarios", scenario_id)

    def list_scenarios(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._read_table("scenarios").values())
        if req_id:
            rows = [row for row in rows if isinstance(row, dict) and row.get("req_id") == req_id]
        return [dict(row) for row in rows if isinstance(row, dict)]

    def upsert_scenario(self, *, scenario_id: str, req_id: str, name: str, steps: list[dict[str, str]]) -> None:
        normalized_scenario_id = str(scenario_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_scenario_id or not normalized_req_id:
            raise ValueError("scenario_id and req_id are required")
        self._upsert_row(
            "scenarios",
            normalized_scenario_id,
            {
                "scenario_id": normalized_scenario_id,
                "name": str(name or "").strip(),
                "req_id": normalized_req_id,
                "steps": _as_list(steps),
            },
        )
        requirement = self.get_requirement(normalized_req_id)
        if requirement:
            scenarios = [
                item
                for item in _as_list(requirement.get("scenarios"))
                if str(item.get("id") or item.get("scenario_id") or "").strip() != normalized_scenario_id
            ]
            scenarios.append({"id": normalized_scenario_id, "name": str(name or "").strip(), "steps": _as_list(steps)})
            self.update_requirement_fields(normalized_req_id, scenarios=scenarios)
        self.events.notify_traceability_changed("scenarios_updated")

    def delete_scenario(self, scenario_id: str) -> None:
        scenario = self.get_scenario(scenario_id)
        self._delete_row("scenarios", scenario_id)
        if scenario:
            req_id = str(scenario.get("req_id") or "").strip()
            requirement = self.get_requirement(req_id)
            if requirement:
                scenarios = [
                    item
                    for item in _as_list(requirement.get("scenarios"))
                    if str(item.get("id") or item.get("scenario_id") or "").strip() != scenario_id
                ]
                self.update_requirement_fields(req_id, scenarios=scenarios)
        self.events.notify_traceability_changed("scenario_deleted")

    def get_interface(self, interface_id: str) -> dict[str, Any] | None:
        return self._get_row("interfaces", interface_id)

    def list_interfaces(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._read_table("interfaces").values() if isinstance(row, dict)]
        if req_id:
            rows = [row for row in rows if req_id in _as_str_list(row.get("req_ids"))]
        return rows

    def upsert_interface(
        self,
        *,
        interface_id: str,
        req_ids: list[str],
        type: str,
        content: str,
        file_path: str | None = None,
        first_line: str | None = None,
        implemented: bool = False,
        callers: list[str] | None = None,
        callees: list[str] | None = None,
        emit_event: bool = True,
    ) -> None:
        normalized_interface_id = str(interface_id or "").strip()
        if not normalized_interface_id:
            raise ValueError("interface_id is required")
        payload = {
            "interface_id": normalized_interface_id,
            "req_ids": _as_str_list(req_ids),
            "type": str(type or "").strip(),
            "content": str(content or "").strip(),
            "file_path": _as_optional_str(file_path),
            "first_line": _as_optional_str(first_line),
            "implemented": bool(implemented),
            "callers": _as_str_list(callers),
            "callees": _as_str_list(callees),
        }
        self._upsert_row("interfaces", normalized_interface_id, payload)
        if emit_event:
            self.events._emit_traceability_event(
                {
                    "type": "interface_upsert",
                    "interface_id": normalized_interface_id,
                    "req_ids": payload["req_ids"],
                    "interface_type": payload["type"],
                    "content": payload["content"],
                    "file_path": payload["file_path"],
                    "first_line": payload["first_line"],
                    "implemented": payload["implemented"],
                    "callers": payload["callers"],
                    "callees": payload["callees"],
                }
            )

    def update_interface_fields(self, interface_id: str, **fields: Any) -> None:
        current = self.get_interface(interface_id)
        if current is None:
            raise ValueError(f"Interface not found: {interface_id}")
        merged = {**current, **fields}
        self.upsert_interface(
            interface_id=interface_id,
            req_ids=_as_str_list(merged.get("req_ids")),
            type=str(merged.get("type") or "").strip(),
            content=str(merged.get("content") or "").strip(),
            file_path=merged.get("file_path"),
            first_line=merged.get("first_line"),
            implemented=bool(merged.get("implemented")),
            callers=_as_str_list(merged.get("callers")),
            callees=_as_str_list(merged.get("callees")),
        )

    def set_interface_implemented(
        self,
        interface_id: str,
        implemented: bool,
        message: str | None = None,
        *,
        emit_event: bool = True,
    ) -> None:
        current = self.get_interface(interface_id)
        if current is None:
            raise ValueError(f"Interface not found: {interface_id}")
        current["implemented"] = bool(implemented)
        self._upsert_row("interfaces", interface_id, current)
        if emit_event:
            self.events._emit_traceability_event(
                {
                    "type": "interface_status",
                    "interface_id": str(interface_id or "").strip(),
                    "implemented": bool(implemented),
                    "message": message,
                }
            )

    def delete_interface(self, interface_id: str) -> None:
        self._delete_row("interfaces", interface_id)
        self.events.notify_traceability_changed("interface_deleted")

    def get_test(self, test_id: str) -> dict[str, Any] | None:
        return self._get_row("tests", test_id)

    def list_tests(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._read_table("tests").values() if isinstance(row, dict)]
        if req_id:
            rows = [row for row in rows if row.get("req_id") == req_id]
        return rows

    def upsert_test(
        self,
        *,
        test_id: str,
        req_id: str,
        type: str,
        file_path: str | None = None,
        first_line: str | None = None,
        interface_ids: list[str] | None = None,
        passed: bool | None = None,
        scenario_id: str | None = None,
        emit_event: bool = True,
    ) -> None:
        normalized_test_id = str(test_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_test_id or not normalized_req_id:
            raise ValueError("test_id and req_id are required")
        existing = self.get_test(normalized_test_id)
        if existing and existing.get("req_id") != normalized_req_id:
            raise ValueError(
                f"Test id collision detected for `{normalized_test_id}`: "
                f"existing req_id=`{existing.get('req_id')}`, new req_id=`{normalized_req_id}`."
            )
        self._upsert_row(
            "tests",
            normalized_test_id,
            {
                "test_id": normalized_test_id,
                "req_id": normalized_req_id,
                "interface_ids": _as_str_list(interface_ids),
                "type": str(type or "").strip(),
                "file_path": _as_optional_str(file_path),
                "passed": _as_bool_or_none(passed),
                "first_line": _as_optional_str(first_line),
                "scenario_id": _as_optional_str(scenario_id),
            },
        )
        if emit_event:
            self.events._emit_traceability_event(
                {
                    "type": "test_upsert",
                    "test_id": normalized_test_id,
                    "req_id": normalized_req_id,
                    "scenario_id": _as_optional_str(scenario_id),
                    "test_type": str(type or "").strip(),
                    "file_path": _as_optional_str(file_path),
                    "first_line": _as_optional_str(first_line),
                    "interface_ids": _as_str_list(interface_ids),
                }
            )

    def update_test_fields(self, test_id: str, **fields: Any) -> None:
        current = self.get_test(test_id)
        if current is None:
            raise ValueError(f"Test not found: {test_id}")
        merged = {**current, **fields}
        self.upsert_test(
            test_id=test_id,
            req_id=str(merged.get("req_id") or "").strip(),
            type=str(merged.get("type") or "").strip(),
            file_path=merged.get("file_path"),
            first_line=merged.get("first_line"),
            interface_ids=_as_str_list(merged.get("interface_ids")),
            passed=_as_bool_or_none(merged.get("passed")),
            scenario_id=merged.get("scenario_id"),
        )

    def set_test_pass_status(self, test_id: str, passed: bool | None) -> None:
        current = self.get_test(test_id)
        if current is None:
            raise ValueError(f"Test not found: {test_id}")
        current["passed"] = _as_bool_or_none(passed)
        self._upsert_row("tests", test_id, current)
        self.events.notify_traceability_changed("test_status_updated")

    def set_test_pass_statuses(self, status_by_test_id: dict[str, bool | None]) -> None:
        if not status_by_test_id:
            return
        rows = self._read_table("tests")
        for test_id, passed in status_by_test_id.items():
            key = str(test_id or "").strip()
            row = rows.get(key)
            if isinstance(row, dict):
                row["passed"] = _as_bool_or_none(passed)
        self._write_table("tests", rows)
        self.events.notify_traceability_changed("test_statuses_updated")

    def reset_test_pass_statuses_for_requirement(self, req_id: str) -> None:
        rows = self._read_table("tests")
        for row in rows.values():
            if isinstance(row, dict) and row.get("req_id") == req_id:
                row["passed"] = None
        self._write_table("tests", rows)
        self.events.notify_traceability_changed("test_statuses_reset")

    def delete_test(self, test_id: str) -> None:
        self._delete_row("tests", test_id)
        self.events.notify_traceability_changed("test_deleted")

    def insert_call_edge(
        self,
        *,
        source_req_id: str,
        target_req_id: str,
        from_interface_id: str,
        to_interface_id: str,
        edge_type: str = "parent_child",
    ) -> None:
        key = _edge_key(source_req_id, target_req_id, from_interface_id, to_interface_id)
        self._upsert_row(
            "call_edges",
            key,
            {
                "source_req_id": str(source_req_id or "").strip(),
                "target_req_id": str(target_req_id or "").strip(),
                "from_interface_id": str(from_interface_id or "").strip(),
                "to_interface_id": str(to_interface_id or "").strip(),
                "edge_type": str(edge_type or "").strip() or "parent_child",
                "created_at": _utc_timestamp(),
            },
        )
        self.events.notify_traceability_changed("call_edges_updated")

    def list_call_edges(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._read_table("call_edges").values() if isinstance(row, dict)]
        if req_id:
            rows = [row for row in rows if row.get("source_req_id") == req_id or row.get("target_req_id") == req_id]
        return rows

    def delete_call_edge(
        self,
        *,
        source_req_id: str,
        target_req_id: str,
        from_interface_id: str,
        to_interface_id: str,
    ) -> None:
        self._delete_row("call_edges", _edge_key(source_req_id, target_req_id, from_interface_id, to_interface_id))
        self.events.notify_traceability_changed("call_edge_deleted")

    def upsert_node_state(self, req_id: str, state: str, phase: str | None = None) -> None:
        normalized_req_id = str(req_id or "").strip()
        normalized_state = str(state or "").strip()
        if not normalized_req_id:
            raise ValueError("req_id is required")
        self._upsert_row(
            "node_states",
            normalized_req_id,
            {
                "req_id": normalized_req_id,
                "state": normalized_state,
                "phase": _as_optional_str(phase),
                "updated_at": _utc_timestamp(),
            },
        )
        self.events.notify_traceability_changed("node_state_updated")

    def set_requirement_state(self, req_id: str, state: str, phase: str | None = None) -> None:
        self.upsert_node_state(req_id, state, phase=phase)

    def get_node_state(self, req_id: str) -> dict[str, Any] | None:
        return self._get_row("node_states", req_id)

    def get_requirement_state(self, req_id: str) -> dict[str, Any] | None:
        return self.get_node_state(req_id)

    def list_node_states(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._read_table("node_states").values() if isinstance(row, dict)]

    def list_requirement_states(self) -> list[dict[str, Any]]:
        return self.list_node_states()

    def delete_node_state(self, req_id: str) -> None:
        self._delete_row("node_states", req_id)
        self.events.notify_traceability_changed("node_state_deleted")

    def upsert_node_contract(self, req_id: str, content: dict[str, Any]) -> None:
        normalized_req_id = str(req_id or "").strip()
        self._upsert_row(
            "node_contracts",
            normalized_req_id,
            {
                "req_id": normalized_req_id,
                "content": content if isinstance(content, dict) else {},
                "updated_at": _utc_timestamp(),
            },
        )
        self.events.notify_traceability_changed("node_contract_updated")

    def get_node_contract(self, req_id: str) -> dict[str, Any] | None:
        return self._get_row("node_contracts", req_id)

    def list_node_contracts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._read_table("node_contracts").values() if isinstance(row, dict)]

    def delete_node_contract(self, req_id: str) -> None:
        self._delete_row("node_contracts", req_id)
        self.events.notify_traceability_changed("node_contract_deleted")

    def clear_node_design_artifacts(self, req_id: str) -> None:
        interfaces = self._read_table("interfaces")
        for key, row in list(interfaces.items()):
            if not isinstance(row, dict):
                continue
            remaining_req_ids = [value for value in _as_str_list(row.get("req_ids")) if value != req_id]
            if remaining_req_ids:
                row["req_ids"] = remaining_req_ids
            else:
                interfaces.pop(key, None)
        self._write_table("interfaces", interfaces)

        tests = self._read_table("tests")
        tests = {key: row for key, row in tests.items() if not isinstance(row, dict) or row.get("req_id") != req_id}
        self._write_table("tests", tests)

        call_edges = self._read_table("call_edges")
        call_edges = {
            key: row
            for key, row in call_edges.items()
            if not isinstance(row, dict) or (row.get("source_req_id") != req_id and row.get("target_req_id") != req_id)
        }
        self._write_table("call_edges", call_edges)
        self.events.notify_traceability_changed("design_artifacts_cleared")
