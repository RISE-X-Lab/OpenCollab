"""Minimal stdlib JSON-Schema validator for workflow structured output.

The supported subset covers the constraints that OpenCollab sends to providers:
``type``, ``required``, ``properties``, ``additionalProperties``, ``items``,
``enum``, string length/pattern bounds, numeric bounds, array length bounds,
and ``oneOf``. Unsupported assertion keywords are rejected during schema
validation instead of being silently claimed to a provider. ``validate``
returns a list of human-readable error strings; an empty list means the value
conforms.

Pure application layer: stdlib only.
"""

from __future__ import annotations

import json
import math
import re
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

_SUPPORTED_KEYWORDS = frozenset({
    "additionalProperties",
    "description",
    "enum",
    "examples",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "oneOf",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
})


def validate_schema(schema: Any) -> list[str]:
    """Return deterministic errors for unsupported or malformed schema nodes."""
    errors: list[str] = []
    _validate_schema(schema, "$schema", errors)
    return errors


def validate(value: Any, schema: dict[str, Any]) -> list[str]:
    """Validate ``value`` against ``schema``; return a list of error strings.

    An empty list means the value is valid. Errors are collected (not raised)
    and carry a dotted path so the model can self-correct from the message.
    """
    errors = validate_schema(schema)
    if errors:
        return errors
    _validate(value, schema, "$", errors)
    return errors


def _validate_schema(schema: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema node must be an object")
        return
    for keyword in schema:
        if keyword not in _SUPPORTED_KEYWORDS:
            errors.append(f"{path}.{keyword}: unsupported schema keyword")
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
    if "additionalProperties" in schema:
        additional_properties = schema["additionalProperties"]
        if isinstance(additional_properties, dict):
            _validate_schema(
                additional_properties,
                f"{path}.additionalProperties",
                errors,
            )
        elif not isinstance(additional_properties, bool):
            errors.append(f"{path}.additionalProperties: must be a boolean or schema object")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            errors.append(f"{path}.enum: must be a non-empty array of JSON values")
        else:
            try:
                json.dumps(enum, allow_nan=False)
            except (TypeError, ValueError):
                errors.append(f"{path}.enum: entries must be valid JSON values")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in schema:
            _validate_nonnegative_integer(schema[keyword], f"{path}.{keyword}", errors)
    for keyword in ("minimum", "maximum"):
        if keyword in schema and not _is_number(schema[keyword]):
            errors.append(f"{path}.{keyword}: must be a finite number")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            errors.append(f"{path}.pattern: must be a string")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{path}.pattern: invalid regular expression: {exc}")
    if "oneOf" in schema:
        one_of = schema["oneOf"]
        if not isinstance(one_of, list) or not one_of:
            errors.append(f"{path}.oneOf: must be a non-empty array of schema objects")
        else:
            for index, branch in enumerate(one_of):
                _validate_schema(branch, f"{path}.oneOf[{index}]", errors)


def _validate_nonnegative_integer(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{path}: must be a non-negative integer")


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _check_type(value, expected_type):
        errors.append(f"{path}: expected type '{expected_type}', got '{_type_name(value)}'")
        # Type mismatch makes deeper checks meaningless for this node.
        return

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: value {value!r} not in enum {enum!r}")

    one_of = schema.get("oneOf")
    if one_of is not None:
        matching_schemas = 0
        for branch in one_of:
            branch_errors: list[str] = []
            _validate(value, branch, path, branch_errors)
            if not branch_errors:
                matching_schemas += 1
        if matching_schemas != 1:
            errors.append(
                f"{path}: must match exactly one schema in oneOf "
                f"(matched {matching_schemas})"
            )

    if isinstance(value, str):
        _validate_string(value, schema, path, errors)
    if _is_number(value):
        _validate_number(value, schema, path, errors)

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
    additional_properties = schema.get("additionalProperties", True)
    for prop_name, prop_value in value.items():
        if prop_name in properties:
            continue
        if additional_properties is False:
            errors.append(f"{path}.{prop_name}: unexpected property")
        elif isinstance(additional_properties, dict):
            _validate(
                prop_value,
                additional_properties,
                f"{path}.{prop_name}",
                errors,
            )


def _validate_array(
    value: list[Any], schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    min_items = schema.get("minItems")
    if min_items is not None and len(value) < min_items:
        errors.append(f"{path}: must contain at least {min_items} items")
    max_items = schema.get("maxItems")
    if max_items is not None and len(value) > max_items:
        errors.append(f"{path}: must contain at most {max_items} items")
    item_schema = schema.get("items")
    if item_schema is None:
        return
    for index, item in enumerate(value):
        _validate(item, item_schema, f"{path}[{index}]", errors)


def _validate_string(value: str, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    min_length = schema.get("minLength")
    if min_length is not None and len(value) < min_length:
        errors.append(f"{path}: must have length >= {min_length}")
    max_length = schema.get("maxLength")
    if max_length is not None and len(value) > max_length:
        errors.append(f"{path}: must have length <= {max_length}")
    pattern = schema.get("pattern")
    if pattern is not None and re.search(pattern, value) is None:
        errors.append(f"{path}: must match pattern {pattern!r}")


def _validate_number(value: int | float, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        errors.append(f"{path}: must be >= {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        errors.append(f"{path}: must be <= {maximum}")


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


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


__all__ = ["validate", "validate_schema"]
