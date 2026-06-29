#!/usr/bin/env python3
"""Create an instance-level loop monitor report for SWE-bench runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


WRITE_TOOLS = {"file_write", "apply_patch"}
WARN_LOOP_COUNT = 5
CRITICAL_LOOP_COUNT = 10
CRITICAL_TEXT_REPEAT = 3


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or "")


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _discover_events(session_root: Path, explicit: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    paths: list[Path] = []
    for item in explicit:
        if item:
            paths.append(Path(item))
    paths.extend(sorted(session_root.glob("*.events.jsonl")))
    default_path = session_root / "events.jsonl"
    if default_path.exists():
        paths.append(default_path)

    seen: set[Path] = set()
    events: list[dict[str, Any]] = []
    used: list[str] = []
    for path in paths:
        path = path.resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        rows = _load_jsonl(path)
        if rows:
            used.append(str(path))
            events.extend(rows)
    return events, used


def _session_messages(session_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    messages: list[dict[str, Any]] = []
    used: list[str] = []
    for path in sorted(session_root.rglob("agent_*.json")):
        obj = _load_json(path)
        if not isinstance(obj, dict):
            continue
        role = str(obj.get("role") or "")
        aid = obj.get("aid")
        raw_messages = obj.get("messages")
        if not isinstance(raw_messages, list):
            continue
        used.append(str(path))
        for index, msg in enumerate(raw_messages):
            if isinstance(msg, dict):
                copy = dict(msg)
                copy["_source_file"] = str(path)
                copy["_message_index"] = index
                copy["_agent_role"] = role
                copy["_aid"] = aid
                messages.append(copy)
    return messages, used


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if value is None else str(value)


def _normalize_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"[\n。！？!?]+|(?<=[a-z0-9\)])\.\s+", text)
    out: list[str] = []
    for chunk in chunks:
        norm = _normalize_text(chunk)
        if len(norm) >= 40:
            out.append(norm)
    return out


def _assistant_texts_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active: dict[Any, list[str]] = {}
    texts: list[dict[str, Any]] = []

    def flush(aid: Any) -> None:
        parts = active.pop(aid, [])
        text = "".join(parts).strip()
        if text:
            texts.append({"aid": aid, "role": None, "source_file": "events", "message_index": None, "text": text})

    for event in events:
        etype = _event_type(event)
        data = _event_data(event)
        aid = data.get("aid")
        if etype == "text_delta":
            active.setdefault(aid, []).append(str(data.get("content") or ""))
        elif etype in {"step_end", "tool_start", "error", "agent_completed"}:
            flush(aid)
    for aid in list(active):
        flush(aid)
    return texts


def _assistant_text_report(messages: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    assistant_texts: list[dict[str, Any]] = []
    sentence_counter: Counter[str] = Counter()
    event_texts = _assistant_texts_from_events(events)
    if event_texts:
        assistant_texts = event_texts
    else:
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            text = _plain_text(msg.get("content"))
            if not text.strip():
                continue
            assistant_texts.append({
                "aid": msg.get("_aid"),
                "role": msg.get("_agent_role"),
                "source_file": msg.get("_source_file"),
                "message_index": msg.get("_message_index"),
                "text": text[-2000:],
            })
    for item in assistant_texts:
        text = str(item.get("text") or "")
        sentence_counter.update(_sentences(text))

    repeated = [
        {
            "count": count,
            "sha1": hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:12],
            "text": sentence[:500],
        }
        for sentence, count in sentence_counter.most_common()
        if count > 1
    ]
    max_repeat = repeated[0]["count"] if repeated else 0
    return {
        "max_repeated_sentence_count": max_repeat,
        "repeated_sentences": repeated[:10],
        "recent_assistant_texts": assistant_texts[-3:],
    }


def _tool_call_name(call: dict[str, Any]) -> str:
    fn = call.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return ""


def _tool_call_args(call: dict[str, Any]) -> Any:
    fn = call.get("function")
    raw = fn.get("arguments") if isinstance(fn, dict) else None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[:1000]
    return raw


def _truncate_obj(value: Any, limit: int = 1600) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return json.loads(text)
    return text[:limit] + "...[truncated]"


def _looks_successful_tool_result(content: str) -> bool:
    head = content.strip().lower()[:300]
    if not head:
        return False
    if head.startswith("error:") or "traceback" in head:
        return False
    if re.search(r"\b(exit code|return code):\s*[1-9]", head):
        return False
    return True


def _write_and_error_report(messages: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    last_write: dict[str, Any] | None = None
    recent_errors: list[dict[str, Any]] = []

    for event in events:
        etype = _event_type(event)
        if etype not in {"error", "tool_error"}:
            continue
        recent_errors.append({
            "source": "event",
            "type": etype,
            "data": _event_data(event),
        })

    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                calls[call_id] = {
                    "tool": _tool_call_name(call),
                    "args": _truncate_obj(_tool_call_args(call)),
                    "aid": msg.get("_aid"),
                    "role": msg.get("_agent_role"),
                    "source_file": msg.get("_source_file"),
                    "message_index": msg.get("_message_index"),
                }
        elif msg.get("role") == "tool":
            content = _plain_text(msg.get("content"))
            call_id = str(msg.get("tool_call_id") or "")
            call = calls.get(call_id)
            if content.strip().startswith("Error:") or "Traceback" in content[:1000]:
                recent_errors.append({
                    "source": "tool_message",
                    "tool": call.get("tool") if call else None,
                    "content": content[:1200],
                })
            if call and call.get("tool") in WRITE_TOOLS and _looks_successful_tool_result(content):
                last_write = {
                    **call,
                    "tool_result": content[:1200],
                }

    return {
        "last_successful_write": last_write,
        "recent_tool_errors": recent_errors[-3:],
    }


def _loop_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    loop_events = [event for event in events if _event_type(event) == "loop_detected"]
    max_tool_count = 0
    by_tool: Counter[str] = Counter()
    for event in loop_events:
        data = _event_data(event)
        tool = str(data.get("tool") or "unknown")
        by_tool[tool] += 1
        try:
            max_tool_count = max(max_tool_count, int(data.get("count") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "loop_detected_count": len(loop_events),
        "max_tool_loop_count": max_tool_count,
        "loop_events_by_tool": dict(by_tool),
        "recent_loop_events": loop_events[-5:],
    }


def _artifact_dir(output_path: Path) -> Path:
    return output_path.with_suffix("").parent / f"{output_path.with_suffix('').name}_artifacts"


def _write_critical_artifacts(
    output_path: Path,
    *,
    diff_file: str | None,
    write_report: dict[str, Any],
    text_report: dict[str, Any],
) -> dict[str, str]:
    artifacts = _artifact_dir(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    if diff_file and Path(diff_file).exists():
        dest = artifacts / "current_git_diff.patch"
        shutil.copyfile(diff_file, dest)
        written["current_git_diff"] = str(dest)
    for name, value in {
        "last_successful_write": write_report.get("last_successful_write"),
        "recent_tool_errors": write_report.get("recent_tool_errors"),
        "recent_assistant_texts": text_report.get("recent_assistant_texts"),
    }.items():
        dest = artifacts / f"{name}.json"
        dest.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        written[name] = str(dest)
    return written


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    session_root = Path(args.session_root).resolve()
    events, event_files = _discover_events(session_root, args.events_file or [])
    messages, session_files = _session_messages(session_root)

    loop = _loop_report(events)
    text = _assistant_text_report(messages, events)
    writes = _write_and_error_report(messages, events)

    loop_count = loop["loop_detected_count"]
    repeat_count = text["max_repeated_sentence_count"]
    level = "ok"
    reasons: list[str] = []
    if loop_count > CRITICAL_LOOP_COUNT:
        level = "critical"
        reasons.append(f"loop_detected_count>{CRITICAL_LOOP_COUNT}")
    if repeat_count > CRITICAL_TEXT_REPEAT:
        level = "critical"
        reasons.append(f"assistant_sentence_repeat>{CRITICAL_TEXT_REPEAT}")
    if level == "ok" and loop_count > WARN_LOOP_COUNT:
        level = "warn"
        reasons.append(f"loop_detected_count>{WARN_LOOP_COUNT}")

    diff_bytes = 0
    if args.diff_file and Path(args.diff_file).exists():
        diff_bytes = Path(args.diff_file).stat().st_size

    report = {
        "instance_id": args.instance_id,
        "level": level,
        "reasons": reasons,
        "session_root": str(session_root),
        "event_files": event_files,
        "session_files": session_files,
        "diff_bytes": diff_bytes,
        **loop,
        **text,
        **writes,
        "suggested_action": "",
    }
    if level == "critical":
        report["suggested_action"] = (
            "Stop the current verification action and ask a fresh role to review "
            "the saved diff, recent write, recent tool errors, and recent assistant text."
        )
        report["artifacts"] = _write_critical_artifacts(
            Path(args.output),
            diff_file=args.diff_file,
            write_report=writes,
            text_report=text,
        )
    elif level == "warn":
        report["suggested_action"] = "Review the instance summary before spending more budget."
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--events-file", action="append", default=[])
    parser.add_argument("--diff-file")
    args = parser.parse_args()

    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"level={report['level']} "
        f"loops={report['loop_detected_count']} "
        f"max_sentence_repeat={report['max_repeated_sentence_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
