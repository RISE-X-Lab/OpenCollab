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
        package_root / "opencollab/domain/session.py",
        package_root / "opencollab/domain/tools.py",
        package_root / "opencollab/domain/compaction.py",
    ]
    forbidden = [
        "opencollab.core",
        "opencollab.application",
        "opencollab.tools",
        "opencollab.bootstrap",
        "opencollab.cli",
        "opencollab.tui",
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
