"""Message content conversion helpers for the Responses adapter."""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.adapters.llm.tool_contracts import normalize_text_content


def message_text(content: Any) -> str:
    try:
        return normalize_text_content(content)
    except ValueError as exc:
        raise ResponsesProtocolError(str(exc)) from exc


def message_content_parts(content: Any) -> str | list[dict[str, Any]]:
    """Project legacy text/image blocks into Responses input content."""
    if not isinstance(content, list):
        return message_text(content)
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            raise ResponsesProtocolError("message content contains an unsupported block")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "input_text", "text": part["text"]})
            continue
        if kind == "image_url" and isinstance(part.get("image_url"), dict):
            image = part["image_url"]
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise ResponsesProtocolError("image_url block is missing url")
            item = {"type": "input_image", "image_url": url}
            if image.get("detail") is not None:
                item["detail"] = image["detail"]
            parts.append(item)
            continue
        raise ResponsesProtocolError(f"unsupported message content block {kind!r}")
    return parts


def function_call_identity(call_id: Any, name: Any, arguments: Any) -> tuple[str, str, str]:
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ResponsesProtocolError("function call is missing call_id or name")
    if not isinstance(arguments, str):
        raise ResponsesProtocolError("function call arguments must be JSON text")
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise ResponsesProtocolError("function call arguments contain invalid JSON") from exc
    normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return call_id, name, normalized
