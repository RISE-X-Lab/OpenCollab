from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from opencollab.adapters.safe_files import (
    _open_regular_read,
    acquire_exclusive_lock,
    ensure_directory_no_symlinks,
    open_regular_text_append,
    read_regular_bytes,
    write_locked_text,
    write_regular_bytes_atomic,
    write_regular_file_atomic,
)
from opencollab.application.ports import (
    _AUTOSAVE_EXPORT_SOURCE_SEQUENCE,
    SnapshotCapacityError,
    SnapshotSequenceConflict,
    _checkpoint_publication,
)
from opencollab.application.session_snapshot import decode_anthropic_history
from opencollab.domain.session import same_snapshot_value

MAX_SESSION_SNAPSHOT_BYTES = 64 * 1024 * 1024
_JSON_WRITE_CHUNK_CHARS = 64 * 1024
_AUTOSAVE_SEQUENCE_KEY = "_autosave_sequence"
_AUTOSAVE_JOURNAL_VERSION = 1
_AUTOSAVE_JOURNAL_SUFFIX = ".journal"
_AUTOSAVE_JOURNAL_LOCK_SUFFIX = ".lock"
_AUTOSAVE_OWNERSHIP_LOCK_SUFFIX = ".autosave.lock"
_JOURNAL_LOCK_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)

_FileRevision = tuple[int, int, int, int, int]
_AutosaveFileRevisions = tuple[_FileRevision | None, _FileRevision | None]


@dataclass(frozen=True)
class _AutosaveCursor:
    path: str
    revisions: _AutosaveFileRevisions
    sequence: int


def _file_revision(info: os.stat_result) -> _FileRevision:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _regular_file_revision(path: str) -> _FileRevision | None:
    try:
        fd = _open_regular_read(path)
    except FileNotFoundError:
        return None
    try:
        return _file_revision(os.fstat(fd))
    finally:
        os.close(fd)


@dataclass
class _JournalLockEntry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0

_JOURNAL_LOCKS: dict[str, _JournalLockEntry] = {}
_JOURNAL_LOCKS_GUARD = threading.Lock()

