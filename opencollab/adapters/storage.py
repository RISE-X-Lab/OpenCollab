from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO

from opencollab.adapters.safe_files import (
    ensure_directory_no_symlinks,
    open_regular_text_append,
    read_regular_bytes,
    write_locked_text,
    write_regular_bytes_atomic,
    write_regular_file_atomic,
)

MAX_SESSION_SNAPSHOT_BYTES = 64 * 1024 * 1024
_JSON_WRITE_CHUNK_CHARS = 64 * 1024
_AUTOSAVE_SEQUENCE_KEY = "_autosave_sequence"
_AUTOSAVE_JOURNAL_VERSION = 1
_AUTOSAVE_JOURNAL_SUFFIX = ".journal"
_AUTOSAVE_JOURNAL_LOCK_SUFFIX = ".lock"
_JOURNAL_LOCKS: dict[str, threading.RLock] = {}
_JOURNAL_LOCKS_GUARD = threading.Lock()


def _journal_lock(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _JOURNAL_LOCKS_GUARD:
        return _JOURNAL_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _journal_operation_lock(path: str) -> Iterator[None]:
    """Serialize compaction and append on a stable cross-process lockfile."""
    with _journal_lock(path):
        lock_path = f"{path}{_AUTOSAVE_JOURNAL_SUFFIX}{_AUTOSAVE_JOURNAL_LOCK_SUFFIX}"
        with open_regular_text_append(lock_path) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _BoundedUTF8Writer:
    """Text writer used by ``json.dump`` with an exact UTF-8 byte budget."""

    def __init__(self, raw: BinaryIO, *, path: str) -> None:
        self._raw = raw
        self._path = path
        self._bytes_written = 0

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("session snapshot writer accepts text only")
        for offset in range(0, len(text), _JSON_WRITE_CHUNK_CHARS):
            payload = text[offset : offset + _JSON_WRITE_CHUNK_CHARS].encode("utf-8")
            next_size = self._bytes_written + len(payload)
            if next_size > MAX_SESSION_SNAPSHOT_BYTES:
                raise ValueError(
                    "session snapshot exceeds "
                    f"{MAX_SESSION_SNAPSHOT_BYTES} UTF-8 bytes while writing: "
                    f"{self._path}"
                )
            written = self._raw.write(payload)
            if written != len(payload):
                raise OSError(
                    f"short write while saving session snapshot: {self._path}"
                )
            self._bytes_written = next_size
        return len(text)


class SessionStore:
    allowed_roles = {"system", "user", "assistant", "tool"}

    def has_snapshot(self, path: str) -> bool:
        """Return whether a base or recoverable journal sidecar is present."""
        return os.path.lexists(path) or os.path.lexists(self._journal_path(path))

    def save(
        self,
        path: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_parent(path)
        obj = {**(meta or {}), "messages": messages}
        self._atomic_json_write(path, obj)

    def save_manifest(self, path: str, manifest: dict[str, Any]) -> None:
        self._ensure_parent(path)
        self._atomic_json_write(path, manifest)

    def load_snapshot(self, path: str, system_prompt: str) -> dict[str, Any]:
        """Load the versioned snapshot, accepting legacy list/JSONL files."""
        try:
            text = self._read_snapshot_text(path)
        except FileNotFoundError:
            text = None
        records = self._read_journal_records(path)
        if text is None and not records:
            raise FileNotFoundError(path)

        blank_legacy_file = text is not None and not text.strip()
        if text is None:
            snapshot: dict[str, Any] = {}
            raw_messages: Any = []
        else:
            parsed = self._parse_document(text)
            if isinstance(parsed, dict):
                snapshot = dict(parsed)
                if "messages" not in snapshot:
                    raise ValueError(
                        "Invalid session snapshot: missing required 'messages' list"
                    )
                raw_messages = snapshot["messages"]
            else:
                snapshot = {"messages": parsed}
                raw_messages = parsed
            if not isinstance(raw_messages, list):
                raise ValueError(
                    "Invalid session snapshot: 'messages' must be a list"
                )
        messages = list(raw_messages)
        sequence = self._snapshot_sequence(snapshot)
        for record in records:
            record_sequence = self._record_integer(record, "sequence", minimum=1)
            if record_sequence <= sequence:
                continue
            replace_from = self._record_integer(record, "replace_from", minimum=0)
            message_count = self._record_integer(record, "message_count", minimum=0)
            if replace_from > len(messages):
                raise ValueError(
                    "Invalid autosave journal: replace_from exceeds restored messages"
                )
            delta = record.get("messages")
            meta = record.get("meta")
            if not isinstance(delta, list) or not isinstance(meta, dict):
                raise ValueError(
                    "Invalid autosave journal: messages and meta must be structured"
                )
            self._validate_messages(delta)
            messages = [*messages[:replace_from], *delta]
            if len(messages) != message_count:
                raise ValueError(
                    "Invalid autosave journal: message_count does not match delta"
                )
            seen_reset = record.get("seen_result_hashes_reset")
            seen_added = record.get("seen_result_hashes_added")
            if not isinstance(seen_reset, bool) or not isinstance(seen_added, list):
                raise ValueError(
                    "Invalid autosave journal: seen-result hash delta"
                )
            if not all(isinstance(value, str) for value in seen_added):
                raise ValueError(
                    "Invalid autosave journal: seen-result hashes must be text"
                )
            prior_state = snapshot.get("session_state")
            prior_seen = (
                prior_state.get("seen_result_hashes", [])
                if isinstance(prior_state, dict)
                else []
            )
            if not isinstance(prior_seen, list) or not all(
                isinstance(value, str) for value in prior_seen
            ):
                raise ValueError(
                    "Invalid session snapshot: seen-result hashes must be text"
                )
            seen_hashes = set() if seen_reset else set(prior_seen)
            seen_hashes.update(seen_added)
            restored_meta = dict(meta)
            restored_state = (
                dict(restored_meta.get("session_state", {}))
                if isinstance(restored_meta.get("session_state", {}), dict)
                else {}
            )
            restored_state["seen_result_hashes"] = sorted(seen_hashes)
            restored_meta["session_state"] = restored_state
            snapshot = {
                **restored_meta,
                _AUTOSAVE_SEQUENCE_KEY: record_sequence,
            }
            sequence = record_sequence

        self._validate_messages(messages)
        if not messages:
            if text is not None and not blank_legacy_file:
                raise ValueError(
                    "Invalid session snapshot: expected at least one message"
                )
            messages = [{"role": "system", "content": system_prompt}]
        snapshot["messages"] = messages
        return snapshot

    def load_messages(self, path: str, system_prompt: str) -> list[dict[str, Any]]:
        return list(self.load_snapshot(path, system_prompt)["messages"])

    def _validate_messages(self, messages: list[dict[str, Any]]) -> None:
        for lineno, msg in enumerate(messages, 1):
            if not isinstance(msg, dict):
                raise ValueError(f"Invalid message at position {lineno}: expected object")
            role = msg.get("role")
            if role not in self.allowed_roles:
                raise ValueError(f"Invalid message role at position {lineno}: {role}")
            if role in {"system", "user"}:
                self._validate_content(msg, lineno)
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if "tool_calls" in msg and not isinstance(tool_calls, list):
                    raise ValueError(
                        "Invalid assistant message at position "
                        f"{lineno}: 'tool_calls' must be a list"
                    )
                if isinstance(tool_calls, list):
                    self._validate_tool_calls(tool_calls, lineno)
                if "content" not in msg and not tool_calls:
                    raise ValueError(
                        "Invalid assistant message at position "
                        f"{lineno}: expected 'content' or 'tool_calls'"
                    )
                if "content" in msg:
                    self._validate_content(msg, lineno, allow_none=bool(tool_calls))
            else:
                self._validate_content(msg, lineno)
                tool_call_id = msg.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise ValueError(
                        "Invalid tool message at position "
                        f"{lineno}: expected non-empty 'tool_call_id'"
                    )

    @staticmethod
    def _validate_content(
        message: dict[str, Any],
        position: int,
        *,
        allow_none: bool = False,
    ) -> None:
        if "content" not in message:
            raise ValueError(
                f"Invalid message at position {position}: missing 'content'"
            )
        content = message["content"]
        if allow_none and content is None:
            return
        if not isinstance(content, (str, list)):
            raise ValueError(
                "Invalid message at position "
                f"{position}: 'content' must be text or a content-part list"
            )
        if isinstance(content, list):
            SessionStore._validate_content_parts(content, position)

    @staticmethod
    def _validate_content_parts(
        parts: list[Any],
        position: int,
    ) -> None:
        if not parts:
            raise ValueError(
                "Invalid message at position "
                f"{position}: 'content' part list must not be empty"
            )
        for part_index, part in enumerate(parts, 1):
            prefix = (
                f"Invalid message at position {position}, "
                f"content part {part_index}"
            )
            if not isinstance(part, dict):
                raise ValueError(f"{prefix}: expected object")
            part_type = part.get("type")
            if not isinstance(part_type, str) or not part_type.strip():
                raise ValueError(f"{prefix}: expected non-empty 'type'")
            if part_type in {"text", "input_text", "output_text"}:
                if not isinstance(part.get("text"), str):
                    raise ValueError(
                        f"{prefix}: {part_type!r} requires text"
                    )
                continue
            if part_type == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, str) and image_url:
                    continue
                if (
                    isinstance(image_url, dict)
                    and isinstance(image_url.get("url"), str)
                    and image_url["url"]
                ):
                    continue
                raise ValueError(
                    f"{prefix}: 'image_url' requires a URL string or object"
                )
            # Preserve provider-specific blocks while rejecting a type-only
            # placeholder that no provider can consume.
            if len(part) == 1:
                raise ValueError(
                    f"{prefix}: {part_type!r} requires provider payload fields"
                )

    @staticmethod
    def _validate_tool_calls(
        tool_calls: list[Any],
        position: int,
    ) -> None:
        for call_index, tool_call in enumerate(tool_calls, 1):
            prefix = (
                f"Invalid assistant message at position {position}, "
                f"tool call {call_index}"
            )
            if not isinstance(tool_call, dict):
                raise ValueError(f"{prefix}: expected object")
            call_id = tool_call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"{prefix}: expected non-empty 'id'")
            if tool_call.get("type") != "function":
                raise ValueError(f"{prefix}: 'type' must be 'function'")
            function = tool_call.get("function")
            if not isinstance(function, dict):
                raise ValueError(f"{prefix}: 'function' must be an object")
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"{prefix}: function requires non-empty 'name'"
                )
            if not isinstance(function.get("arguments"), str):
                raise ValueError(
                    f"{prefix}: function 'arguments' must be a string"
                )

    def _parse_document(self, text: str) -> dict[str, Any] | list[dict[str, Any]]:
        if not text.strip():
            return []
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return self._parse_jsonl(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return obj
        raise ValueError("Invalid session file: expected object or array")

    def append_snapshot_delta(
        self,
        path: str,
        *,
        sequence: int,
        replace_from: int,
        messages: list[dict[str, Any]],
        meta: dict[str, Any],
        seen_result_hashes_reset: bool = False,
        seen_result_hashes_added: list[str] | None = None,
    ) -> None:
        """Durably append one absolute, idempotent autosave delta."""
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
        ):
            raise ValueError("autosave sequence must be a positive integer")
        if (
            isinstance(replace_from, bool)
            or not isinstance(replace_from, int)
            or replace_from < 0
        ):
            raise ValueError("autosave replace_from must be a non-negative integer")
        if not isinstance(seen_result_hashes_reset, bool):
            raise ValueError("autosave seen-result reset must be boolean")
        seen_added = (
            [] if seen_result_hashes_added is None else seen_result_hashes_added
        )
        if not isinstance(seen_added, list) or not all(
            isinstance(value, str) for value in seen_added
        ):
            raise ValueError("autosave seen-result hashes must be text")
        self._validate_messages(messages)
        journal_meta = dict(meta)
        raw_state = journal_meta.get("session_state")
        if isinstance(raw_state, dict):
            journal_state = dict(raw_state)
            journal_state.pop("seen_result_hashes", None)
            journal_meta["session_state"] = journal_state
        record = {
            "journal_version": _AUTOSAVE_JOURNAL_VERSION,
            "sequence": sequence,
            "replace_from": replace_from,
            "message_count": replace_from + len(messages),
            "messages": messages,
            "meta": journal_meta,
            "seen_result_hashes_reset": seen_result_hashes_reset,
            "seen_result_hashes_added": seen_added,
        }
        self._append_journal_record(path, record)

    def checkpoint_snapshot(
        self,
        path: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any],
        sequence: int,
    ) -> None:
        """Publish one full base, then atomically compact its covered journal."""
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ValueError("autosave sequence must be a non-negative integer")
        self._ensure_parent(path)
        obj = {
            **meta,
            _AUTOSAVE_SEQUENCE_KEY: sequence,
            "messages": messages,
        }
        with _journal_operation_lock(path):
            self._atomic_json_write(path, obj)
            write_regular_bytes_atomic(
                self._journal_path(path),
                b"",
                max_bytes=0,
            )

    @staticmethod
    def _journal_path(path: str) -> str:
        return f"{path}{_AUTOSAVE_JOURNAL_SUFFIX}"

    @staticmethod
    def _encode_journal_record(record: dict[str, Any]) -> str:
        return (
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )

    def _append_journal_record(self, path: str, record: dict[str, Any]) -> None:
        with _journal_operation_lock(path):
            journal_path = self._journal_path(path)
            payload = self._encode_journal_record(record)
            payload_size = len(payload.encode("utf-8"))
            if payload_size > MAX_SESSION_SNAPSHOT_BYTES:
                raise ValueError(
                    "autosave journal record exceeds "
                    f"{MAX_SESSION_SNAPSHOT_BYTES} UTF-8 bytes while writing: "
                    f"{journal_path}"
                )
            with open_regular_text_append(journal_path, readable=True) as handle:
                current_size = os.fstat(handle.fileno()).st_size
                complete_size = self._complete_journal_size(
                    handle.fileno(),
                    current_size,
                )
                if complete_size < current_size:
                    os.ftruncate(handle.fileno(), complete_size)
                    current_size = complete_size
                if current_size + payload_size > MAX_SESSION_SNAPSHOT_BYTES:
                    raise ValueError(
                        "autosave journal exceeds "
                        f"{MAX_SESSION_SNAPSHOT_BYTES} UTF-8 bytes while writing: "
                        f"{journal_path}"
                    )
                write_locked_text(handle, payload)
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _complete_journal_size(fd: int, size: int) -> int:
        """Find the end of the last complete line using bounded tail reads."""
        if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
            return size
        cursor = size
        while cursor:
            start = max(0, cursor - _JSON_WRITE_CHUNK_CHARS)
            chunk = os.pread(fd, cursor - start, start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            cursor = start
        return 0

    def _read_journal_records(self, path: str) -> list[dict[str, Any]]:
        journal_path = self._journal_path(path)
        try:
            payload = read_regular_bytes(
                journal_path,
                max_bytes=MAX_SESSION_SNAPSHOT_BYTES,
            )
        except FileNotFoundError:
            return []
        final_newline = payload.rfind(b"\n")
        if final_newline < 0:
            return []
        try:
            complete = payload[: final_newline + 1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid autosave journal: non-UTF-8 record") from exc
        records: list[dict[str, Any]] = []
        for lineno, line in enumerate(complete.splitlines(), 1):
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid autosave journal record at line {lineno}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Invalid autosave journal record at line {lineno}: expected object"
                )
            if record.get("journal_version") != _AUTOSAVE_JOURNAL_VERSION:
                raise ValueError(
                    f"Invalid autosave journal record at line {lineno}: version"
                )
            records.append(record)
        return records

    @staticmethod
    def _snapshot_sequence(snapshot: dict[str, Any]) -> int:
        value = snapshot.get(_AUTOSAVE_SEQUENCE_KEY, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid session snapshot: autosave sequence")
        return value

    @staticmethod
    def _record_integer(
        record: dict[str, Any],
        key: str,
        *,
        minimum: int,
    ) -> int:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"Invalid autosave journal: {key}")
        return value

    @staticmethod
    def _parse_jsonl(text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                messages.append(json.loads(line))
        return messages

    @staticmethod
    def _ensure_parent(path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            ensure_directory_no_symlinks(parent)

    @staticmethod
    def _read_snapshot_text(path: str) -> str:
        return read_regular_bytes(
            path,
            max_bytes=MAX_SESSION_SNAPSHOT_BYTES,
        ).decode("utf-8")

    @staticmethod
    def _atomic_json_write(path: str, value: Any) -> None:
        """Durably replace one JSON file without exposing a partial snapshot."""
        def serialize(handle: BinaryIO) -> None:
            writer = _BoundedUTF8Writer(handle, path=path)
            json.dump(value, writer, ensure_ascii=False, indent=2)

        write_regular_file_atomic(
            path,
            serialize,
            max_bytes=MAX_SESSION_SNAPSHOT_BYTES,
            context="session snapshot",
        )
