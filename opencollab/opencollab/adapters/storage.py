from __future__ import annotations

import json
import os
from typing import Any, BinaryIO

from opencollab.adapters.safe_files import (
    ensure_directory_no_symlinks,
    read_regular_bytes,
    write_regular_file_atomic,
)

MAX_SESSION_SNAPSHOT_BYTES = 64 * 1024 * 1024
_JSON_WRITE_CHUNK_CHARS = 64 * 1024


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
        text = self._read_snapshot_text(path)

        parsed = self._parse_document(text)
        snapshot = dict(parsed) if isinstance(parsed, dict) else {"messages": parsed}
        messages = list(snapshot.get("messages", []))
        self._validate_messages(messages)
        if not messages:
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

    def _parse(self, text: str) -> list[dict[str, Any]]:
        """Read the structured-JSON format, falling back to legacy JSONL."""
        obj = self._parse_document(text)
        if isinstance(obj, dict):
            return list(obj.get("messages", []))
        return obj

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
