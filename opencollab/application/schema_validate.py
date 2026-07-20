"""Minimal stdlib JSON-Schema validator for workflow structured output.

A deliberately tiny subset — ``type`` (object/array/string/number/integer/
boolean), ``required``, ``properties``, ``items``, and ``enum`` — enough to
shape one-shot agent output without pulling ``jsonschema`` (a third-party
import is forbidden in the application layer). ``validate`` returns a list of
human-readable error strings; an empty list means the value conforms.

Pure application layer: stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

# Maps a JSON-Schema ``type`` name to the predicate that accepts a value of it.
# ``bool`` is intentionally excluded from integer/number (Python ``bool`` is an
# ``int`` subclass, but a boolean is not a valid number for our purposes).
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
    ),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any]) -> list[str]:
    """Validate ``value`` against ``schema``; return a list of error strings.

    An empty list means the value is valid. Errors are collected (not raised)
    and carry a dotted path so the model can self-correct from the message.
    """
    errors: list[str] = []
    _validate_schema(schema, "$schema", errors)
    if errors:
        return errors
    _validate(value, schema, "$", errors)
    return errors


def _validate_schema(schema: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema node must be an object")
        return
    expected_type = schema.get("type")
    if expected_type is not None:
        members = (
            list(expected_type)
            if isinstance(expected_type, Sequence) and not isinstance(expected_type, str)
            else [expected_type]
        )
        if not members or any(
            not isinstance(member, str) or member not in _TYPE_CHECKS
            for member in members
        ):
            errors.append(f"{path}.type: unsupported schema type {expected_type!r}")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(field, str) for field in required
    ):
        errors.append(f"{path}.required: must be an array of property names")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        errors.append(f"{path}.properties: must be an object")
    else:
        for name, child in properties.items():
            _validate_schema(child, f"{path}.properties.{name}", errors)
    if "items" in schema:
        _validate_schema(schema["items"], f"{path}.items", errors)


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _check_type(value, expected_type):
        errors.append(f"{path}: expected type '{expected_type}', got '{_type_name(value)}'")
        # Type mismatch makes deeper checks meaningless for this node.
        return

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: value {value!r} not in enum {enum!r}")

    if expected_type == "object" or (expected_type is None and isinstance(value, dict)):
        _validate_object(value, schema, path, errors)
    elif expected_type == "array" or (expected_type is None and isinstance(value, list)):
        _validate_array(value, schema, path, errors)


def _validate_object(
    value: dict[str, Any], schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    for field in schema.get("required", []):
        if field not in value:
            errors.append(f"{path}.{field}: required property is missing")

    properties = schema.get("properties", {})
    for prop_name, prop_schema in properties.items():
        if prop_name in value:
            _validate(value[prop_name], prop_schema, f"{path}.{prop_name}", errors)


def _validate_array(
    value: list[Any], schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    item_schema = schema.get("items")
    if item_schema is None:
        return
    for index, item in enumerate(value):
        _validate(item, item_schema, f"{path}[{index}]", errors)


def _check_type(value: Any, expected_type: Any) -> bool:
    # JSON Schema allows ``type`` to be a list of names meaning "any of these".
    if isinstance(expected_type, Sequence) and not isinstance(expected_type, str):
        return any(_check_type(value, member) for member in expected_type)
    check = _TYPE_CHECKS.get(expected_type)
    if check is None:
        return False
    return check(value)


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


__all__ = ["validate"]
