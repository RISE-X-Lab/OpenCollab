"""Canonical identities used at scheduler and persistence boundaries."""

from __future__ import annotations

import hashlib
import re
import unicodedata

MAX_ROLE_IDENTITY_BYTES = 128
_ROLE_SLUG_MAX_CHARS = 32


def validate_role_identity(value: object) -> str:
    """Return one normalized role identity or reject ambiguous/path-like input."""
    if not isinstance(value, str):
        raise ValueError("role must be a non-empty string")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized != normalized.strip()
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in normalized
        )
    ):
        raise ValueError("role must be one safe path component")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("role must be valid UTF-8") from exc
    if len(encoded) > MAX_ROLE_IDENTITY_BYTES:
        raise ValueError(
            f"role exceeds {MAX_ROLE_IDENTITY_BYTES}-byte UTF-8 limit"
        )
    return normalized


def role_collision_key(role: object) -> str:
    return validate_role_identity(role).casefold()


def role_storage_slug(role: object) -> str:
    """Stable ASCII component with a digest that preserves identity uniqueness."""
    normalized = validate_role_identity(role)
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._")
    readable = readable[:_ROLE_SLUG_MAX_CHARS] or "role"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


__all__ = [
    "MAX_ROLE_IDENTITY_BYTES",
    "role_collision_key",
    "role_storage_slug",
    "validate_role_identity",
]
