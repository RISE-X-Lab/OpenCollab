from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_RUNNER_EVENTS_PATH = ".arc/runner-events.jsonl"
DEFAULT_TRACEABILITY_DIR = ".arc/traceability"
DEFAULT_PROJECT_DIR = "."
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


def _resolve_under_project(project_dir: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_dir / path


def _paths(
    project_dir: str | None,
    events_path: str | None,
    traceability_dir: str | None,
) -> tuple[Path, Path, Path]:
    resolved_project_dir = (
        Path(
            project_dir
            or os.environ.get("ARCBENCH_OUTPUT_DIR", "").strip()
            or os.environ.get("ARCBENCH_PROJECT_DIR", "").strip()
            or os.environ.get("ARCBENCH_TEMPLATE_DIR", "").strip()
            or DEFAULT_PROJECT_DIR
        )
        .expanduser()
        .resolve()
    )
    runner_events_value = (
        events_path or os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip() or DEFAULT_RUNNER_EVENTS_PATH
    )
    traceability_value = (
        traceability_dir or os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or DEFAULT_TRACEABILITY_DIR
    )
    runner_events_path = _resolve_under_project(resolved_project_dir, runner_events_value)
    traceability_path = _resolve_under_project(resolved_project_dir, traceability_value)
    resolved_project_dir.mkdir(parents=True, exist_ok=True)
    runner_events_path.parent.mkdir(parents=True, exist_ok=True)
    traceability_path.mkdir(parents=True, exist_ok=True)
    return resolved_project_dir, runner_events_path, traceability_path


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    tmp_path.replace(path)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        return json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    if args.payload_json:
        return json.loads(args.payload_json)
    return {}


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


def _table_path(traceability_dir: Path, table_name: str) -> Path:
    if table_name not in TABLE_NAMES:
        raise ValueError(f"Unknown traceability table: {table_name}")
    return traceability_dir / f"{table_name}.json"


def _read_table(traceability_dir: Path, table_name: str) -> dict[str, Any]:
    return _read_json(_table_path(traceability_dir, table_name), {})


def _write_table(traceability_dir: Path, table_name: str, rows: dict[str, Any]) -> None:
    _write_json_atomic(_table_path(traceability_dir, table_name), dict(sorted(rows.items())))


def _upsert_row(traceability_dir: Path, table_name: str, key: str, row: dict[str, Any]) -> None:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        raise ValueError("Traceability row key is required")
    rows = _read_table(traceability_dir, table_name)
    rows[normalized_key] = row
    _write_table(traceability_dir, table_name, rows)


def _delete_row(traceability_dir: Path, table_name: str, key: str) -> None:
    rows = _read_table(traceability_dir, table_name)
    rows.pop(str(key or "").strip(), None)
    _write_table(traceability_dir, table_name, rows)


def _emit_refresh_signal(
    runner_events_path: Path,
    *,
    reason: str,
    submission: bool = False,
    logs: bool = False,
    commit_history: bool = False,
    traceability_selected: bool = False,
    traceability_all: bool = False,
    preview: bool = False,
) -> None:
    _append_jsonl(
        runner_events_path,
        {
            "type": "signal",
            "reason": str(reason or "").strip() or "arcbench_traceability",
            "timestamp": _utc_timestamp(),
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


def _notify_traceability_changed(runner_events_path: Path, reason: str) -> None:
    _emit_refresh_signal(
        runner_events_path,
        reason=reason,
        submission=True,
        traceability_selected=True,
        traceability_all=True,
    )


def _emit_traceability_event(runner_events_path: Path, payload: dict[str, Any]) -> None:
    normalized = dict(payload)
    normalized.setdefault("timestamp", _utc_timestamp())
    _append_jsonl(runner_events_path, normalized)


def _init_store(traceability_dir: Path, runner_events_path: Path, *, reset: bool = False) -> None:
    traceability_dir.mkdir(parents=True, exist_ok=True)
    for table_name in TABLE_NAMES:
        path = _table_path(traceability_dir, table_name)
        if reset or not path.exists():
            _write_json_atomic(path, {})
    _notify_traceability_changed(runner_events_path, "traceability_store_initialized")


def _get_requirement(traceability_dir: Path, req_id: str) -> dict[str, Any] | None:
    row = _read_table(traceability_dir, "requirements").get(str(req_id or "").strip())
    return dict(row) if isinstance(row, dict) else None


def _list_requirements(traceability_dir: Path) -> list[dict[str, Any]]:
    return [dict(row) for row in _read_table(traceability_dir, "requirements").values() if isinstance(row, dict)]


def _list_interfaces(traceability_dir: Path, *, req_id: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _read_table(traceability_dir, "interfaces").values() if isinstance(row, dict)]
    if req_id:
        rows = [row for row in rows if req_id in _as_str_list(row.get("req_ids"))]
    return rows


def _list_tests(traceability_dir: Path, *, req_id: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _read_table(traceability_dir, "tests").values() if isinstance(row, dict)]
    if req_id:
        rows = [row for row in rows if row.get("req_id") == req_id]
    return rows


def _list_call_edges(traceability_dir: Path, *, req_id: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _read_table(traceability_dir, "call_edges").values() if isinstance(row, dict)]
    if req_id:
        rows = [row for row in rows if row.get("source_req_id") == req_id or row.get("target_req_id") == req_id]
    return rows


def _list_node_states(traceability_dir: Path) -> list[dict[str, Any]]:
    return [dict(row) for row in _read_table(traceability_dir, "node_states").values() if isinstance(row, dict)]


def _upsert_requirement(traceability_dir: Path, runner_events_path: Path, **payload: Any) -> None:
    normalized_req_id = str(payload.get("req_id") or "").strip()
    if not normalized_req_id:
        raise ValueError("req_id is required")
    normalized_scenarios = [dict(item) for item in _as_list(payload.get("scenarios")) if isinstance(item, dict)]
    _upsert_row(
        traceability_dir,
        "requirements",
        normalized_req_id,
        {
            "req_id": normalized_req_id,
            "name": str(payload.get("name") or "").strip(),
            "description": str(payload.get("description") or "").strip(),
            "visual_reference": _as_str_list(payload.get("visual_reference")),
            "scenarios": normalized_scenarios,
            "parent_id": _as_optional_str(payload.get("parent_id")),
            "children_ids": _as_str_list(payload.get("children_ids")),
            "dependencies": _as_str_list(payload.get("dependencies")),
        },
    )
    scenarios_table = _read_table(traceability_dir, "scenarios")
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
    _write_table(traceability_dir, "scenarios", scenarios_table)
    _notify_traceability_changed(runner_events_path, "requirements_updated")


def _update_requirement_fields(traceability_dir: Path, runner_events_path: Path, req_id: str, **fields: Any) -> None:
    current = _get_requirement(traceability_dir, req_id)
    if current is None:
        raise ValueError(f"Requirement not found: {req_id}")
    merged = {**current, **fields}
    _upsert_requirement(traceability_dir, runner_events_path, **merged)


def _store_requirement_tree(traceability_dir: Path, runner_events_path: Path, requirement_tree: dict[str, Any]) -> None:
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
    _write_table(traceability_dir, "requirements", requirements)
    _write_table(traceability_dir, "scenarios", scenarios)
    _notify_traceability_changed(runner_events_path, "requirement_tree_stored")


def _upsert_scenario(traceability_dir: Path, runner_events_path: Path, **payload: Any) -> None:
    normalized_scenario_id = str(payload.get("scenario_id") or "").strip()
    normalized_req_id = str(payload.get("req_id") or "").strip()
    if not normalized_scenario_id or not normalized_req_id:
        raise ValueError("scenario_id and req_id are required")
    row = {
        "scenario_id": normalized_scenario_id,
        "name": str(payload.get("name") or "").strip(),
        "req_id": normalized_req_id,
        "steps": _as_list(payload.get("steps")),
    }
    _upsert_row(traceability_dir, "scenarios", normalized_scenario_id, row)
    requirement = _get_requirement(traceability_dir, normalized_req_id)
    if requirement:
        scenarios = [
            item
            for item in _as_list(requirement.get("scenarios"))
            if str(item.get("id") or item.get("scenario_id") or "").strip() != normalized_scenario_id
        ]
        scenarios.append({"id": normalized_scenario_id, "name": row["name"], "steps": row["steps"]})
        _update_requirement_fields(traceability_dir, runner_events_path, normalized_req_id, scenarios=scenarios)
    _notify_traceability_changed(runner_events_path, "scenarios_updated")


def _upsert_interface(traceability_dir: Path, runner_events_path: Path, **payload: Any) -> None:
    normalized_interface_id = str(payload.get("interface_id") or "").strip()
    if not normalized_interface_id:
        raise ValueError("interface_id is required")
    row = {
        "interface_id": normalized_interface_id,
        "req_ids": _as_str_list(payload.get("req_ids")),
        "type": str(payload.get("type") or "").strip(),
        "content": str(payload.get("content") or "").strip(),
        "file_path": _as_optional_str(payload.get("file_path")),
        "first_line": _as_optional_str(payload.get("first_line")),
        "implemented": bool(payload.get("implemented", False)),
        "callers": _as_str_list(payload.get("callers")),
        "callees": _as_str_list(payload.get("callees")),
    }
    _upsert_row(traceability_dir, "interfaces", normalized_interface_id, row)
    _emit_traceability_event(
        runner_events_path,
        {
            "type": "interface_upsert",
            "interface_id": normalized_interface_id,
            "req_ids": row["req_ids"],
            "interface_type": row["type"],
            "content": row["content"],
            "file_path": row["file_path"],
            "first_line": row["first_line"],
            "implemented": row["implemented"],
            "callers": row["callers"],
            "callees": row["callees"],
        },
    )


def _set_interface_implemented(
    traceability_dir: Path, runner_events_path: Path, interface_id: str, implemented: bool
) -> None:
    normalized_interface_id = str(interface_id or "").strip()
    rows = _read_table(traceability_dir, "interfaces")
    current = rows.get(normalized_interface_id)
    if not isinstance(current, dict):
        raise ValueError(f"Interface not found: {interface_id}")
    current["implemented"] = bool(implemented)
    _write_table(traceability_dir, "interfaces", rows)
    _emit_traceability_event(
        runner_events_path,
        {
            "type": "interface_status",
            "interface_id": normalized_interface_id,
            "implemented": bool(implemented),
            "message": None,
        },
    )


def _upsert_test(traceability_dir: Path, runner_events_path: Path, **payload: Any) -> None:
    normalized_test_id = str(payload.get("test_id") or "").strip()
    normalized_req_id = str(payload.get("req_id") or "").strip()
    if not normalized_test_id or not normalized_req_id:
        raise ValueError("test_id and req_id are required")
    existing = _read_table(traceability_dir, "tests").get(normalized_test_id)
    if isinstance(existing, dict) and existing.get("req_id") != normalized_req_id:
        raise ValueError(
            f"Test id collision detected for `{normalized_test_id}`: "
            f"existing req_id=`{existing.get('req_id')}`, new req_id=`{normalized_req_id}`."
        )
    row = {
        "test_id": normalized_test_id,
        "req_id": normalized_req_id,
        "interface_ids": _as_str_list(payload.get("interface_ids")),
        "type": str(payload.get("type") or "").strip(),
        "file_path": _as_optional_str(payload.get("file_path")),
        "passed": _as_bool_or_none(payload.get("passed")),
        "first_line": _as_optional_str(payload.get("first_line")),
        "scenario_id": _as_optional_str(payload.get("scenario_id")),
    }
    _upsert_row(traceability_dir, "tests", normalized_test_id, row)
    _emit_traceability_event(
        runner_events_path,
        {
            "type": "test_upsert",
            "test_id": normalized_test_id,
            "req_id": normalized_req_id,
            "scenario_id": row["scenario_id"],
            "test_type": row["type"],
            "file_path": row["file_path"],
            "first_line": row["first_line"],
            "interface_ids": row["interface_ids"],
        },
    )


def _set_test_status(traceability_dir: Path, runner_events_path: Path, test_id: str, passed: Any) -> None:
    normalized_test_id = str(test_id or "").strip()
    rows = _read_table(traceability_dir, "tests")
    current = rows.get(normalized_test_id)
    if not isinstance(current, dict):
        raise ValueError(f"Test not found: {test_id}")
    current["passed"] = _as_bool_or_none(passed)
    _write_table(traceability_dir, "tests", rows)
    _notify_traceability_changed(runner_events_path, "test_status_updated")


def _set_test_statuses(traceability_dir: Path, runner_events_path: Path, status_by_test_id: dict[str, Any]) -> None:
    rows = _read_table(traceability_dir, "tests")
    for test_id, passed in status_by_test_id.items():
        key = str(test_id or "").strip()
        row = rows.get(key)
        if isinstance(row, dict):
            row["passed"] = _as_bool_or_none(passed)
    _write_table(traceability_dir, "tests", rows)
    _notify_traceability_changed(runner_events_path, "test_statuses_updated")


def _insert_call_edge(traceability_dir: Path, runner_events_path: Path, **payload: Any) -> None:
    source_req_id = str(payload.get("source_req_id") or "").strip()
    target_req_id = str(payload.get("target_req_id") or "").strip()
    from_interface_id = str(payload.get("from_interface_id") or "").strip()
    to_interface_id = str(payload.get("to_interface_id") or "").strip()
    key = _edge_key(source_req_id, target_req_id, from_interface_id, to_interface_id)
    _upsert_row(
        traceability_dir,
        "call_edges",
        key,
        {
            "source_req_id": source_req_id,
            "target_req_id": target_req_id,
            "from_interface_id": from_interface_id,
            "to_interface_id": to_interface_id,
            "edge_type": str(payload.get("edge_type") or "").strip() or "parent_child",
            "created_at": _utc_timestamp(),
        },
    )
    _notify_traceability_changed(runner_events_path, "call_edges_updated")


def _upsert_node_state(
    traceability_dir: Path, runner_events_path: Path, req_id: str, state: str, phase: str | None = None
) -> None:
    normalized_req_id = str(req_id or "").strip()
    normalized_state = str(state or "").strip()
    if not normalized_req_id:
        raise ValueError("req_id is required")
    _upsert_row(
        traceability_dir,
        "node_states",
        normalized_req_id,
        {
            "req_id": normalized_req_id,
            "state": normalized_state,
            "phase": _as_optional_str(phase),
            "updated_at": _utc_timestamp(),
        },
    )
    _notify_traceability_changed(runner_events_path, "node_state_updated")


def _upsert_node_contract(
    traceability_dir: Path, runner_events_path: Path, req_id: str, content: dict[str, Any]
) -> None:
    normalized_req_id = str(req_id or "").strip()
    _upsert_row(
        traceability_dir,
        "node_contracts",
        normalized_req_id,
        {
            "req_id": normalized_req_id,
            "content": content if isinstance(content, dict) else {},
            "updated_at": _utc_timestamp(),
        },
    )
    _notify_traceability_changed(runner_events_path, "node_contract_updated")


def _clear_node_design_artifacts(traceability_dir: Path, runner_events_path: Path, req_id: str) -> None:
    interfaces = _read_table(traceability_dir, "interfaces")
    for key, row in list(interfaces.items()):
        if not isinstance(row, dict):
            continue
        remaining_req_ids = [value for value in _as_str_list(row.get("req_ids")) if value != req_id]
        if remaining_req_ids:
            row["req_ids"] = remaining_req_ids
        else:
            interfaces.pop(key, None)
    _write_table(traceability_dir, "interfaces", interfaces)

    tests = _read_table(traceability_dir, "tests")
    tests = {key: row for key, row in tests.items() if not isinstance(row, dict) or row.get("req_id") != req_id}
    _write_table(traceability_dir, "tests", tests)

    call_edges = _read_table(traceability_dir, "call_edges")
    call_edges = {
        key: row
        for key, row in call_edges.items()
        if not isinstance(row, dict) or (row.get("source_req_id") != req_id and row.get("target_req_id") != req_id)
    }
    _write_table(traceability_dir, "call_edges", call_edges)
    _notify_traceability_changed(runner_events_path, "design_artifacts_cleared")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read/write ARC Bench traceability tables.")
    parser.add_argument("action")
    parser.add_argument("--project-dir")
    parser.add_argument("--events-path")
    parser.add_argument("--traceability-dir")
    parser.add_argument("--payload-json")
    parser.add_argument("--payload-file")
    parser.add_argument("--req-id")
    parser.add_argument("--scenario-id")
    parser.add_argument("--interface-id")
    parser.add_argument("--test-id")
    parser.add_argument("--state")
    parser.add_argument("--phase")
    parser.add_argument("--passed")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    _, runner_events_path, traceability_dir = _paths(args.project_dir, args.events_path, args.traceability_dir)
    action = args.action.strip().lower()
    payload = _payload(args)

    if action == "init":
        _init_store(traceability_dir, runner_events_path, reset=args.reset)
        result = {"ok": True, "traceability_dir": str(traceability_dir)}
    elif action == "store-requirement-tree":
        _store_requirement_tree(traceability_dir, runner_events_path, payload)
        result = {"ok": True}
    elif action == "upsert-requirement":
        _upsert_requirement(traceability_dir, runner_events_path, **payload)
        result = {"ok": True}
    elif action == "upsert-scenario":
        _upsert_scenario(traceability_dir, runner_events_path, **payload)
        result = {"ok": True}
    elif action == "upsert-interface":
        _upsert_interface(traceability_dir, runner_events_path, **payload)
        result = {"ok": True}
    elif action == "set-interface-implemented":
        interface_id = args.interface_id or payload.get("interface_id")
        implemented = payload.get("implemented", True)
        _set_interface_implemented(traceability_dir, runner_events_path, interface_id, bool(implemented))
        result = {"ok": True}
    elif action == "upsert-test":
        _upsert_test(traceability_dir, runner_events_path, **payload)
        result = {"ok": True}
    elif action == "set-test-status":
        test_id = args.test_id or payload.get("test_id")
        passed = args.passed if args.passed is not None else payload.get("passed")
        _set_test_status(traceability_dir, runner_events_path, test_id, passed)
        result = {"ok": True}
    elif action == "set-test-statuses":
        _set_test_statuses(traceability_dir, runner_events_path, payload)
        result = {"ok": True}
    elif action == "insert-call-edge":
        _insert_call_edge(traceability_dir, runner_events_path, **payload)
        result = {"ok": True}
    elif action == "upsert-node-state":
        req_id = args.req_id or payload.get("req_id")
        state = args.state or payload.get("state")
        phase = args.phase if args.phase is not None else payload.get("phase")
        _upsert_node_state(traceability_dir, runner_events_path, req_id, state, phase=phase)
        result = {"ok": True}
    elif action == "upsert-node-contract":
        _upsert_node_contract(traceability_dir, runner_events_path, payload["req_id"], payload.get("content", {}))
        result = {"ok": True}
    elif action == "clear-node-design-artifacts":
        _clear_node_design_artifacts(traceability_dir, runner_events_path, args.req_id or payload.get("req_id"))
        result = {"ok": True}
    elif action == "get-requirement":
        result = _get_requirement(traceability_dir, args.req_id or payload.get("req_id"))
    elif action == "list-requirements":
        result = _list_requirements(traceability_dir)
    elif action == "list-interfaces":
        result = _list_interfaces(traceability_dir, req_id=args.req_id)
    elif action == "list-tests":
        result = _list_tests(traceability_dir, req_id=args.req_id)
    elif action == "list-call-edges":
        result = _list_call_edges(traceability_dir, req_id=args.req_id)
    elif action == "list-node-states":
        result = _list_node_states(traceability_dir)
    else:
        raise SystemExit(f"Unsupported action: {args.action}")

    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
