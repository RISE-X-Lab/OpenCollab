#!/usr/bin/env python3
"""Create an instance-level loop monitor report for SWE-bench runs."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.adapters.safe_files import (  # noqa: E402
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    regular_path_identity,
    write_regular_bytes_atomic,
)

WRITE_TOOLS = {"file_write", "apply_patch"}
WARN_LOOP_COUNT = 5
CRITICAL_LOOP_COUNT = 10
CRITICAL_TEXT_REPEAT = 3
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
MAX_EVENT_JSONL_BYTES = 256 * 1024 * 1024
MAX_EVENT_TOTAL_BYTES = 512 * 1024 * 1024
MAX_EVENT_RECORDS_PER_FILE = 1_000_000
MAX_EVENT_TOTAL_RECORDS = 2_000_000
MAX_SESSION_JSON_BYTES = 16 * 1024 * 1024
MAX_SESSION_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RETAINED_MESSAGES = 2_000
MAX_MESSAGE_TEXT_CHARS = 8_000
MAX_TOOL_ARGUMENT_CHARS = 4_000
MAX_TOOL_CALLS_PER_MESSAGE = 50
MAX_RETAINED_TOOL_CALLS = 2_000
MAX_ACTIVE_ASSISTANTS = 256
MAX_ACTIVE_TEXT_CHARS = 64_000
MAX_SENTENCE_KEYS = 20_000
MAX_SENTENCE_CHARS = 2_048
MAX_TOOL_KEYS = 1_024
MAX_TAIL_READ_BYTES = 4 * 1024 * 1024
MAX_DIFF_BYTES = 64 * 1024 * 1024
MAX_EVENT_FILES = 1_024
MAX_SESSION_FILES = 4_096
MAX_SESSION_TREE_ENTRIES = 20_000


@contextmanager
def _open_regular_binary(path: Path) -> Iterator[BinaryIO]:
    """Open one stable regular input without waiting on FIFOs or devices."""
    path = _lexical_absolute(path)
    parent_fd = _open_directory_no_symlinks(path.parent)
    fd = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"monitor input is not a regular file: {path}")
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"monitor input is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"monitor input changed while opening: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            yield handle
            after = os.fstat(handle.fileno())
            current = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if opened_identity != after_identity or after_identity != current_identity:
                raise OSError(f"monitor input changed while reading: {path}")
            if not _directory_path_matches_fd(path.parent, parent_fd):
                raise OSError(f"monitor input parent changed while reading: {path}")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _read_regular_bytes(
    path: Path,
    max_bytes: int,
    *,
    expected_size: int | None = None,
) -> bytes:
    with _open_regular_binary(path) as handle:
        before = os.fstat(handle.fileno())
        if expected_size is not None and before.st_size != expected_size:
            raise OSError(f"input changed between accounting and read: {path}")
        if before.st_size > max_bytes:
            raise ValueError(f"input exceeds {max_bytes}-byte limit: {path}")
        payload = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    if len(payload) > max_bytes:
        raise ValueError(f"input exceeds {max_bytes}-byte limit: {path}")
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        raise OSError(f"input changed while reading: {path}")
    return payload


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    target = _lexical_absolute(path)
    ensure_directory_no_symlinks(target.parent)
    write_regular_bytes_atomic(target, payload)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _session_root_path(value: str) -> Path:
    path = _lexical_absolute(Path(value))
    try:
        inspected = path.lstat()
    except FileNotFoundError:
        parent_fd = _open_directory_no_symlinks(path.parent)
        os.close(parent_fd)
        return path
    if not stat.S_ISDIR(inspected.st_mode):
        raise ValueError(f"session root is not a real directory: {path}")
    directory_fd = _open_directory_no_symlinks(path)
    os.close(directory_fd)
    return path


def _read_tail_text(path: Path, max_bytes: int) -> str:
    try:
        max_bytes = max(0, int(max_bytes))
    except (TypeError, ValueError, OverflowError):
        return ""
    max_bytes = min(max_bytes, MAX_TAIL_READ_BYTES)
    if max_bytes == 0:
        return ""
    try:
        with _open_regular_binary(path) as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), 0)
            return handle.read(max_bytes).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _load_json(path: Path) -> Any | None:
    try:
        return _load_json_strict(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _load_json_strict(path: Path, *, expected_size: int | None = None) -> Any:
    data = _read_regular_bytes(
        path,
        MAX_SESSION_JSON_BYTES,
        expected_size=expected_size,
    )
    return json.loads(data.decode("utf-8"))


def _iter_jsonl(path: Path, *, expected_size: int | None = None):
    with _open_regular_binary(path) as handle:
        opened_size = os.fstat(handle.fileno()).st_size
        if expected_size is not None and opened_size != expected_size:
            raise OSError(f"event input changed between accounting and read: {path}")
        if opened_size > MAX_EVENT_JSONL_BYTES:
            raise ValueError(
                f"event JSONL exceeds {MAX_EVENT_JSONL_BYTES}-byte limit: {path}"
            )
        bytes_read = 0
        records_read = 0
        while True:
            line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
            if not line:
                break
            bytes_read += len(line)
            if bytes_read > MAX_EVENT_JSONL_BYTES:
                raise ValueError(
                    f"event JSONL exceeds {MAX_EVENT_JSONL_BYTES}-byte limit: {path}"
                )
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise ValueError(
                    "event JSONL record exceeds "
                    f"{MAX_JSONL_LINE_BYTES}-byte limit: {path}"
                )
            if not line.strip():
                continue
            records_read += 1
            if records_read > MAX_EVENT_RECORDS_PER_FILE:
                raise ValueError(
                    "event JSONL records exceed limit of "
                    f"{MAX_EVENT_RECORDS_PER_FILE}: {path}"
                )
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid event JSONL record: {path}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"event JSONL record is not an object: {path}")
            yield obj


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or "")


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _event_paths(session_root: Path, explicit: list[str]) -> list[Path]:
    paths, _errors = _event_paths_status(session_root, explicit)
    return paths


def _event_paths_status(
    session_root: Path,
    explicit: list[str],
) -> tuple[list[Path], list[str]]:
    paths = [Path(item) for item in explicit if item]
    explicit_count = len(paths)
    errors: list[str] = []
    if len(paths) > MAX_EVENT_FILES:
        raise ValueError(f"explicit event files exceed limit of {MAX_EVENT_FILES}")
    try:
        with os.scandir(session_root) as entries:
            scanned = 0
            for entry in entries:
                scanned += 1
                if scanned > MAX_SESSION_TREE_ENTRIES:
                    raise ValueError(
                        "session root entries exceed limit of "
                        f"{MAX_SESSION_TREE_ENTRIES}"
                    )
                try:
                    is_candidate = entry.name == "events.jsonl" or fnmatch.fnmatch(
                        entry.name,
                        "*.events.jsonl",
                    )
                except OSError as exc:
                    errors.append(
                        f"{entry.path}: {type(exc).__name__}: {exc}"
                    )
                    continue
                if is_candidate:
                    paths.append(Path(entry.path))
                    if len(paths) > MAX_EVENT_FILES:
                        raise ValueError(
                            f"event files exceed limit of {MAX_EVENT_FILES}"
                        )
    except OSError as exc:
        errors.append(f"{session_root}: {type(exc).__name__}: {exc}")

    seen: set[Path] = set()
    unique: list[Path] = []
    for index, path in enumerate(paths):
        path = _lexical_absolute(path)
        if path in seen:
            continue
        try:
            path.lstat()
        except OSError as exc:
            if index < explicit_count:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        seen.add(path)
        unique.append(path)
    return sorted(unique), errors


def _session_paths_status(session_root: Path) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    errors: list[str] = []
    pending = [session_root]
    scanned = 0
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            errors.append(f"{directory}: {type(exc).__name__}: {exc}")
            continue
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SESSION_TREE_ENTRIES:
                    raise ValueError(
                        "session tree entries exceed limit of "
                        f"{MAX_SESSION_TREE_ENTRIES}"
                    )
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                except OSError as exc:
                    errors.append(
                        f"{entry.path}: {type(exc).__name__}: {exc}"
                    )
                    continue
                if not fnmatch.fnmatch(entry.name, "agent_*.json"):
                    continue
                paths.append(Path(entry.path))
                if len(paths) > MAX_SESSION_FILES:
                    raise ValueError(
                        f"session files exceed limit of {MAX_SESSION_FILES}"
                    )
    return sorted(paths), errors


def _session_paths(session_root: Path) -> list[Path]:
    paths, _errors = _session_paths_status(session_root)
    return paths


def _compact_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"id": str(call.get("id") or "")[:500]}
    function = call.get("function")
    if isinstance(function, dict):
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = arguments[:MAX_TOOL_ARGUMENT_CHARS]
        else:
            arguments = _truncate_obj(arguments, MAX_TOOL_ARGUMENT_CHARS)
        compact["function"] = {
            "name": str(function.get("name") or "")[:500],
            "arguments": arguments,
        }
    return compact


def _compact_message(
    msg: dict[str, Any],
    *,
    path: Path,
    index: int,
    role: str,
    aid: Any,
) -> dict[str, Any]:
    content = _plain_text(msg.get("content"))[-MAX_MESSAGE_TEXT_CHARS:]
    compact: dict[str, Any] = {
        "role": str(msg.get("role") or "")[:500],
        "content": content,
        "tool_call_id": str(msg.get("tool_call_id") or "")[:500],
        "_source_file": str(path),
        "_message_index": index,
        "_agent_role": role,
        "_aid": aid,
    }
    raw_calls = msg.get("tool_calls")
    if isinstance(raw_calls, list):
        compact["tool_calls"] = [
            _compact_tool_call(call)
            for call in raw_calls[-MAX_TOOL_CALLS_PER_MESSAGE:]
            if isinstance(call, dict)
        ]
    return compact


def _session_messages(session_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    messages, used, _errors, _discovered = _session_messages_status(session_root)
    return messages, used


def _session_messages_status(
    session_root: Path,
) -> tuple[list[dict[str, Any]], list[str], list[str], int]:
    messages: deque[dict[str, Any]] = deque()
    retained_tool_calls = 0
    used: list[str] = []
    errors: list[str] = []
    paths, discovery_errors = _session_paths_status(session_root)
    errors.extend(discovery_errors)
    total_bytes = 0
    for path in paths:
        try:
            _dev, _ino, file_bytes, _mtime, _ctime = regular_path_identity(path)
            total_bytes += file_bytes
            if total_bytes > MAX_SESSION_TOTAL_BYTES:
                raise ValueError(
                    "session inputs exceed total byte limit of "
                    f"{MAX_SESSION_TOTAL_BYTES}"
                )
            obj = _load_json_strict(path, expected_size=file_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{path}: session JSON is not an object")
            continue
        role = str(obj.get("role") or "")[:500]
        aid = str(obj.get("aid") or "")[:500]
        raw_messages = obj.get("messages")
        if not isinstance(raw_messages, list):
            errors.append(f"{path}: session messages is not a list")
            continue
        used.append(str(path))
        start_index = max(0, len(raw_messages) - MAX_RETAINED_MESSAGES)
        for index, msg in enumerate(
            raw_messages[start_index:],
            start=start_index,
        ):
            if isinstance(msg, dict):
                compact = _compact_message(
                    msg,
                    path=path,
                    index=index,
                    role=role,
                    aid=aid,
                )
                messages.append(compact)
                retained_tool_calls += len(compact.get("tool_calls") or [])
                while (
                    len(messages) > MAX_RETAINED_MESSAGES
                    or retained_tool_calls > MAX_RETAINED_TOOL_CALLS
                ):
                    removed = messages.popleft()
                    retained_tool_calls -= len(removed.get("tool_calls") or [])
    return list(messages), used, errors, len(paths)


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


def _update_sentence_counter(counter: Counter[str], text: str) -> None:
    for sentence in _sentences(text):
        sentence = sentence[:MAX_SENTENCE_CHARS]
        if sentence in counter or len(counter) < MAX_SENTENCE_KEYS:
            counter[sentence] += 1


def _text_report_from_counter(
    sentence_counter: Counter[str],
    recent_texts: list[dict[str, Any]],
) -> dict[str, Any]:
    repeated = [
        {
            "count": count,
            "sha1": hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:12],
            "text": sentence[:500],
        }
        for sentence, count in sentence_counter.most_common()
        if count > 1
    ]
    return {
        "max_repeated_sentence_count": repeated[0]["count"] if repeated else 0,
        "repeated_sentences": repeated[:10],
        "recent_assistant_texts": recent_texts[-3:],
    }


def _discover_event_analysis(
    session_root: Path,
    explicit: list[str],
) -> tuple[dict[str, Any], list[str]]:
    loop_count = 0
    max_tool_count = 0
    by_tool: Counter[str] = Counter()
    recent_loops: deque[dict[str, Any]] = deque(maxlen=5)
    recent_errors: deque[dict[str, Any]] = deque(maxlen=3)
    sentence_counter: Counter[str] = Counter()
    recent_texts: deque[dict[str, Any]] = deque(maxlen=3)
    active: dict[Any, str] = {}
    assistant_text_count = 0
    used: list[str] = []
    event_paths, input_errors = _event_paths_status(session_root, explicit)
    total_bytes = 0
    total_records = 0

    def flush(aid: Any) -> None:
        nonlocal assistant_text_count
        text = active.pop(aid, "").strip()
        if not text:
            return
        assistant_text_count += 1
        _update_sentence_counter(sentence_counter, text)
        recent_texts.append(
            {
                "aid": aid,
                "role": None,
                "source_file": "events",
                "message_index": None,
                "text": text,
            }
        )

    for path in event_paths:
        saw_event = False
        try:
            with _open_regular_binary(path) as handle:
                file_bytes = os.fstat(handle.fileno()).st_size
            total_bytes += file_bytes
            if total_bytes > MAX_EVENT_TOTAL_BYTES:
                raise ValueError(
                    "event inputs exceed total byte limit of "
                    f"{MAX_EVENT_TOTAL_BYTES}"
                )
            for event in _iter_jsonl(path, expected_size=file_bytes):
                total_records += 1
                if total_records > MAX_EVENT_TOTAL_RECORDS:
                    raise ValueError(
                        "event inputs exceed total record limit of "
                        f"{MAX_EVENT_TOTAL_RECORDS}"
                    )
                saw_event = True
                etype = _event_type(event)
                data = _event_data(event)
                aid = str(data.get("aid") or "")[:500]
                if etype == "loop_detected":
                    loop_count += 1
                    tool = str(data.get("tool") or "unknown")[:500]
                    if tool not in by_tool and len(by_tool) >= MAX_TOOL_KEYS:
                        tool = "other"
                    by_tool[tool] += 1
                    try:
                        max_tool_count = max(
                            max_tool_count,
                            int(data.get("count") or 0),
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
                    recent_loops.append(_truncate_obj(event, 4000))
                if etype in {"error", "tool_error"}:
                    recent_errors.append(
                        {
                            "source": "event",
                            "type": etype,
                            "data": _truncate_obj(data),
                        }
                    )
                if etype == "text_delta":
                    if aid not in active and len(active) >= MAX_ACTIVE_ASSISTANTS:
                        flush(next(iter(active)))
                    content = str(data.get("content") or "")[
                        -MAX_ACTIVE_TEXT_CHARS:
                    ]
                    active[aid] = (active.get(aid, "") + content)[
                        -MAX_ACTIVE_TEXT_CHARS:
                    ]
                elif etype in {
                    "step_end",
                    "tool_start",
                    "error",
                    "agent_completed",
                }:
                    flush(aid)
        except (OSError, ValueError) as exc:
            input_errors.append(f"{path}: {type(exc).__name__}: {exc}")
        for aid in list(active):
            flush(aid)
        if saw_event:
            used.append(str(path))

    loop_report = {
        "loop_detected_count": loop_count,
        "max_tool_loop_count": max_tool_count,
        "loop_events_by_tool": dict(by_tool),
        "recent_loop_events": list(recent_loops),
    }
    text_report = _text_report_from_counter(
        sentence_counter,
        list(recent_texts),
    )
    return {
        "loop": loop_report,
        "text": text_report,
        "assistant_text_count": assistant_text_count,
        "recent_event_errors": list(recent_errors),
        "input_errors": input_errors,
    }, used


def _assistant_text_report(messages: list[dict[str, Any]]) -> dict[str, Any]:
    assistant_texts: list[dict[str, Any]] = []
    sentence_counter: Counter[str] = Counter()
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
        _update_sentence_counter(sentence_counter, text)

    return _text_report_from_counter(sentence_counter, assistant_texts[-3:])


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


def _write_and_error_report(messages: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    last_write: dict[str, Any] | None = None
    recent_errors: list[dict[str, Any]] = []

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


def _artifact_dir(output_path: Path) -> Path:
    return output_path.with_suffix("").parent / f"{output_path.with_suffix('').name}_artifacts"


def _write_critical_artifacts(
    output_path: Path,
    *,
    diff_payload: bytes | None,
    write_report: dict[str, Any],
    text_report: dict[str, Any],
) -> dict[str, str]:
    artifacts = _artifact_dir(output_path)
    written: dict[str, str] = {}
    if diff_payload is not None:
        dest = artifacts / "current_git_diff.patch"
        _atomic_write_bytes(dest, diff_payload)
        written["current_git_diff"] = str(dest)
    for name, value in {
        "last_successful_write": write_report.get("last_successful_write"),
        "recent_tool_errors": write_report.get("recent_tool_errors"),
        "recent_assistant_texts": text_report.get("recent_assistant_texts"),
    }.items():
        dest = artifacts / f"{name}.json"
        payload = (
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(dest, payload)
        written[name] = str(dest)
    return written


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    session_root = _session_root_path(args.session_root)
    event_analysis, event_files = _discover_event_analysis(
        session_root,
        args.events_file or [],
    )
    (
        messages,
        session_files,
        session_input_errors,
        session_files_discovered,
    ) = _session_messages_status(session_root)

    loop = event_analysis["loop"]
    if event_analysis["assistant_text_count"]:
        text = event_analysis["text"]
    else:
        text = _assistant_text_report(messages)
    writes = _write_and_error_report(messages)
    writes["recent_tool_errors"] = (
        event_analysis["recent_event_errors"]
        + writes["recent_tool_errors"]
    )[-3:]

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
    diff_payload: bytes | None = None
    diff_input_error: str | None = None
    if args.diff_file:
        diff_path = _lexical_absolute(Path(args.diff_file))
        try:
            diff_payload = _read_regular_bytes(diff_path, MAX_DIFF_BYTES)
        except (OSError, ValueError) as exc:
            diff_input_error = f"{type(exc).__name__}: {exc}"
    diff_bytes = len(diff_payload) if diff_payload is not None else 0
    input_errors = [*event_analysis["input_errors"], *session_input_errors]
    if diff_input_error is not None:
        input_errors.append(diff_input_error)
    # A diff is a review artifact.  It contains no execution/event evidence, so
    # a diff-only invocation must remain visibly incomplete.
    has_observation_input = bool(event_files or session_files)
    if input_errors or not has_observation_input:
        if level == "ok":
            level = "warn"
        reasons.append("input_incomplete")

    report = {
        "instance_id": args.instance_id,
        "level": level,
        "reasons": reasons,
        "session_root": str(session_root),
        "event_files": event_files,
        "session_files": session_files,
        "session_files_discovered": session_files_discovered,
        "diff_bytes": diff_bytes,
        "diff_input_error": diff_input_error,
        "input_complete": not input_errors and has_observation_input,
        "input_errors": input_errors,
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
            diff_payload=diff_payload,
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
    payload = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(output, payload)
    print(
        f"level={report['level']} "
        f"loops={report['loop_detected_count']} "
        f"max_sentence_repeat={report['max_repeated_sentence_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
