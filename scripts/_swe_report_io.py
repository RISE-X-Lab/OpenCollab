"""Bounded, descriptor-safe I/O shared by SWE report scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "opencollab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "opencollab"))

from opencollab.adapters import retirement_registry, safe_files  # noqa: E402

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 128 * 1024 * 1024


def ensure_directory(path: Path) -> None:
    """Create one directory hierarchy while rejecting symlink components."""
    safe_files.ensure_directory_no_symlinks(path)


def configure_retirement_registry(output_dir: Path) -> str:
    """Share verified retirement identities across report writer processes."""
    state_dir = output_dir / ".opencollab"
    ensure_directory(state_dir)
    return retirement_registry.configure_persistent_retirement_log(
        state_dir / "retirements.jsonl"
    )


def load_json_with_error(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read one bounded regular JSON object and retain the failure reason."""
    try:
        text = safe_files.read_regular_text(path, max_bytes=MAX_REPORT_BYTES)
        value = json.loads(text)
    except FileNotFoundError:
        return {}, "missing_report_file"
    except UnicodeDecodeError:
        return {}, "invalid_report_encoding"
    except json.JSONDecodeError:
        return {}, "invalid_report_json"
    except (OSError, ValueError):
        return {}, "unsafe_or_unstable_report_file"
    if not isinstance(value, dict):
        return {}, "report_root_not_object"
    return value, None


def load_json(path: Path) -> dict[str, Any]:
    """Read one bounded regular JSON object, returning an empty object on failure."""
    value, _error = load_json_with_error(path)
    return value


def read_text(path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> str:
    """Read one bounded regular text file without following symlinks."""
    return safe_files.read_regular_text(path, max_bytes=max_bytes)


def write_text(path: Path, value: str, *, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Atomically replace one regular text file without following symlinks."""
    payload = value.encode("utf-8")
    safe_files.write_regular_bytes_atomic(path, payload, max_bytes=max_bytes)


def write_json(path: Path, value: Any) -> None:
    """Atomically persist one bounded JSON report."""
    write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        max_bytes=MAX_REPORT_BYTES,
    )


__all__ = [
    "MAX_LOG_BYTES",
    "MAX_REPORT_BYTES",
    "configure_retirement_registry",
    "ensure_directory",
    "load_json",
    "load_json_with_error",
    "read_text",
    "write_json",
    "write_text",
]
