from __future__ import annotations

import hashlib

from opencollab.harness.swe_generation_proof import (
    MAX_TRUSTED_PATCH_BYTES,
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_EXTRACTED_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
    TRUSTED_PATCH_EXTRACTION_SCHEMA,
)


def trusted_summary_proof_fields(
    patch_sha256: str,
    *,
    patch_bytes: int = 1,
) -> dict:
    snapshot = {
        "enabled": True,
        "anonymous_head": "a" * 40,
        "base_tree": "b" * 40,
        "commit_count": 1,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": 0,
    }
    extraction = {
        "schema": TRUSTED_PATCH_EXTRACTION_SCHEMA,
        "host_trusted": True,
        "fixed_anonymous_base": snapshot["anonymous_head"],
        "base_tree": snapshot["base_tree"],
        "archive_bounded": True,
        "baseline_archive_sha256": "c" * 64,
        "baseline_archive_bytes": 10,
        "baseline_archive_entries": 1,
        "baseline_extracted_bytes": 1,
        "workspace_archive_sha256": "d" * 64,
        "workspace_archive_bytes": 10,
        "workspace_archive_entries": 1,
        "workspace_extracted_bytes": 1,
        "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
        "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
        "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
        "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
        "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
        "container_quiesced_before": True,
        "container_quiesced_after": True,
        "patch_sha256": patch_sha256,
        "patch_bytes": patch_bytes,
    }
    return {
        "solver_git_snapshot": snapshot,
        "trusted_patch_extraction": extraction,
    }


def trusted_patch_proof_fields(patch: str) -> dict:
    encoded = patch.encode("utf-8", errors="surrogatepass")
    return trusted_summary_proof_fields(
        hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    )
