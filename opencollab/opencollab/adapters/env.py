"""Environment — abstraction over execution contexts.

First Principle: All side effects go through Environment.
- LocalEnvironment: direct OS execution (daily dev)
- WorktreeEnvironment: isolated git worktree per spawned agent (parallel agents)
- DockerEnvironment: container sandbox (eval / SWE-bench)

Ref:
- Design doc: Environment base class with exec_cmd/read_file/write_file
- openclaw: sandbox.ts workspaceDir + readonlyRoots
- User feedback: parallel spawned agents MUST use separate physical workspaces (git worktree)
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


class Environment:
    """Abstract execution environment. All tools operate through this."""

    workspace: str = "."

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        raise NotImplementedError

    async def read_file(self, path: str) -> str:
        raise NotImplementedError

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    async def cleanup(self) -> None:
        """Clean up any temporary resources."""
        pass


class LocalEnvironment(Environment):
    """Direct OS execution — for interactive CLI use."""

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecResult(
                returncode=proc.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ExecResult(returncode=-1, stdout="", stderr=f"Command timed out after {timeout}s")

    async def read_file(self, path: str) -> str:
        full = os.path.join(self.workspace, path) if not os.path.isabs(path) else path
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    async def write_file(self, path: str, content: str) -> None:
        full = os.path.join(self.workspace, path) if not os.path.isabs(path) else path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


class WorktreeEnvironment(Environment):
    """Isolated git worktree — for parallel spawned-agent execution.

    Each spawned agent gets a separate physical copy of the repo via
    `git worktree add`. After task completion, changes are collected as a
    diff patch.

    This solves the concurrency problem: spawned agents cannot corrupt each
    other's git state, env variables, or file locks.

    Ref: User feedback on blind spot #2 — parallel delegation must use
    separate physical workspaces, not just file locks.
    """

    def __init__(self, source_workspace: str, branch_name: str | None = None):
        import uuid

        self._source = os.path.abspath(source_workspace)
        # Use UUID to guarantee uniqueness even under parallel same-role delegation
        self._branch = branch_name or f"opencollab-wt-{uuid.uuid4().hex[:12]}"
        self._worktree_dir: str | None = None
        self._local_env: LocalEnvironment | None = None

    async def setup(self) -> str:
        """Create the worktree. Returns the worktree directory path."""
        self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-wt-")

        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "-b", self._branch, self._worktree_dir,
            cwd=self._source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Fallback: just copy the directory (non-git repos)
            shutil.rmtree(self._worktree_dir, ignore_errors=True)
            self._worktree_dir = tempfile.mkdtemp(prefix="opencollab-cp-")
            shutil.copytree(self._source, self._worktree_dir, dirs_exist_ok=True)

        self.workspace = self._worktree_dir
        self._local_env = LocalEnvironment(self._worktree_dir)
        return self._worktree_dir

    async def get_diff(self) -> str:
        """Get the diff of changes made in this worktree."""
        if not self._local_env:
            return ""
        result = await self._local_env.exec_cmd("git diff HEAD")
        return result.stdout

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        if not self._local_env:
            await self.setup()
        return await self._local_env.exec_cmd(cmd, timeout)

    async def read_file(self, path: str) -> str:
        if not self._local_env:
            await self.setup()
        return await self._local_env.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        if not self._local_env:
            await self.setup()
        await self._local_env.write_file(path, content)

    async def cleanup(self) -> None:
        """Remove worktree and temporary branch."""
        if self._worktree_dir and os.path.exists(self._worktree_dir):
            # Try git worktree remove first
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", "--force", self._worktree_dir,
                cwd=self._source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Clean up branch
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", self._branch,
                cwd=self._source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Fallback: remove directory
            if os.path.exists(self._worktree_dir):
                shutil.rmtree(self._worktree_dir, ignore_errors=True)


class DockerEnvironment(Environment):
    """Docker container sandbox — for eval / SWE-bench.

    Each eval task runs in an isolated container with the repo mounted.
    Ref: design doc Environment abstraction + Harness Engineering.
    """

    def __init__(self, image: str = "python:3.11-slim", workspace: str = "/workspace"):
        self._image = image
        self.workspace = workspace
        self._container_id: str | None = None

    async def setup(self, mount_dir: str | None = None) -> str:
        """Start a container. Optionally mount a local directory."""
        cmd = ["docker", "run", "-d", "--rm"]
        if mount_dir:
            cmd += ["-v", f"{os.path.abspath(mount_dir)}:{self.workspace}"]
        cmd += ["-w", self.workspace, self._image, "sleep", "infinity"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to start container: {stderr.decode()}")

        self._container_id = stdout.decode().strip()
        return self._container_id

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        if not self._container_id:
            raise RuntimeError("Container not started. Call setup() first.")

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", self._container_id, "bash", "-c", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecResult(
                returncode=proc.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            return ExecResult(returncode=-1, stdout="", stderr=f"Command timed out after {timeout}s")

    async def read_file(self, path: str) -> str:
        quoted_path = shlex.quote(path)
        result = await self.exec_cmd(f"cat -- {quoted_path}")
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        import base64
        encoded = base64.b64encode(content.encode()).decode()
        quoted_path = shlex.quote(path)
        await self.exec_cmd(
            f"base64 -d > {quoted_path} <<'__OPENCOLLAB_B64__'\n"
            f"{encoded}\n"
            "__OPENCOLLAB_B64__"
        )

    async def cleanup(self) -> None:
        if self._container_id:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", self._container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            self._container_id = None
