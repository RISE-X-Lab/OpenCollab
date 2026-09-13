"""Message content conversion helpers for the Responses adapter."""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.llm.responses_errors import ResponsesProtocolError
from opencollab.adapters.llm.tool_contracts import normalize_text_content
from opencollab.adapters.llm.types import to_plain_data

OUTPUT_ITEM_TYPES = frozenset({"message", "reasoning", "function_call"})


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
        # Snapshots can contain either the legacy ``text`` discriminator or
        # native Responses input/output text blocks.  EasyInputMessageParam
        # uses ``input_text`` for both user and assistant history; normalize
        # all three accepted spellings instead of replaying an unsupported
        # ``output_text`` item into a new request.
        if kind in {"text", "input_text", "output_text"} and isinstance(
            part.get("text"), str
        ):
            parts.append({"type": "input_text", "text": part["text"]})
            continue
        if kind == "image_url":
            image = part.get("image_url")
            if isinstance(image, str):
                url = image
                detail = None
            elif isinstance(image, dict):
                url = image.get("url")
                detail = image.get("detail")
            else:
                url = None
                detail = None
            if not isinstance(url, str) or not url:
                raise ResponsesProtocolError("image_url block is missing url")
            item = {"type": "input_image", "image_url": url}
            if detail is not None:
                item["detail"] = detail
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


def validated_response_items(value: Any) -> list[dict[str, Any]]:
    """Validate provider-owned items before storing or replaying them."""
    if not isinstance(value, list):
        raise ResponsesProtocolError("response_items must be a list")
    items: list[dict[str, Any]] = []
    for raw in value:
        item = to_plain_data(raw)
        if not isinstance(item, dict) or item.get("type") not in OUTPUT_ITEM_TYPES:
            raise ResponsesProtocolError("response_items contain an unsupported item")
        items.append(item)
    return items


def _verify_replay_tool_calls(
    message: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    if "tool_calls" not in message:
        return
    legacy = []
    for call in message.get("tool_calls") or ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise ResponsesProtocolError("legacy tool call is missing function data")
        legacy.append(
            function_call_identity(
                call.get("id"),
                function.get("name"),
                function.get("arguments") or "{}",
            )
        )
    replayed = [
        function_call_identity(
            item.get("call_id"),
            item.get("name"),
            item.get("arguments"),
        )
        for item in items
        if item["type"] == "function_call"
    ]
    if legacy != replayed:
        raise ResponsesProtocolError("tool_calls and response_items disagree")


def messages_to_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert stored chat messages into native Responses input items."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    answered_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = message_text(message.get("content"))
            if text:
                if message.get("compacted"):
                    items.append({"role": "user", "content": text})
                else:
                    instructions.append(text)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ResponsesProtocolError("function_call_output is missing call_id")
            if call_id not in call_ids:
                raise ResponsesProtocolError(f"function_call_output has no matching call_id {call_id!r}")
            if call_id in answered_ids:
                raise ResponsesProtocolError(f"duplicate function_call_output for {call_id!r}")
            answered_ids.add(call_id)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": message_text(message.get("content")),
                }
            )
            continue
        replay = message.get("response_items")
        if replay is not None:
            replay_items = validated_response_items(replay)
            _verify_replay_tool_calls(message, replay_items)
            for item in replay_items:
                if item["type"] != "function_call":
                    continue
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ResponsesProtocolError("replayed function_call is missing call_id")
                if call_id in call_ids:
                    raise ResponsesProtocolError(f"duplicate replayed call_id {call_id!r}")
                call_ids.add(call_id)
            items.extend(replay_items)
            continue
        if role not in {"user", "assistant"}:
            raise ResponsesProtocolError(f"unsupported message role {role!r}")
        content = message_content_parts(message.get("content"))
        if content:
            item = {"role": role, "content": content}
            if role == "assistant" and message.get("phase") is not None:
                phase = message["phase"]
                if phase not in {"commentary", "final_answer"}:
                    raise ResponsesProtocolError(
                        f"unsupported assistant phase {phase!r}"
                    )
                item["phase"] = phase
            items.append(item)
        for call in message.get("tool_calls") or ():
            function = call.get("function") if isinstance(call, dict) else None
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(call_id, str) or not call_id:
                raise ResponsesProtocolError("legacy tool call is missing function data or call_id")
            if call_id in call_ids:
                raise ResponsesProtocolError(f"duplicate legacy call_id {call_id!r}")
            call_ids.add(call_id)
            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "{}",
                }
            )
    return ("\n\n".join(instructions) or None), items


__all__ = [
    "OUTPUT_ITEM_TYPES",
    "function_call_identity",
    "message_content_parts",
    "message_text",
    "messages_to_input",
    "validated_response_items",
]
