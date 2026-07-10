from __future__ import annotations

import json
import os
import stat
import uuid
from typing import Any, BinaryIO

from opencollab.adapters.safe_files import (
    _open_directory_no_symlinks,
    ensure_directory_no_symlinks,
    read_regular_bytes,
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
        target = os.path.abspath(path)
        directory = os.path.dirname(target) or "."
        filename = os.path.basename(target)
        if not filename or filename in {".", ".."}:
            raise ValueError(f"invalid session snapshot path: {path}")
        directory_fd = _open_directory_no_symlinks(os.path.abspath(directory))
        temporary = f".{filename}.{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = -1
        replaced = False
        written_identity: tuple[int, int] | None = None
        primary_error: BaseException | None = None
        try:
            fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            handle = os.fdopen(fd, "wb")
            fd = -1
            serialization_error: BaseException | None = None
            try:
                writer = _BoundedUTF8Writer(handle, path=path)
                json.dump(value, writer, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
                written = os.fstat(handle.fileno())
                written_identity = (written.st_dev, written.st_ino)
            except BaseException as exc:
                serialization_error = exc
            try:
                handle.close()
            except BaseException as close_error:
                if serialization_error is not None:
                    serialization_error.add_note(
                        "session temporary handle close failed with "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                else:
                    raise
            if serialization_error is not None:
                raise serialization_error
            verified_directory_fd = _open_directory_no_symlinks(
                os.path.abspath(directory)
            )
            verification_error: BaseException | None = None
            try:
                original = os.fstat(directory_fd)
                verified = os.fstat(verified_directory_fd)
                if (original.st_dev, original.st_ino) != (
                    verified.st_dev,
                    verified.st_ino,
                ):
                    raise OSError(
                        "session snapshot parent changed before atomic replace: "
                        f"{directory}"
                    )
            except BaseException as exc:
                verification_error = exc
            try:
                os.close(verified_directory_fd)
            except BaseException as close_error:
                if verification_error is not None:
                    verification_error.add_note(
                        "session verified parent fd close failed with "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                else:
                    raise
            if verification_error is not None:
                raise verification_error
            try:
                existing = os.stat(
                    filename,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise OSError(f"session snapshot target is not a regular file: {target}")
            os.replace(
                temporary,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replaced = True
            current = os.stat(
                filename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != written_identity
            ):
                raise OSError(f"session snapshot changed during replace: {target}")
            os.fsync(directory_fd)
            final_directory_fd = _open_directory_no_symlinks(
                os.path.abspath(directory)
            )
            final_verification_error: BaseException | None = None
            try:
                original = os.fstat(directory_fd)
                final = os.fstat(final_directory_fd)
                if (original.st_dev, original.st_ino) != (
                    final.st_dev,
                    final.st_ino,
                ):
                    raise OSError(
                        "session snapshot parent changed after atomic replace: "
                        f"{directory}"
                    )
                visible = os.stat(
                    filename,
                    dir_fd=final_directory_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(visible.st_mode)
                    or (visible.st_dev, visible.st_ino) != written_identity
                ):
                    raise OSError(
                        f"session snapshot path changed after atomic replace: {target}"
                    )
            except BaseException as exc:
                final_verification_error = exc
            try:
                os.close(final_directory_fd)
            except BaseException as close_error:
                if final_verification_error is not None:
                    final_verification_error.add_note(
                        "session final parent fd close failed with "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                else:
                    raise
            if final_verification_error is not None:
                raise final_verification_error
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_errors: list[tuple[str, BaseException]] = []
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException as exc:
                    cleanup_errors.append(("temporary fd close", exc))
            if not replaced:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except BaseException as exc:
                    cleanup_errors.append((f"temporary unlink {temporary}", exc))
            try:
                os.close(directory_fd)
            except BaseException as exc:
                cleanup_errors.append(("parent directory fd close", exc))
            if cleanup_errors:
                if primary_error is not None:
                    for stage, cleanup_error in cleanup_errors:
                        primary_error.add_note(
                            f"session atomic cleanup {stage} failed with "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                else:
                    stage, cleanup_error = cleanup_errors[0]
                    for extra_stage, extra_error in cleanup_errors[1:]:
                        cleanup_error.add_note(
                            "additional session atomic cleanup "
                            f"{extra_stage} failed with "
                            f"{type(extra_error).__name__}: {extra_error}"
                        )
                    cleanup_error.add_note(
                        f"session atomic cleanup stage: {stage}"
                    )
                    raise cleanup_error
