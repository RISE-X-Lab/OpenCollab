"""Authenticated framing for persistent retirement records."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence

SCHEMA = "opencollab.retirement.v3"
ZERO_MAC = "0" * 64


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_key(signing_key: bytes, expected_bytes: int) -> None:
    if not isinstance(signing_key, bytes) or len(signing_key) != expected_bytes:
        raise ValueError("internal retirement authentication key is invalid")


def encode_record(
    values: Mapping[str, object],
    signing_key: bytes,
    *,
    sequence: int,
    previous_mac: str,
    expected_key_bytes: int,
) -> bytes:
    """Encode one ordered, chained HMAC record."""
    _require_key(signing_key, expected_key_bytes)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("retirement log sequence is invalid")
    if len(previous_mac) != 64 or any(character not in "0123456789abcdef" for character in previous_mac):
        raise ValueError("retirement log previous authentication code is invalid")
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "sequence": sequence,
        "previous_mac": previous_mac,
        **values,
    }
    payload["mac"] = hmac.new(signing_key, _canonical(payload), hashlib.sha256).hexdigest()
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _strict_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("internal retirement log contains a duplicate JSON field")
        result[key] = value
    return result


def decode_records(
    raw: bytes,
    signing_key: bytes,
    *,
    record_fields: Sequence[str],
    max_records: int,
    expected_key_bytes: int,
) -> list[tuple[dict[str, object], str]]:
    """Authenticate a complete JSONL stream and enforce exact chain order."""
    _require_key(signing_key, expected_key_bytes)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("internal retirement log ends with a partial record")
    lines = raw.splitlines()
    if len(lines) > max_records:
        raise ValueError("internal retirement log has too many records")
    expected_fields = {
        "schema",
        "sequence",
        "previous_mac",
        "mac",
        *record_fields,
    }
    previous_mac = ZERO_MAC
    records: list[tuple[dict[str, object], str]] = []
    for expected_sequence, line in enumerate(lines):
        if not line:
            raise ValueError("internal retirement log contains an empty record")
        try:
            payload = json.loads(line, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("internal retirement log contains invalid JSONL") from exc
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError("retirement log record fields are invalid")
        if payload.get("schema") != SCHEMA:
            raise ValueError("retirement log record schema is invalid")
        sequence = payload.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != expected_sequence
        ):
            raise ValueError("retirement log record sequence is duplicated or out of order")
        if payload.get("previous_mac") != previous_mac:
            raise ValueError("retirement log authentication chain is broken")
        supplied_mac = payload.get("mac")
        if (
            not isinstance(supplied_mac, str)
            or len(supplied_mac) != 64
            or any(character not in "0123456789abcdef" for character in supplied_mac)
        ):
            raise ValueError("retirement log record authentication is missing")
        authenticated = dict(payload)
        authenticated.pop("mac")
        expected_mac = hmac.new(signing_key, _canonical(authenticated), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ValueError("retirement log record authentication failed")
        previous_mac = supplied_mac
        records.append(
            ({field: payload[field] for field in record_fields}, supplied_mac)
        )
    return records


__all__ = ["ZERO_MAC", "decode_records", "encode_record"]
