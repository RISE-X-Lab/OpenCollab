"""JSON-schema validation and ``StructuredOutputTool`` contract tests."""

from __future__ import annotations

import pytest
from structured_output_test_support import runtime as _runtime

from opencollab.application.schema_validate import validate, validate_schema
from opencollab.application.structured_output import StructuredOutputTool


def test_validate_valid_object():
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    assert validate({"name": "ada", "age": 36}, schema) == []


def test_validate_missing_required():
    schema = {"type": "object", "required": ["name"], "properties": {}}
    errors = validate({}, schema)
    assert errors
    assert any("name" in e for e in errors)


def test_validate_wrong_type():
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
    errors = validate({"age": "old"}, schema)
    assert errors
    assert any("age" in e for e in errors)


def test_validate_enum_violation():
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
    }
    assert validate({"color": "red"}, schema) == []
    errors = validate({"color": "blue"}, schema)
    assert errors
    assert any("color" in e for e in errors)


@pytest.mark.parametrize("enum", ["abc", 3, {"x": 1}, [], [float("nan")]])
def test_validate_schema_rejects_malformed_enum(enum):
    errors = validate_schema({"type": "string", "enum": enum})
    assert errors
    assert any("enum" in error for error in errors)


def test_validate_schema_accepts_nonempty_json_enum():
    schema = {"enum": ["red", 3, None, {"kind": "nested"}, ["x"]]}
    assert validate_schema(schema) == []


def test_validate_nested_object_and_array():
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "integer"}},
                },
            },
        },
    }
    assert validate({"items": [{"id": 1}, {"id": 2}]}, schema) == []
    errors = validate({"items": [{"id": 1}, {"id": "two"}]}, schema)
    assert errors
    # missing required inside a nested array item
    errors2 = validate({"items": [{}]}, schema)
    assert errors2


def test_validate_boolean_and_number():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "ratio": {"type": "number"}},
    }
    assert validate({"ok": True, "ratio": 0.5}, schema) == []
    assert validate({"ok": True, "ratio": 3}, schema) == []  # int is a number
    assert validate({"ok": 1, "ratio": 0.5}, schema)  # bool!=int slot here


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_rejects_non_finite_json_numbers(value):
    assert validate(value, {"type": "number"})


def test_validate_rejects_unknown_schema_types_even_when_value_matches():
    errors = validate("anything", {"type": "mystery"})
    assert errors
    assert "unsupported schema type" in errors[0]


def test_validate_integer_rejects_bool():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert validate({"n": 5}, schema) == []
    assert validate({"n": True}, schema)


def test_validate_top_level_array():
    schema = {"type": "array", "items": {"type": "string"}}
    assert validate(["a", "b"], schema) == []
    assert validate(["a", 2], schema)


def test_validate_union_type_list():
    # JSON Schema permits ``type`` to be a list ("any of these"). A union type
    # must not raise (unhashable list) and must accept any listed member.
    schema = {
        "type": "object",
        "properties": {"x": {"type": ["string", "null"]}},
    }
    assert validate({"x": "hi"}, schema) == []
    assert validate({"x": None}, schema) == []
    # a value matching none of the union members is rejected, not crashed
    assert validate({"x": 7}, schema)


# --------------------------------------------------------------------------- #
# StructuredOutputTool
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_captures_valid_payload():
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    tool = StructuredOutputTool(schema)
    assert tool.captured is None

    result = await tool.execute_with_runtime({"x": 7}, _runtime())

    assert tool.captured == {"x": 7}
    assert "x" in result.lower() or "record" in result.lower() or result


@pytest.mark.asyncio
async def test_tool_returns_errors_on_invalid_payload():
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    tool = StructuredOutputTool(schema)

    result = await tool.execute_with_runtime({"x": "nope"}, _runtime())

    assert tool.captured is None
    assert "x" in result  # error string mentions the offending field


@pytest.mark.asyncio
async def test_tool_schema_surface():
    schema = {"type": "object", "properties": {}}
    tool = StructuredOutputTool(schema)
    assert tool.name == "structured_output"
    assert isinstance(tool.description, str) and tool.description
    openai = tool.to_openai_schema()
    assert openai["function"]["name"] == "structured_output"
    # the tool's parameters expose the caller's schema as the input shape
    assert openai["function"]["parameters"] == schema


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
        {"type": ["object", "null"]},
        {"properties": {}},
    ],
)
def test_tool_rejects_non_object_top_level_schema(schema):
    with pytest.raises(ValueError, match="top-level type must be 'object'"):
        StructuredOutputTool(schema)


@pytest.mark.parametrize(
    ("schema", "value", "error_fragment"),
    [
        (
            {
                "type": ["object", "null"],
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
            {},
            "$.name: required property is missing",
        ),
        (
            {
                "type": ["null", "object"],
                "properties": {"name": {"type": "string"}},
            },
            {"name": 7},
            "$.name: expected type 'string'",
        ),
        (
            {
                "type": ["array", "null"],
                "items": {"type": "integer"},
            },
            [1, "two"],
            "$[1]: expected type 'integer'",
        ),
    ],
)
def test_validate_union_type_still_applies_container_constraints(
    schema, value, error_fragment
):
    errors = validate(value, schema)
    assert any(error_fragment in error for error in errors)
    assert validate(None, schema) == []


@pytest.mark.parametrize("value", [1, 1.0, -0.0])
def test_validate_integer_accepts_mathematically_integral_numbers(value):
    assert validate(value, {"type": "integer"}) == []


@pytest.mark.parametrize("value", [1.5, True, False])
def test_validate_integer_rejects_non_integral_or_boolean_values(value):
    assert validate(value, {"type": "integer"})


@pytest.mark.parametrize(
    ("value", "enum", "matches"),
    [
        (1, [True], False),
        (0, [False], False),
        (True, [1], False),
        (False, [0], False),
        (1.0, [1], True),
        ({"x": 1}, [{"x": True}], False),
        ([0], [[False]], False),
    ],
)
def test_validate_enum_uses_json_type_aware_equality(value, enum, matches):
    errors = validate(value, {"enum": enum})
    assert (errors == []) is matches


# --------------------------------------------------------------------------- #
# agent(schema=...)
# --------------------------------------------------------------------------- #