@contextmanager
def _journal_thread_lock(path: str) -> Iterator[None]:
    key = os.path.abspath(path)
    with _JOURNAL_LOCKS_GUARD:
        entry = _JOURNAL_LOCKS.setdefault(key, _JournalLockEntry())
        entry.users += 1
    acquired = False
    try:
        acquired = entry.lock.acquire(timeout=_JOURNAL_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            raise TimeoutError("timed out acquiring journal thread lock")
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _JOURNAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _JOURNAL_LOCKS.get(key) is entry:
                del _JOURNAL_LOCKS[key]

def _has_journal_sidecars(path: str) -> bool:
    """Check sidecar absence without treating inaccessible paths as missing."""
    journal_path = f"{path}{_AUTOSAVE_JOURNAL_SUFFIX}"
    for candidate in (journal_path, f"{journal_path}{_AUTOSAVE_JOURNAL_LOCK_SUFFIX}"):
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        return True
    return False

@contextmanager
def _journal_operation_lock(path: str, *, reading: bool = False) -> Iterator[bool]:
    """Serialize compaction and append on a stable cross-process lockfile.

    The lockfile is deliberately persistent. Removing it after unlock can split
    waiters across old and newly-created inodes, allowing two critical sections
    to overlap. The snapshot and journal themselves are replaced by rename, so
    neither data inode can safely serve as this coordination point.
    """
    with _journal_thread_lock(path):
        lock_path = f"{path}{_AUTOSAVE_JOURNAL_SUFFIX}{_AUTOSAVE_JOURNAL_LOCK_SUFFIX}"
        with ExitStack() as stack:
            if reading:
                try:
                    handle = stack.enter_context(os.fdopen(_open_regular_read(lock_path), "rb"))
                except FileNotFoundError:
                    try:
                        handle = stack.enter_context(open_regular_text_append(lock_path))
                    except OSError as exc:
                        # A standalone export needs one bounded atomic file read.
                        # Creation failure proves this reader cannot publish a
                        # journal or checkpoint beside it. Existing sidecars
                        # still require their stable coordination lock.
                        if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                            raise
                        if _has_journal_sidecars(path):
                            raise
                        yield False
                        return
                except PermissionError:
                    # Preserve access through an existing write-only lock.
                    handle = stack.enter_context(open_regular_text_append(lock_path))
            else:
                handle = stack.enter_context(open_regular_text_append(lock_path))
            acquire_exclusive_lock(
                handle.fileno(),
                timeout_seconds=_JOURNAL_LOCK_TIMEOUT_SECONDS,
                context="journal operation lock",
            )
            try:
                yield True
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
                raise SnapshotCapacityError(
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
    allowed_roles = {"system", "developer", "user", "assistant", "tool"}
    _autosave_cursor: _AutosaveCursor | None = None

    def try_autosave_lock(
        self, path: str, *, expected_sequence: int | None = None,
    ) -> Callable[[], None] | None:
        # Keep the sidecar inode stable while snapshots and journals are replaced.
        lock_path = f"{path}{_AUTOSAVE_OWNERSHIP_LOCK_SUFFIX}"
        handle = open_regular_text_append(lock_path)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            handle.close()
            if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return None
            raise
        try:
            if expected_sequence is not None:
                with _journal_operation_lock(path):
                    sequence = self._autosave_sequence_for_ownership(path)
                    if sequence > expected_sequence:
                        raise SnapshotSequenceConflict(
                            f"refusing stale autosave sequence {expected_sequence}; "
                            f"persisted sequence {sequence} is newer"
                        )
        except BaseException:
            handle.close()
            raise
        return handle.close

    def _autosave_file_revisions(self, path: str) -> _AutosaveFileRevisions:
        return _regular_file_revision(path), _regular_file_revision(self._journal_path(path))

    def _autosave_sequence_for_ownership(self, path: str) -> int:
        cursor = self._autosave_cursor
        if cursor is not None and cursor.path == os.path.abspath(path):
            try:
                if cursor.revisions == self._autosave_file_revisions(path):
                    return cursor.sequence
            except OSError:
                pass
        sequence = self._newest_persisted_sequence(path)
        self._remember_autosave_cursor(path, sequence)
        return sequence

    def _remember_autosave_cursor(self, path: str, sequence: int) -> None:
        # The caller holds the journal lock across the read or publication.
        try:
            key = os.path.abspath(path)
            revisions = self._autosave_file_revisions(path)
        except OSError:
            return
        self._autosave_cursor = _AutosaveCursor(key, revisions, sequence)

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
        with _journal_operation_lock(path):
            records = self._read_journal_records(path)
            destination_sequence = None
            if records:
                destination_sequence = self._newest_persisted_sequence(path)
            elif _AUTOSAVE_SEQUENCE_KEY in obj:
                try:
                    destination_sequence = self._persisted_base_sequence(path)
                except ValueError:
                    # An explicit export may replace a non-snapshot file.
                    destination_sequence = None
            if destination_sequence is not None:
                # An export supersedes the destination even after journal compaction.
                source_sequence = self._snapshot_sequence(obj)
                if destination_sequence >= source_sequence and _AUTOSAVE_SEQUENCE_KEY in obj:
                    obj.setdefault(_AUTOSAVE_EXPORT_SOURCE_SEQUENCE, source_sequence)
                obj[_AUTOSAVE_SEQUENCE_KEY] = max(source_sequence, destination_sequence + 1)
            self._atomic_json_write(path, obj)
            if records:
                write_regular_bytes_atomic(self._journal_path(path), b"", max_bytes=0)

    def save_manifest(self, path: str, manifest: dict[str, Any]) -> None:
        self._ensure_parent(path)
        self._atomic_json_write(path, manifest)

    def load_snapshot(self, path: str, system_prompt: str) -> dict[str, Any]:
        """Load the versioned snapshot, accepting legacy list/JSONL files."""
        # A checkpoint replaces the base file and then truncates the journal.
        # Keep both reads in the same critical section so a reader cannot
        # observe the old base together with the already-compacted journal (or
        # vice versa).
        with _journal_operation_lock(path, reading=True) as journal_locked:
            snapshot = self._load_snapshot_unlocked(
                path, system_prompt, read_journal=journal_locked,
            )
            if journal_locked:
                self._remember_autosave_cursor(path, self._snapshot_sequence(snapshot))
            return snapshot

    def _load_snapshot_unlocked(
        self, path: str, fallback_system_prompt: str | None, *,
        read_journal: bool = True,
        pending_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Load a locked snapshot or a standalone read-only export."""
        try:
            text = self._read_snapshot_text(path)
        except FileNotFoundError:
            text = None
        records = self._read_journal_records(path) if read_journal else []
        if pending_record is not None:
            records.append(pending_record)
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
            retry_snapshot = None
            if record_sequence <= sequence:
                if record is not pending_record:
                    continue
                if record_sequence < sequence:
                    raise SnapshotSequenceConflict(
                        f"refusing stale autosave sequence {record_sequence}; "
                        f"persisted sequence is {sequence}"
                    )
                retry_snapshot = {**snapshot, "messages": messages}
            replace_from = self._record_integer(record, "replace_from", minimum=0)
            message_count = self._record_integer(record, "message_count", minimum=0)
            if replace_from > len(messages):
                if retry_snapshot is not None:
                    raise SnapshotSequenceConflict(
                        f"refusing conflicting autosave sequence {record_sequence}"
                    )
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
            if retry_snapshot is not None and not self._checkpoint_states_equal(
                retry_snapshot, {**snapshot, "messages": messages},
            ):
                raise SnapshotSequenceConflict(
                    f"refusing conflicting autosave sequence {record_sequence}"
                )
            sequence = record_sequence

        self._validate_messages(messages)
        if not messages:
            if text is not None and not blank_legacy_file:
                raise ValueError(
                    "Invalid session snapshot: expected at least one message"
                )
            if fallback_system_prompt is not None:
                messages = [{"role": "system", "content": fallback_system_prompt}]
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
            if role in {"system", "developer", "user"}:
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
        document = text.strip()
        if not document:
            return []
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return self._parse_jsonl(text)
        if isinstance(obj, dict):
            # A single JSONL message also parses as one JSON object.
            snapshot_fields = {
                "messages", "snapshot_version", "session_state", "pending_messages",
                "aid", "run_id", "model", _AUTOSAVE_SEQUENCE_KEY, _AUTOSAVE_EXPORT_SOURCE_SEQUENCE,
            }
            role = obj.get("role")
            if (
                "\n" not in document
                and isinstance(role, str) and role in self.allowed_roles
                and ("content" in obj or (role == "assistant" and "tool_calls" in obj))
                and obj.keys().isdisjoint(snapshot_fields)
            ):
                return [obj]
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
        record = self._snapshot_delta_record(
            sequence=sequence,
            replace_from=replace_from,
            messages=messages,
            meta=meta,
            seen_result_hashes_reset=seen_result_hashes_reset,
            seen_result_hashes_added=seen_result_hashes_added,
        )
        self._append_journal_record(path, record)

    def append_snapshot_replacement(
        self,
        path: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any],
        sequence: int,
        reject_conflicts: bool = False,
    ) -> bool:
        state = meta.get("session_state")
        seen_result_hashes = state.get("seen_result_hashes", []) if isinstance(state, dict) else []
        record = self._snapshot_delta_record(
            sequence=sequence,
            replace_from=0,
            messages=messages,
            meta=meta,
            seen_result_hashes_reset=True,
            seen_result_hashes_added=seen_result_hashes,
        )
        with _journal_operation_lock(path):
            try:
                persisted = self._load_snapshot_unlocked(path, fallback_system_prompt=None)
            except FileNotFoundError:
                pass
            else:
                persisted_sequence = self._snapshot_sequence(persisted)
                if sequence <= persisted_sequence:
                    if reject_conflicts:
                        candidate = {**meta, _AUTOSAVE_SEQUENCE_KEY: sequence, "messages": messages}
                        if not self._checkpoint_states_equal(persisted, candidate):
                            raise SnapshotSequenceConflict(
                                f"refusing replacement sequence {sequence}; "
                                f"it conflicts with persisted state at sequence {persisted_sequence}"
                            )
                    return False
                stored_messages = persisted["messages"]
                replace_from = min(len(stored_messages), len(messages))
                for index in range(replace_from):
                    if not same_snapshot_value(stored_messages[index], messages[index]):
                        replace_from = index
                        break
                record["replace_from"] = replace_from
                record["messages"] = messages[replace_from:]
            self._append_journal_record_unlocked(path, record)
        return True

    def _snapshot_delta_record(
        self,
        *,
        sequence: int,
        replace_from: int,
        messages: list[dict[str, Any]],
        meta: dict[str, Any],
        seen_result_hashes_reset: bool,
        seen_result_hashes_added: list[str] | None,
    ) -> dict[str, Any]:
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
        return {
            "journal_version": _AUTOSAVE_JOURNAL_VERSION,
            "sequence": sequence,
            "replace_from": replace_from,
            "message_count": replace_from + len(messages),
            "messages": messages,
            "meta": journal_meta,
            "seen_result_hashes_reset": seen_result_hashes_reset,
            "seen_result_hashes_added": seen_added,
        }

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
        self._validate_messages(messages)
        self._ensure_parent(path)
        obj = {
            **meta,
            _AUTOSAVE_SEQUENCE_KEY: sequence,
            "messages": messages,
        }
        with _journal_operation_lock(path):
            persisted_sequence = self._newest_persisted_sequence(path)
            if persisted_sequence > sequence:
                raise SnapshotSequenceConflict(
                    "refusing stale checkpoint sequence "
                    f"{sequence}; persisted sequence {persisted_sequence} is newer"
                )
            journal_records = self._read_journal_records(path)
            same_sequence_records = [
                record
                for record in journal_records
                if self._record_integer(record, "sequence", minimum=1) == sequence
            ]
            if len(same_sequence_records) > 1:
                # Multiple records at one cursor are ambiguous: replay skips
                # the second and later records, so accepting this checkpoint
                # would clear data that cannot be proven equivalent.
                raise SnapshotSequenceConflict(
                    "refusing checkpoint sequence "
                    f"{sequence}; persisted journal contains duplicate sequence records"
                )
            if persisted_sequence == sequence > 0:
                # A peer can own this sequence in either the base or the journal.
                persisted = self._load_snapshot_unlocked(path, "")
                source_sequence = obj.get(_AUTOSAVE_EXPORT_SOURCE_SEQUENCE)
                if (
                    _AUTOSAVE_EXPORT_SOURCE_SEQUENCE not in persisted
                    and type(source_sequence) is int
                    and source_sequence == sequence - 1
                ):
                    # Older restores left this provenance implicit in their local cursor.
                    persisted[_AUTOSAVE_EXPORT_SOURCE_SEQUENCE] = source_sequence
                if not self._checkpoint_states_equal(persisted, obj):
                    source = "journal" if same_sequence_records else "snapshot"
                    raise SnapshotSequenceConflict(
                        "refusing checkpoint sequence "
                        f"{sequence}; it conflicts with persisted {source} state"
                    )
            self._publish_checkpoint_unlocked(path, obj)

    def _publish_checkpoint_unlocked(
        self, path: str, snapshot: dict[str, Any], *, defer_journal_reset: bool = False,
    ) -> None:
        publication = _checkpoint_publication.get()
        if publication is not None and (publication.path != path or defer_journal_reset):
            publication = None
        try:
            self._atomic_json_write(path, snapshot)
        except SnapshotCapacityError:
            self._atomic_json_write(path, snapshot, indent=None)
        if publication is not None:
            publication.synced = True
        journal_path = self._journal_path(path)
        try:
            write_regular_bytes_atomic(journal_path, b"", max_bytes=0)
        except OSError as exc:
            if not (defer_journal_reset or publication is not None and publication.defer_journal_reset):
                raise
            # The synced snapshot already supersedes every journal record.
            logger.warning("could not compact covered session journal %s: %s", journal_path, exc)
        self._remember_autosave_cursor(path, snapshot[_AUTOSAVE_SEQUENCE_KEY])

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
            try:
                self._append_journal_record_unlocked(path, record)
            except SnapshotCapacityError:
                snapshot = self._load_snapshot_unlocked(path, None, pending_record=record)
                self._publish_checkpoint_unlocked(path, snapshot, defer_journal_reset=True)

    def _append_journal_record_unlocked(self, path: str, record: dict[str, Any]) -> None:
        journal_path = self._journal_path(path)
        payload = self._encode_journal_record(record)
        payload_size = len(payload.encode("utf-8"))
        if payload_size > MAX_SESSION_SNAPSHOT_BYTES:
            raise SnapshotCapacityError(
                "autosave journal record exceeds "
                f"{MAX_SESSION_SNAPSHOT_BYTES} UTF-8 bytes while writing: "
                f"{journal_path}"
            )
        append_start: int | None = None
        try:
            with open_regular_text_append(journal_path, readable=True) as handle:
                before_append = os.fstat(handle.fileno())
                current_size = before_append.st_size
                complete_size = self._complete_journal_size(
                    handle.fileno(),
                    current_size,
                )
                if complete_size < current_size:
                    os.ftruncate(handle.fileno(), complete_size)
                    current_size = complete_size
                if current_size + payload_size > MAX_SESSION_SNAPSHOT_BYTES:
                    raise SnapshotCapacityError(
                        "autosave journal exceeds "
                        f"{MAX_SESSION_SNAPSHOT_BYTES} UTF-8 bytes while writing: "
                        f"{journal_path}"
                    )
                append_start = current_size
                write_locked_text(handle, payload)
                handle.flush()
                os.fsync(handle.fileno())
                self._advance_autosave_cursor(path, handle.fileno(), before_append, record["sequence"])
        except BaseException as append_error:
            if append_start is not None:
                try:
                    # Close the append handle before truncating so a buffered
                    # close cannot write the failed record back afterward.
                    with open_regular_text_append(journal_path, readable=True) as handle:
                        os.ftruncate(handle.fileno(), append_start)
                        os.fsync(handle.fileno())
                except BaseException as rollback_error:
                    raise append_error from rollback_error
            raise

    def _advance_autosave_cursor(
        self, path: str, fd: int, before_append: os.stat_result, sequence: int,
    ) -> None:
        cursor = self._autosave_cursor
        if cursor is None or cursor.path != os.path.abspath(path):
            return
        snapshot_revision, journal_revision = cursor.revisions
        if journal_revision != _file_revision(before_append) and not (
            journal_revision is None and before_append.st_size == 0
        ):
            return
        try:
            journal_revision = _file_revision(os.fstat(fd))
        except OSError:
            return
        # A late append can leave an older record after the current cursor.
        self._autosave_cursor = _AutosaveCursor(
            cursor.path, (snapshot_revision, journal_revision), max(cursor.sequence, sequence),
        )

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
        for lineno, line in enumerate(complete.split("\n"), 1):
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

    def _persisted_base_sequence(self, path: str) -> int | None:
        try:
            text = self._read_snapshot_text(path)
        except FileNotFoundError:
            return None
        parsed = self._parse_document(text)
        if isinstance(parsed, dict) and _AUTOSAVE_SEQUENCE_KEY in parsed:
            return self._snapshot_sequence(parsed)
        return None

    def _newest_persisted_sequence(self, path: str) -> int:
        newest = self._persisted_base_sequence(path) or 0
        for record in self._read_journal_records(path):
            newest = max(
                newest,
                self._record_integer(record, "sequence", minimum=1),
            )
        return newest

    @staticmethod
    def _checkpoint_states_equal(
        persisted: dict[str, Any], candidate: dict[str, Any]
    ) -> bool:
        def normalize(value: dict[str, Any]) -> dict[str, Any]:
            normalized = dict(value)
            raw_state = normalized.get("session_state")
            state = dict(raw_state) if isinstance(raw_state, dict) else {}
            # Journal replay materializes and sorts the seen-result set.
            seen = state.get("seen_result_hashes", [])
            if isinstance(seen, list) and all(isinstance(item, str) for item in seen):
                state["seen_result_hashes"] = sorted(set(seen))
            normalized["session_state"] = state
            return normalized

        persisted = normalize(persisted)
        candidate = normalize(candidate)
        if same_snapshot_value(persisted, candidate):
            history = persisted["session_state"].get("anthropic_history")
            return history is None or decode_anthropic_history(history, persisted["messages"]) is not None

        for snapshot in (persisted, candidate):
            state = snapshot["session_state"]
            history = state.get("anthropic_history")
            if history is None:
                return False
            decoded = decode_anthropic_history(history, snapshot["messages"])
            if decoded is None:
                return False
            state["anthropic_history"] = {**history, **decoded}
        return same_snapshot_value(persisted, candidate)

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
        for line in text.split("\n"):
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
    def _atomic_json_write(path: str, value: Any, *, indent: int | None = 2) -> None:
        """Durably replace one JSON file without exposing a partial snapshot."""
        publication = _checkpoint_publication.get()

        def mark_replaced() -> None:
            if publication is not None and publication.path == path:
                publication.visible = True

        def serialize(handle: BinaryIO) -> None:
            writer = _BoundedUTF8Writer(handle, path=path)
            if indent is None:
                json.dump(value, writer, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(value, writer, ensure_ascii=False, indent=indent)

        write_regular_file_atomic(
            path,
            serialize,
            max_bytes=MAX_SESSION_SNAPSHOT_BYTES,
            context="session snapshot",
            on_replace=mark_replaced if publication is not None else None,
        )
