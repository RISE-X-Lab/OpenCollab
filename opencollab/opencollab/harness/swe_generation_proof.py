"""Strict validation for current SWE generation integrity evidence."""

from __future__ import annotations

import hashlib
import re
from typing import Any

TRUSTED_PATCH_EXTRACTION_SCHEMA = "opencollab.trusted_patch_extraction.v1"
MAX_WORKSPACE_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_WORKSPACE_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_ARCHIVE_ENTRIES = 1_000_000
MAX_TRUSTED_PATCH_BYTES = 8 * 1024 * 1024

_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT_KEYS = {
    "enabled",
    "anonymous_head",
    "base_tree",
    "commit_count",
    "remote_count",
    "extra_git_metadata",
    "removed_git_metadata",
}
_EXTRACTION_KEYS = {
    "schema",
    "host_trusted",
    "fixed_anonymous_base",
    "base_tree",
    "archive_bounded",
    "baseline_archive_sha256",
    "baseline_archive_bytes",
    "baseline_archive_entries",
    "baseline_extracted_bytes",
    "workspace_archive_sha256",
    "workspace_archive_bytes",
    "workspace_archive_entries",
    "workspace_extracted_bytes",
    "archive_byte_limit",
    "extracted_byte_limit",
    "file_byte_limit",
    "entry_limit",
    "patch_byte_limit",
    "container_quiesced_before",
    "container_quiesced_after",
    "patch_sha256",
    "patch_bytes",
}


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def solver_git_snapshot_valid(value: Any) -> bool:
    """Return whether *value* is an exact current solver snapshot proof."""
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        return False
    anonymous_head = value.get("anonymous_head")
    base_tree = value.get("base_tree")
    if not isinstance(anonymous_head, str) or not _OBJECT_ID_RE.fullmatch(anonymous_head):
        return False
    if not isinstance(base_tree, str) or not _OBJECT_ID_RE.fullmatch(base_tree):
        return False
    if len(anonymous_head) != len(base_tree):
        return False
    return bool(
        value.get("enabled") is True
        and value.get("commit_count") == 1
        and not isinstance(value.get("commit_count"), bool)
        and value.get("remote_count") == 0
        and not isinstance(value.get("remote_count"), bool)
        and value.get("extra_git_metadata") == 0
        and not isinstance(value.get("extra_git_metadata"), bool)
        and _nonnegative_int(value.get("removed_git_metadata"))
    )


def _trusted_patch_extraction_shape_valid(
    value: Any,
    *,
    snapshot: Any,
) -> bool:
    if not solver_git_snapshot_valid(snapshot):
        return False
    if not isinstance(value, dict) or set(value) != _EXTRACTION_KEYS:
        return False
    if value.get("schema") != TRUSTED_PATCH_EXTRACTION_SCHEMA:
        return False
    if value.get("host_trusted") is not True or value.get("archive_bounded") is not True:
        return False
    if value.get("container_quiesced_before") is not True:
        return False
    if value.get("container_quiesced_after") is not True:
        return False
    if value.get("fixed_anonymous_base") != snapshot["anonymous_head"]:
        return False
    if value.get("base_tree") != snapshot["base_tree"]:
        return False
    for key in ("baseline_archive_sha256", "workspace_archive_sha256"):
        if not isinstance(value.get(key), str) or not _SHA256_RE.fullmatch(value[key]):
            return False
    for key in (
        "baseline_archive_bytes",
        "baseline_archive_entries",
        "baseline_extracted_bytes",
        "workspace_archive_bytes",
        "workspace_archive_entries",
        "workspace_extracted_bytes",
        "patch_bytes",
    ):
        if not _nonnegative_int(value.get(key)):
            return False
    if any(
        value[key] <= 0
        for key in (
            "baseline_archive_bytes",
            "baseline_archive_entries",
            "workspace_archive_bytes",
            "workspace_archive_entries",
        )
    ):
        return False
    expected_limits = {
        "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
        "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
        "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
        "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
        "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
    }
    if any(value.get(key) != expected for key, expected in expected_limits.items()):
        return False
    if value["baseline_archive_bytes"] > MAX_WORKSPACE_ARCHIVE_BYTES:
        return False
    if value["workspace_archive_bytes"] > MAX_WORKSPACE_ARCHIVE_BYTES:
        return False
    if value["baseline_archive_entries"] > MAX_WORKSPACE_ARCHIVE_ENTRIES:
        return False
    if value["workspace_archive_entries"] > MAX_WORKSPACE_ARCHIVE_ENTRIES:
        return False
    if value["baseline_extracted_bytes"] > MAX_WORKSPACE_EXTRACTED_BYTES:
        return False
    if value["workspace_extracted_bytes"] > MAX_WORKSPACE_EXTRACTED_BYTES:
        return False
    if value["patch_bytes"] > MAX_TRUSTED_PATCH_BYTES:
        return False
    return isinstance(value.get("patch_sha256"), str) and bool(
        _SHA256_RE.fullmatch(value["patch_sha256"])
    )


def trusted_patch_extraction_valid(
    value: Any,
    *,
    patch: str,
    snapshot: Any,
) -> bool:
    """Bind a current host extraction proof to its snapshot and exact patch."""
    if not _trusted_patch_extraction_shape_valid(value, snapshot=snapshot):
        return False
    encoded = patch.encode("utf-8", errors="surrogatepass")
    return bool(
        value.get("patch_bytes") == len(encoded)
        and value.get("patch_sha256") == hashlib.sha256(encoded).hexdigest()
    )


def current_generation_proof_valid(metric: Any, patch: str) -> bool:
    """Validate the two proofs required by every current generation record."""
    if not isinstance(metric, dict):
        return False
    snapshot = metric.get("solver_git_snapshot")
    return trusted_patch_extraction_valid(
        metric.get("trusted_patch_extraction"),
        patch=patch,
        snapshot=snapshot,
    )


def current_generation_summary_proof_valid(metric: Any) -> bool:
    """Validate proof shape and patch identity in a report row without patch text."""
    if not isinstance(metric, dict):
        return False
    snapshot = metric.get("solver_git_snapshot")
    extraction = metric.get("trusted_patch_extraction")
    if not _trusted_patch_extraction_shape_valid(extraction, snapshot=snapshot):
        return False
    patch_sha = metric.get("patch_sha256")
    return bool(
        isinstance(patch_sha, str)
        and _SHA256_RE.fullmatch(patch_sha)
        and extraction["patch_sha256"] == patch_sha
    )


__all__ = [
    "TRUSTED_PATCH_EXTRACTION_SCHEMA",
    "MAX_TRUSTED_PATCH_BYTES",
    "MAX_WORKSPACE_ARCHIVE_BYTES",
    "MAX_WORKSPACE_ARCHIVE_ENTRIES",
    "MAX_WORKSPACE_EXTRACTED_BYTES",
    "MAX_WORKSPACE_FILE_BYTES",
    "current_generation_proof_valid",
    "current_generation_summary_proof_valid",
    "solver_git_snapshot_valid",
    "trusted_patch_extraction_valid",
]
