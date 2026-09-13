"""Provider-neutral validation for text messages and function tools."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from opencollab.domain.tools import validate_unique_tool_names

_FUNCTION_FIELDS = frozenset({"name", "description", "parameters", "strict"})
_STRING_TOOL_CHOICES = frozenset({"auto", "none", "required"})
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class NormalizedToolChoice:
    """One provider-independent tool selection."""

    mode: str
    name: str | None = None


def normalize_text_content(content: Any) -> str:
    """Flatten supported text content without dropping other modalities."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("message content must be text or a text block list")
    parts: list[str] = []
    for index, part in enumerate(content):
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            raise ValueError(
                f"message content block {index} must be text or an object"
            )
        part_type = part.get("type")
        if part_type not in {None, "text", "input_text", "output_text"}:
            raise ValueError(
                f"message content block {index} has unsupported type {part_type!r}"
            )
        text = part.get("text")
        if not isinstance(text, str):
            raise ValueError(f"message content block {index} is missing text")
        parts.append(text)
    return "\n".join(parts)


def normalize_function_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Validate and copy OpenAI-shaped function tool definitions."""
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")

    normalized: list[dict[str, Any]] = []
    names: list[str] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ValueError(f"tool {index} must be an object")
        unknown_tool_fields = set(tool) - {"type", "function"}
        if unknown_tool_fields:
            fields = ", ".join(sorted(map(str, unknown_tool_fields)))
            raise ValueError(f"tool {index} has unsupported field(s): {fields}")
        if tool.get("type", "function") != "function":
            raise ValueError(f"tool {index} must have type 'function'")

        function = tool.get("function")
        if not isinstance(function, dict):
            raise ValueError(f"tool {index}.function must be an object")
        unknown_function_fields = set(function) - _FUNCTION_FIELDS
        if unknown_function_fields:
            fields = ", ".join(sorted(map(str, unknown_function_fields)))
            raise ValueError(
                f"tool {index}.function has unsupported field(s): {fields}"
            )

        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tool {index}.function.name must be a non-empty string")
        if _TOOL_NAME_RE.fullmatch(name) is None:
            raise ValueError(
                f"tool {index}.function.name must contain 1-64 letters, digits, "
                "underscores, or hyphens"
            )
        description = function.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"tool {index}.function.description must be a string")
        parameters = function.get(
            "parameters", {"type": "object", "properties": {}}
        )
        if not isinstance(parameters, dict):
            raise ValueError(f"tool {index}.function.parameters must be an object")
        strict = function.get("strict")
        if "strict" in function and not isinstance(strict, bool):
            raise ValueError(f"tool {index}.function.strict must be a boolean")

        normalized_function = copy.deepcopy(function)
        normalized_function["parameters"] = copy.deepcopy(parameters)
        normalized.append({"type": "function", "function": normalized_function})
        names.append(name)

    validate_unique_tool_names(names)
    return normalized


def normalize_tool_choice(value: Any) -> NormalizedToolChoice | None:
    """Normalize supported string and named-function tool choices."""
    if value is None:
        return None
    if isinstance(value, str):
        if value not in _STRING_TOOL_CHOICES:
            raise ValueError(f"unsupported tool_choice {value!r}")
        return NormalizedToolChoice(value)
    if not isinstance(value, dict):
        raise ValueError("tool_choice must be a string or object")

    choice_type = value.get("type")
    if choice_type in {"auto", "none", "any"}:
        if set(value) != {"type"}:
            raise ValueError(f"tool_choice type {choice_type!r} has unsupported fields")
        mode = "required" if choice_type == "any" else choice_type
        return NormalizedToolChoice(mode)

    if choice_type == "function":
        if isinstance(value.get("function"), dict):
            if set(value) != {"type", "function"}:
                raise ValueError("named function tool_choice has unsupported fields")
            function = value["function"]
            if set(function) != {"name"}:
                raise ValueError("named function tool_choice requires only function.name")
            name = function.get("name")
        else:
            if set(value) != {"type", "name"}:
                raise ValueError("named function tool_choice requires only name")
            name = value.get("name")
    elif choice_type == "tool":
        if set(value) != {"type", "name"}:
            raise ValueError("named tool_choice requires only name")
        name = value.get("name")
    else:
        raise ValueError(f"unsupported tool_choice type {choice_type!r}")

    if not isinstance(name, str) or not name:
        raise ValueError("named tool_choice requires a non-empty name")
    return NormalizedToolChoice("named", name)


def validate_tool_choice_target(
    choice: NormalizedToolChoice | None,
    tools: list[dict[str, Any]],
) -> None:
    """Require forced choices to reference an available function tool."""
    if choice is None or choice.mode in {"auto", "none"}:
        return
    names = {tool["function"]["name"] for tool in tools}
    if choice.mode == "required" and not names:
        raise ValueError("tool_choice 'required' needs at least one tool")
    if choice.mode == "named" and choice.name not in names:
        raise ValueError(f"tool_choice names unavailable tool {choice.name!r}")
