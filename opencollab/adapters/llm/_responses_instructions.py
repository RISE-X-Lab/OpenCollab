"""Optional provider instruction validation before model output delivery."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.adapters.llm.types import to_plain_data


def _check_instructions_echo(response: Any, request: dict[str, Any]) -> None:
    """Reject a known provider instruction rewrite before delivering tools."""
    if os.environ.get("OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO") != "1":
        return
    expected = request.get("instructions")
    actual = getattr(response, "instructions", None)
    matched = isinstance(expected, str) and (
        actual == expected or (expected == "" and actual is None)
    )
    audit_dir = os.environ.get("OPENCOLLAB_INSTRUCTIONS_AUDIT_DIR")
    if audit_dir:
        identity = str(getattr(response, "id", "missing-response-id"))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", identity)
        directory = Path(audit_dir)
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "response_id": identity,
            "requested_model": request.get("model"),
            "actual_model": getattr(response, "model", None),
            "requested_reasoning": request.get("reasoning"),
            "actual_reasoning": to_plain_data(getattr(response, "reasoning", None)),
            "instructions_match": matched,
            "expected_instructions": expected,
            "returned_instructions": actual,
            "usage": to_plain_data(getattr(response, "usage", None)),
            "validation_stage": "before_solver_delivery",
        }
        (directory / (name + ".json")).write_text(json.dumps(record, ensure_ascii=False) + "\n")
    if not matched:
        raise ResponsesProtocolError("provider response instructions differ from the submitted native instructions")
