"""Stable registry for fail-closed retirement of integration-owned files."""

from __future__ import annotations

from opencollab.adapters.retirement_registry import (
    INTERNAL_RETIREMENT_LOG_ENV,
    INTERNAL_RETIREMENT_WORKSPACE_ENV,
    MAX_RETIREMENT_LOG_BYTES,
    MAX_RETIREMENT_LOG_RECORDS,
    RETIRED_FILE_PREFIX,
    configure_persistent_retirement_log,
    forget_verified_retirements,
    initialize_persistent_retirement_log,
    register_verified_retirement,
    registered_retirement_paths,
    verified_retirement_identities,
)

__all__ = [
    "INTERNAL_RETIREMENT_LOG_ENV",
    "INTERNAL_RETIREMENT_WORKSPACE_ENV",
    "MAX_RETIREMENT_LOG_BYTES",
    "MAX_RETIREMENT_LOG_RECORDS",
    "RETIRED_FILE_PREFIX",
    "configure_persistent_retirement_log",
    "forget_verified_retirements",
    "initialize_persistent_retirement_log",
    "register_verified_retirement",
    "registered_retirement_paths",
    "verified_retirement_identities",
]
