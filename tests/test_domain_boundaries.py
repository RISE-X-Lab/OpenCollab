"""Structural tests for the domain layer's published shape.

The dependency rule itself (nothing inward may import outward) is enforced by
``lint-imports`` against the contracts in ``.importlinter``, which walks the
real import graph and therefore also catches indirect edges and cycles. What
stays here is the part an import graph cannot check: that the concrete tool
adapter and the application-layer port still satisfy the domain's ``ToolSpec``.
"""


def test_tool_base_satisfies_tool_spec():
    from opencollab.adapters.tools.base import Tool
    from opencollab.domain.tools import ToolSpec

    instance: ToolSpec = Tool()
    assert isinstance(instance.name, str)
    assert isinstance(instance.description, str)
    assert isinstance(instance.parameters, dict)
    assert callable(instance.to_openai_schema)


def test_tool_port_carries_tool_spec_schema_surface():
    from opencollab.application.ports import ToolPort
    from opencollab.domain.tools import ToolSpec

    required_annotations = {"name", "description", "parameters"}
    assert required_annotations.issubset(ToolSpec.__annotations__)
    assert required_annotations.issubset(ToolPort.__annotations__)
    assert callable(ToolSpec.__dict__["to_openai_schema"])
    assert callable(ToolPort.__dict__["to_openai_schema"])
