from pathlib import Path


def test_domain_modules_do_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    domain_files = [
        package_root / "opencollab/domain/agent.py",
        package_root / "opencollab/domain/session.py",
        package_root / "opencollab/domain/tools.py",
        package_root / "opencollab/domain/compaction.py",
        package_root / "opencollab/domain/events.py",
    ]
    forbidden = [
        "opencollab.core",
        "opencollab.application",
        "opencollab.tools",
        "opencollab.bootstrap",
        "opencollab.cli",
        "opencollab.adapters",
        "opencollab.team",
    ]

    for path in domain_files:
        source = path.read_text(encoding="utf-8")
        for import_path in forbidden:
            assert import_path not in source


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
