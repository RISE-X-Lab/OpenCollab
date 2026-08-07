"""Repo map generation + startup injection into the project context layer.

A bounded repo map answers the "where is everything" question once, at zero
per-call cost — instead of mid-tier backends burning their early budget on
ls/find exploration. The map ships as the PROJECT layer's startup content in
the system prompt; when no map is available the layer stays a
registered-but-deferred source (the pre-existing behavior).
"""

from __future__ import annotations

import asyncio

from opencollab.adapters import repo_map as repo_map_module
from opencollab.adapters.env import ExecResult, LocalEnvironment
from opencollab.adapters.repo_map import (
    MAP_HEADER,
    build_repo_map,
    build_repo_map_via_env,
)
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.session_factory import DefaultSessionFactory
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.domain.context import ContextPosition
from opencollab.domain.team import Topology


def run(coro):
    return asyncio.run(coro)


# --- build_repo_map: local walker -------------------------------------------


def _workspace(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "core.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text("def test(): pass\n")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "core.cpython-311.pyc").write_text("")
    return tmp_path


def test_build_repo_map_renders_bounded_tree(tmp_path):
    result = build_repo_map(str(_workspace(tmp_path)))

    assert result.startswith(MAP_HEADER)
    assert "src/" in result
    assert "  pkg/" in result
    assert "    core.py" in result
    assert "README.md" in result


def test_build_repo_map_skips_hidden_and_junk_dirs(tmp_path):
    result = build_repo_map(str(_workspace(tmp_path)))

    assert ".git" not in result
    assert "__pycache__" not in result


def test_build_repo_map_keeps_common_hidden_project_configuration(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("", encoding="utf-8")
    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text("", encoding="utf-8")
    (tmp_path / ".pre-commit-config.yaml").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    result = build_repo_map(str(tmp_path))

    assert ".github/" in result
    assert "ci.yml" in result
    assert ".devcontainer/" in result
    assert ".pre-commit-config.yaml" in result
    assert ".env" not in result
    assert not any(line.strip().startswith(".git/") for line in result.splitlines())


def test_build_repo_map_respects_max_depth(tmp_path):
    result = build_repo_map(str(_workspace(tmp_path)), max_depth=2)

    assert "  pkg/" in result            # depth 2: listed
    assert "\n    core.py" not in result  # depth 3: below the cap


def test_build_repo_map_caps_total_chars(tmp_path):
    for i in range(200):
        (tmp_path / f"module_with_a_long_name_{i:03}.py").write_text("")

    result = build_repo_map(str(tmp_path), max_chars=500)

    assert "truncated" in result
    assert len(result) < 800


def test_build_repo_map_truncates_only_at_complete_lines(tmp_path):
    names = {f"module_with_a_long_name_{index:03}.py" for index in range(40)}
    for name in names:
        (tmp_path / name).write_text("", encoding="utf-8")

    result = build_repo_map(str(tmp_path), max_chars=300)

    for line in result.splitlines()[2:]:
        entry = line.strip()
        if not entry or "truncated" in entry:
            continue
        assert entry in names or entry.startswith("... (")


def test_build_repo_map_caps_entries_per_dir(tmp_path):
    for i in range(40):
        (tmp_path / f"f{i:02}.py").write_text("")

    result = build_repo_map(str(tmp_path))

    assert "... (10 more)" in result


def test_build_repo_map_stops_scanning_when_global_budget_is_exhausted(
    tmp_path,
    monkeypatch,
):
    for index in range(500):
        (tmp_path / f"entry-{index:04}.txt").write_text("")
    real_scandir = repo_map_module.os.scandir
    scanned = 0

    class CountingScandir:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal scanned
            entry = next(self._inner)
            scanned += 1
            return entry

    monkeypatch.setattr(repo_map_module.os, "scandir", CountingScandir)

    result = build_repo_map(
        str(tmp_path),
        max_entries=5,
        max_dirs=2,
        max_scanned_entries=32,
    )

    assert scanned <= 32
    assert "truncated" in result


def test_build_repo_map_reserves_root_budget_for_project_files(tmp_path):
    for index in range(31):
        (tmp_path / f"directory-{index:02}").mkdir()
    for name in ("README.md", "pyproject.toml", "team.yaml"):
        (tmp_path / name).write_text("", encoding="utf-8")

    result = build_repo_map(str(tmp_path))

    assert "README.md" in result
    assert "pyproject.toml" in result
    assert "team.yaml" in result
    assert "directory-00/" in result


def test_build_repo_map_missing_or_empty_workspace_returns_empty(tmp_path):
    assert build_repo_map(str(tmp_path / "nope")) == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert build_repo_map(str(empty)) == ""


# --- build_repo_map_via_env: find-based (Docker-safe) ------------------------


class _FakeEnv:
    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        raises: bool = False,
        stderr: str = "",
    ):
        self._result = ExecResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        self._raises = raises
        self.cmds: list[str] = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        if self._raises:
            raise RuntimeError("no exec")
        self.cmds.append(cmd)
        return self._result


def test_build_repo_map_via_env_formats_find_output():
    env = _FakeEnv(stdout=".\n./src\n./src/core.py\n./README.md\n")

    result = run(build_repo_map_via_env(env))

    assert result.startswith(MAP_HEADER)
    assert "src/core.py" in result
    assert "README.md" in result
    assert "\n.\n" not in result
    assert "find . -mindepth 1 -maxdepth" in env.cmds[0]


def test_build_repo_map_via_env_limits_enumeration_work() -> None:
    env = _FakeEnv(
        stdout="\n".join(f"./path-{index:03}" for index in range(20)) + "\n"
    )

    result = run(build_repo_map_via_env(env, max_entries=5))

    assert "path-004" in result
    assert "path-005" not in result
    assert "truncated" in result
    assert "| head -n 6" in env.cmds[0]
    assert "| sort" not in env.cmds[0]


def test_build_repo_map_via_env_checks_find_before_sorting():
    env = _FakeEnv(stdout="./zeta.py\n./alpha.py\n")

    result = run(build_repo_map_via_env(env))

    assert "| sort" not in env.cmds[0]
    assert result.index("alpha.py") < result.index("zeta.py")


def test_build_repo_map_via_env_rejects_find_diagnostics():
    env = _FakeEnv(
        stdout="./visible.py\n",
        stderr="find: unreadable directory",
    )

    assert run(build_repo_map_via_env(env)) == ""


def test_build_repo_map_via_env_filters_hidden_paths_consistently():
    env = _FakeEnv(
        stdout=(
            "./.github\n"
            "./.github/workflows\n"
            "./.github/workflows/ci.yml\n"
            "./.env\n"
            "./.git/HEAD\n"
        )
    )

    result = run(build_repo_map_via_env(env))

    assert ".github/workflows/ci.yml" in result
    assert ".env" not in result
    assert not any(line.strip().startswith(".git/") for line in result.splitlines())


def test_build_repo_map_via_env_failure_or_empty_returns_empty():
    assert run(build_repo_map_via_env(_FakeEnv(returncode=1, stdout="x"))) == ""
    assert run(build_repo_map_via_env(_FakeEnv(stdout=".\n"))) == ""
    assert run(build_repo_map_via_env(_FakeEnv(raises=True))) == ""


def test_build_repo_map_via_env_works_against_local_environment(tmp_path):
    _workspace(tmp_path)
    env = LocalEnvironment(str(tmp_path))

    result = run(build_repo_map_via_env(env))

    assert "src/pkg/core.py" in result
    assert ".git" not in result


# --- injection into the PROJECT context layer --------------------------------


def _spawn_cfg() -> SpawnConfig:
    return SpawnConfig(
        model="gpt-4o",
        provider="openai",
        api_key="test-key",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(),
        permission_policy=None,
    )


def _team() -> TeamConfig:
    return TeamConfig(
        roles={"lead": RoleConfig(prompt="Lead.", model=None, tools=["bash"])},
        topology=Topology(),
    )


def test_project_context_ships_as_startup_system_source():
    repo_map = f"{MAP_HEADER}\n\nsrc/core.py"
    builder = ContextBuilder(_team(), _spawn_cfg(), project_context=repo_map)

    plan = builder.build_plan("lead")
    project = next(s for s in plan.sources if s.name == "project")
    assert project.position is ContextPosition.SYSTEM
    assert project.content == repo_map

    agent = builder.build_agent("lead", plan=plan)
    assert MAP_HEADER in agent.system_prompt
    assert "src/core.py" in agent.system_prompt


def test_without_project_context_no_project_source_is_emitted():
    # No repo map → the project layer is simply absent (an honest gap), not a
    # registered-but-empty deferred placeholder.
    plan = ContextBuilder(_team(), _spawn_cfg()).build_plan("lead")
    assert not any(s.name == "project" for s in plan.sources)


def test_session_factory_injects_lead_workspace_repo_map(tmp_path):
    ws = _workspace(tmp_path)
    factory = DefaultSessionFactory(_spawn_cfg(), lead_workspace=str(ws))

    session = factory.build_spawn_session(
        role="coder", env=LocalEnvironment(str(ws)), budget=10_000, aid=1
    )

    assert MAP_HEADER in session.agent.system_prompt
    assert "core.py" in session.agent.system_prompt


def test_session_factory_refreshes_repo_map_for_each_new_session(tmp_path):
    ws = _workspace(tmp_path)
    factory = DefaultSessionFactory(_spawn_cfg(), lead_workspace=str(ws))
    first = factory.build_spawn_session(
        role="coder",
        env=LocalEnvironment(str(ws)),
        budget=10_000,
        aid=1,
    )

    (ws / "created-after-first-session.py").write_text("", encoding="utf-8")
    second = factory.build_spawn_session(
        role="coder",
        env=LocalEnvironment(str(ws)),
        budget=10_000,
        aid=2,
    )

    assert "created-after-first-session.py" not in first.agent.system_prompt
    assert "created-after-first-session.py" in second.agent.system_prompt


def test_session_factory_without_workspace_injects_no_map(tmp_path):
    factory = DefaultSessionFactory(_spawn_cfg())

    session = factory.build_spawn_session(
        role="coder", env=LocalEnvironment(str(tmp_path)), budget=10_000, aid=1
    )

    assert MAP_HEADER not in session.agent.system_prompt
