"""Atomic local report publication for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from opencollab.adapters.safe_files import write_regular_bytes_atomic


def _local_report_target_expectation(path: Path) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"report destination must be regular or absent: {path}")
    if before is None:
        return {"require_target_absent": True}
    return {"expected_target_identity": (before.st_dev, before.st_ino)}


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    if os.path.abspath(json_path) == os.path.abspath(md_path):
        raise ValueError("JSON and Markdown reports must use different paths")
    bundle_id = uuid.uuid4().hex
    bundled_summary = {**summary, "local_report_bundle_id": bundle_id}
    json_payload = (json.dumps(bundled_summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    markdown = markdown.rstrip("\n") + f"\n\n<!-- local_report_bundle_id:{bundle_id} -->\n"
    json_expectation = _local_report_target_expectation(json_path)
    md_expectation = _local_report_target_expectation(md_path)
    # JSON is the bundle commit marker. A reader accepts the pair only when its
    # bundle id matches the Markdown marker, so publish Markdown first.
    write_regular_bytes_atomic(
        md_path,
        markdown.encode("utf-8"),
        **md_expectation,
    )
    write_regular_bytes_atomic(json_path, json_payload, **json_expectation)


__all__ = [name for name in globals() if not name.startswith("__")]
