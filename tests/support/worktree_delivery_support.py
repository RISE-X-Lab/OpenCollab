"""Real Git worktrees behind an in-process Docker transport for delivery tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_process import ProcessResult, run_process

CONTAINER_ID = "c" * 64


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "tests@example.com")
    git(path, "config", "user.name", "OpenCollab Tests")
    (path / "initial.txt").write_text("initial\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "fixture")
    return path


def apply_and_execute(repo: Path, patch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "apply", "-"], input=patch, text=True,
        check=True, capture_output=True,
    )
    executed = subprocess.run(
        ["python3", str(repo / "solution.py")], check=True, capture_output=True, text=True,
    )
    assert executed.stdout == "42\n"


@pytest.fixture
def worktree_transport(monkeypatch):
    calls: list[list[str]] = []

    async def docker_exec(command, **kwargs):
        argv = list(command)
        calls.append(argv)
        assert argv[0] == "docker", argv
        if argv[1] == "inspect":
            return ProcessResult(0, f"{CONTAINER_ID}\t/oc-test\ttrue".encode(), b"")
        assert argv[1] == "exec", argv
        rest = argv[2:]
        workdir = None
        environment = os.environ.copy()
        while rest and rest[0] != "--":
            flag = rest.pop(0)
            if flag == "-w":
                workdir = rest.pop(0)
            elif flag == "-e":
                key, value = rest.pop(0).split("=", 1)
                environment[key] = value
            else:
                assert flag == "-i", argv
        assert rest[:2] == ["--", CONTAINER_ID], rest
        return await run_process(
            tuple(rest[2:]), shell=False, cwd=workdir, env=environment,
            timeout=kwargs.get("timeout", 60.0), input_bytes=kwargs.get("input_bytes"),
            output_limit=kwargs.get("output_limit"),
        )

    monkeypatch.setattr(docker_module, "run_process", docker_exec)
    return calls
