from pathlib import Path

import opencollab.core.session as core_session
from opencollab.core.session import compactor as core_compactor
from opencollab.core.session import state as core_state
from opencollab.core.session import tools as core_tools
from opencollab.domain import compaction as domain_compaction
from opencollab.domain import session as domain_session
from opencollab.domain import tools as domain_tools


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


def test_core_session_reexports_domain_value_objects():
    assert core_session.SessionPhase is domain_session.SessionPhase
    assert core_session.SessionState is domain_session.SessionState
    assert core_session.ToolProcessingResult is domain_tools.ToolProcessingResult
    assert core_session.CompactResult is domain_compaction.CompactResult


def test_legacy_core_session_modules_reexport_domain_value_objects():
    assert core_state.SessionPhase is domain_session.SessionPhase
    assert core_state.SessionState is domain_session.SessionState
    assert core_tools.ToolProcessingResult is domain_tools.ToolProcessingResult
    assert core_tools.MAX_CALL_HASH_WINDOW == domain_tools.MAX_CALL_HASH_WINDOW
    assert core_compactor.CompactResult is domain_compaction.CompactResult


def test_tool_base_satisfies_tool_spec():
    from opencollab.domain.tools import ToolSpec
    from opencollab.tools.base import Tool

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
