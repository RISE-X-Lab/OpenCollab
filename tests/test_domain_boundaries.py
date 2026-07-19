import re
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "opencollab"

# The domain is the innermost layer: it may import nothing else in the package
# (application included — the rule that the application-boundary glob does not
# cover). Match real import statements only; a docstring mention is fine.
_FORBIDDEN = re.compile(
    r"^\s*(?:from|import)\s+opencollab\."
    r"(?:core|application|tools|bootstrap|cli|adapters|team)\b",
    re.MULTILINE,
)


def test_domain_modules_do_not_import_outer_layers():
    offenders = [
        str(p.relative_to(_PACKAGE_ROOT))
        for p in (_PACKAGE_ROOT / "domain").rglob("*.py")
        if _FORBIDDEN.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


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
