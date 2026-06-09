"""
SWE-bench Docker evaluator using OpenCollab agent framework.

Runs inside swe-collab container (Python 3.11). Controls Epoch AI pre-built
ARM64 containers via Docker socket for agent execution + evaluation.

Architecture:
  swe-collab container (this script, Python 3.11)
    | docker exec
    v
  epoch ARM64 container (correct Python + deps + repo at /testbed)

Evaluation uses the official swebench eval_script + grading when available,
falling back to test_cmd execution otherwise.

Usage:
  scripts/run_swe_docker.sh --instance_ids django__django-15400
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib
import json
import os
import platform
import shlex
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Allow config loading and package imports relative to this script.
_repo_root = Path(__file__).resolve().parents[1]

# Allow running without editable install: repo_root/opencollab is the package root.
_pkg_root = _repo_root / "opencollab"
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

# swebench grading — each import isolated to avoid false HAS_SWEBENCH=False
HAS_SWEBENCH_SPEC = False
HAS_SWEBENCH_GRADE = False
make_test_spec = None
get_eval_report = None
try:
    from swebench.harness.test_spec.test_spec import make_test_spec  # type: ignore[import]

    HAS_SWEBENCH_SPEC = True
except ImportError:
    pass
try:
    from swebench.harness.grading import get_eval_report  # type: ignore[import]

    HAS_SWEBENCH_GRADE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

DOCKER_WORKDIR = "/testbed"


def _detect_arch() -> str:
    """Detect architecture from Docker daemon, not from current process."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Architecture}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker info failed: {result.stderr.strip()}")
        arch = result.stdout.strip()
        if not arch:
            raise RuntimeError("docker info returned empty architecture")
        if "arm" in arch or "aarch" in arch:
            return "arm64"
        return "x86_64"
    except Exception as e:
        fallback = "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
        print(f"  WARNING: arch detection failed ({e}), falling back to {fallback}")
        return fallback


_cfg = {"arch": _detect_arch()}


def get_image_name(instance_id: str) -> str:
    return f"ghcr.io/epoch-research/swe-bench.eval.{_cfg['arch']}.{instance_id}:latest"


def _image_cache() -> set[str]:
    """Cache available images. Raises RuntimeError if Docker daemon is unreachable."""
    if not hasattr(_image_cache, "_set"):
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker daemon error: {result.stderr.strip()}")
        _image_cache._set = set(result.stdout.strip().split("\n")) - {""}  # type: ignore[attr-defined]
    return _image_cache._set  # type: ignore[attr-defined]


def image_exists(instance_id: str) -> bool:
    """Check if image exists locally, try pulling if not."""
    image = get_image_name(instance_id)
    if image in _image_cache():
        return True
    print(f"  Pulling {image}...")
    result = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode == 0:
        _image_cache().add(image)
        return True
    return False


def start_container(instance_id: str, run_id: str) -> str:
    """Start epoch container. Returns container_id."""
    image = get_image_name(instance_id)
    name = f"swe-{run_id}-{instance_id}"[:63]
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name, image, "tail", "-f", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start: {result.stderr}")
    return result.stdout.strip()[:12]


def stop_container(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def exec_in(cid: str, cmd: str, timeout: int = 120) -> tuple[int, str]:
    """Execute command in container. Returns (returncode, output)."""
    try:
        result = subprocess.run(
            ["docker", "exec", "-w", DOCKER_WORKDIR, cid, "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 124, f"Command timed out after {timeout}s: {cmd[:80]}"


def safe_test_names(names: list[str]) -> str:
    """Shell-escape test identifiers to prevent injection."""
    return " ".join(shlex.quote(n) for n in names)


def container_has_module(cid: str, module_name: str) -> bool:
    """Check whether a Python module is importable inside the target container."""
    cmd = (
        "python - <<'PY'\n"
        "import importlib.util\n"
        f"print('YES' if importlib.util.find_spec({module_name!r}) else 'NO')\n"
        "PY"
    )
    rc, out = exec_in(cid, cmd, timeout=15)
    return rc == 0 and "YES" in out


def _is_output_path_persisted(output_path: str) -> bool:
    """Only allow outputs under mounted logs/ to avoid container-local data loss."""
    p = Path(os.path.normpath(str(output_path)))
    if p.is_absolute():
        p = p.resolve()
        logs_abs = Path("/app/logs").resolve()
        return p == logs_abs or logs_abs in p.parents
    parts = p.parts
    return bool(parts) and parts[0] == "logs"


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append one JSON record and fsync so progress survives crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# OpenCollab environment adapter (existing docker container)
# ---------------------------------------------------------------------------


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


class ExistingContainerEnvironment:
    """Environment adapter that targets an already-running container id."""

    def __init__(self, container_id: str, workspace: str = DOCKER_WORKDIR):
        self._container_id = container_id
        self.workspace = workspace

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-w",
                self.workspace,
                self._container_id,
                "bash",
                "-c",
                cmd,
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
        quoted = shlex.quote(path)
        result = await self.exec_cmd(f"cat -- {quoted}")
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr)
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        import base64

        encoded = base64.b64encode(content.encode()).decode()
        quoted = shlex.quote(path)
        await self.exec_cmd(
            f"base64 -d > {quoted} <<'__OPENCOLLAB_B64__'\n"
            f"{encoded}\n"
            "__OPENCOLLAB_B64__"
        )


@dataclass
class AgentRunConfig:
    model: str
    provider: str
    api_key: Optional[str]
    base_url: Optional[str]
    budget: int


EVAL_AGENT_PROMPT = """\
You are an autonomous coding agent running inside a SWE-bench repository.

Rules:
- Read relevant files before making changes.
- Make minimal, targeted changes to fix the issue.
- Verify with the provided test command when possible.
- Do NOT commit.
"""

TEAM_LEAD_PROMPT = """\
You are the lead agent for SWE-bench bug fixing.

Collaboration policy:
- First delegate to analyst for a short plan.
- Delegate implementation to coder with concrete file-level steps.
- Use delegate_with_review for risky or non-trivial code changes.
- Ask coder to run the provided verification command before finishing.
- Return a concise summary of what changed and why tests should pass.

Constraints:
- Work only inside the repository workspace.
- Make minimal, targeted edits.
- Do NOT commit.
"""


async def run_opencollab_team_agent(
    task: str,
    container_id: str,
    cfg: AgentRunConfig,
    max_round: int,
    max_steps: int,
    run_id: str = "",
) -> tuple[str, int, int]:
    """Run OpenCollab Team against an existing docker container workspace."""
    team_mod = importlib.import_module("opencollab.team.orchestrator")
    prompts_mod = importlib.import_module("opencollab.team.prompts")
    safety_mod = importlib.import_module("opencollab.tools.safety")
    bash_mod = importlib.import_module("opencollab.tools.bash")
    fs_mod = importlib.import_module("opencollab.tools.fs")
    agent_mod = importlib.import_module("opencollab.core.agent")
    session_mod = importlib.import_module("opencollab.core.session")

    Team = team_mod.Team
    get_role_prompt = prompts_mod.get_role_prompt
    SandboxInterceptor = safety_mod.SandboxInterceptor
    Agent = agent_mod.Agent
    Session = session_mod.Session
    BashTool = bash_mod.BashTool
    FileReadTool = fs_mod.FileReadTool
    FileWriteTool = fs_mod.FileWriteTool
    GrepTool = fs_mod.GrepTool

    tracer_mod = importlib.import_module("opencollab.core.tracer")
    Tracer = tracer_mod.Tracer
    safe_tracer_id = cfg.model.replace("/", "_").replace(":", "_")
    traj_run_id = f"team_{safe_tracer_id}_{run_id}" if run_id else f"team_{safe_tracer_id}"
    tracer = Tracer(run_id=traj_run_id, output_dir=str(Path("logs") / "trajectories"))

    lead_env = ExistingContainerEnvironment(container_id=container_id, workspace=DOCKER_WORKDIR)
    lead_max_steps = max(1, max_round) * max(1, max_steps)

    team = Team(
        workspace=DOCKER_WORKDIR,
        model=cfg.model,
        provider=cfg.provider,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        lead_prompt=TEAM_LEAD_PROMPT,
        max_budget_tokens=cfg.budget,
        use_worktrees=False,
        tracer=tracer,
        lead_env=lead_env,
        lead_max_steps=lead_max_steps,
    )

    team._teammate_steps = 0

    async def _delegate_in_container(self, role: str, subtask: str, context: str = "") -> str:
        teammate_env = ExistingContainerEnvironment(container_id=container_id, workspace=DOCKER_WORKDIR)
        teammate_interceptor = SandboxInterceptor(DOCKER_WORKDIR)

        teammate_agent = Agent(
            name=role,
            system_prompt=get_role_prompt(role),
            tools=[
                BashTool(teammate_interceptor),
                FileReadTool(teammate_interceptor),
                FileWriteTool(teammate_interceptor),
                GrepTool(teammate_interceptor),
            ],
            model=self.model,
            provider=self.provider,
            api_key=self.api_key,
            base_url=self.base_url,
        )

        remaining_budget = max(10_000, self._total_budget - self._used_tokens)
        reserve_for_lead = min(max(10_000, self._total_budget // 4), max(0, remaining_budget - 10_000))
        teammate_budget = max(10_000, remaining_budget - reserve_for_lead)

        teammate_session = Session(
            agent=teammate_agent,
            env=teammate_env,
            tracer=self.tracer,
            max_budget_tokens=teammate_budget,
            max_steps=max(20, max_steps),
            on_event=self.on_event,
            confirm_fn=self.confirm_fn,
        )

        task_message = subtask if not context else f"Context:\n{context}\n\nTask:\n{subtask}"
        await teammate_session.add_user_message(task_message)
        result = await teammate_session.run_loop()

        self._used_tokens += teammate_session.used_tokens
        self._teammate_steps += teammate_session.step_count
        return result

    # Rebind delegation path so Team tools execute inside the target container.
    team.delegate = types.MethodType(_delegate_in_container, team)

    result = await team.run(task)
    used_tokens = team.lead_session.used_tokens + team._used_tokens
    steps = team.lead_session.step_count + team._teammate_steps
    return result, used_tokens, steps


async def run_opencollab_single_agent(
    task: str,
    container_id: str,
    cfg: AgentRunConfig,
    max_round: int,
    max_steps: int,
) -> tuple[str, int, int]:
    """Run OpenCollab single-agent Session inside the existing docker container."""
    agent_mod = importlib.import_module("opencollab.core.agent")
    session_mod = importlib.import_module("opencollab.core.session")
    bash_mod = importlib.import_module("opencollab.tools.bash")
    fs_mod = importlib.import_module("opencollab.tools.fs")

    Agent = agent_mod.Agent
    Session = session_mod.Session
    BashTool = bash_mod.BashTool
    FileReadTool = fs_mod.FileReadTool
    FileWriteTool = fs_mod.FileWriteTool
    GrepTool = fs_mod.GrepTool

    env = ExistingContainerEnvironment(container_id=container_id, workspace=DOCKER_WORKDIR)
    agent = Agent(
        name="swe_eval_agent",
        system_prompt=EVAL_AGENT_PROMPT,
        tools=[
            BashTool(),
            FileReadTool(),
            FileWriteTool(),
            GrepTool(),
        ],
        model=cfg.model,
        provider=cfg.provider,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
    )

    session = Session(
        agent=agent,
        env=env,
        max_budget_tokens=cfg.budget,
        max_steps=max(1, max_round) * max(1, max_steps),
    )

    await session.add_user_message(task)
    result = await session.run_loop()
    return result, session.used_tokens, session.step_count


# ---------------------------------------------------------------------------
# Test command builder — uses eval_script when available
# ---------------------------------------------------------------------------


def build_test_cmd(instance: Dict[str, Any], cid: str) -> Tuple[Optional[str], Optional[str], Optional[object]]:
    """Build test command. Prefers swebench eval_script, falls back to heuristic.

    Returns (test_cmd, eval_script, test_spec_or_None).
    """
    fail_to_pass = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(fail_to_pass, str):
        fail_to_pass = json.loads(fail_to_pass)

    eval_script = instance.get("eval_script") if isinstance(instance.get("eval_script"), str) else None
    test_spec = None
    if HAS_SWEBENCH_SPEC and make_test_spec is not None:
        try:
            test_spec = make_test_spec(instance)
            eval_script = test_spec.eval_script
        except Exception:
            pass

    if not fail_to_pass:
        return None, eval_script, test_spec

    if eval_script:
        for line in eval_script.split("\n"):
            stripped = line.strip()
            if (
                stripped
                and not stripped.startswith("#")
                and not stripped.startswith("set")
                and not stripped.startswith("cd")
                and not stripped.startswith("git")
                and not stripped.startswith("source")
                and not stripped.startswith("conda")
                and not stripped.startswith("export")
            ):
                if "pytest" in stripped or "runtests" in stripped or "bin/test" in stripped:
                    return stripped, eval_script, test_spec

    escaped = safe_test_names(fail_to_pass)
    rc, _ = exec_in(cid, "test -f tests/runtests.py", timeout=5)
    if rc == 0:
        return (
            "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
            f"{escaped}",
            eval_script,
            test_spec,
        )
    return f"python -m pytest --no-header -rA --tb=short {escaped}", eval_script, test_spec


# ---------------------------------------------------------------------------
# Evaluation — official grading when available
# ---------------------------------------------------------------------------


def evaluate_patch(
    instance: Dict[str, Any],
    cid: str,
    patch: str,
    eval_script: Optional[str],
    test_spec: Optional[object],
    log_dir: Path,
    safe_model_name: str,
    timeout: int,
) -> Optional[bool]:
    """Evaluate using official swebench grading if available, else return None for fallback."""
    instance_id = instance["instance_id"]

    if eval_script and test_spec and HAS_SWEBENCH_GRADE and get_eval_report is not None:
        try:
            eval_path = log_dir / "eval.sh"
            eval_path.write_text(eval_script)
            cp_result = subprocess.run(
                ["docker", "cp", str(eval_path), f"{cid}:/eval.sh"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if cp_result.returncode != 0:
                raise RuntimeError(f"docker cp failed: {cp_result.stderr}")
            rc, test_output = exec_in(cid, "/bin/bash /eval.sh", timeout=timeout)
            if not test_output.strip():
                raise RuntimeError("eval script produced no output")
            _ = rc
            test_output_path = log_dir / "test_output.txt"
            test_output_path.write_text(test_output)

            pred = {
                "instance_id": instance_id,
                "model_name_or_path": safe_model_name,
                "model_patch": patch,
            }
            report = get_eval_report(
                test_spec=test_spec,
                prediction=pred,
                test_log_path=test_output_path,
                include_tests_status=True,
            )
            (log_dir / "report.json").write_text(json.dumps(report, indent=2))

            return report.get(instance_id, {}).get("resolved", False)
        except RuntimeError as e:
            print(f"    Eval environment error (not a test failure): {e}")
        except Exception as e:
            print(f"    Grading error, falling back to rc check: {e}")

    return None


def run_eval_script_rc_fallback(
    cid: str,
    eval_script: str,
    log_dir: Path,
    timeout: int,
) -> tuple[bool, str]:
    """Run eval_script directly and use exit code as fallback signal."""
    eval_path = log_dir / "eval_fallback.sh"
    eval_path.write_text(eval_script)
    cp_result = subprocess.run(
        ["docker", "cp", str(eval_path), f"{cid}:/eval_fallback.sh"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if cp_result.returncode != 0:
        raise RuntimeError(f"docker cp failed: {cp_result.stderr}")

    rc, test_output = exec_in(cid, "/bin/bash /eval_fallback.sh", timeout=timeout)
    (log_dir / "test_output.txt").write_text(test_output)
    return rc == 0, test_output


# ---------------------------------------------------------------------------
# Per-instance runner
# ---------------------------------------------------------------------------


def run_instance_docker(
    instance: Dict[str, Any],
    cfg: AgentRunConfig,
    run_id: str,
    safe_model_name: str,
    agent_mode: str = "team",
    max_round: int = 2,
    max_steps: int = 12,
    timeout: int = 300,
    verbose: bool = True,
) -> tuple[str, bool, str, str]:
    """Run OpenCollab agent inside epoch Docker container, then evaluate."""
    instance_id = instance["instance_id"]

    if not image_exists(instance_id):
        if verbose:
            print(f"  {instance_id}: SKIP (image not found)")
        return instance_id, False, "", "skipped"

    log_dir = Path("logs") / run_id / safe_model_name / instance_id
    log_dir.mkdir(parents=True, exist_ok=True)

    cid = None
    try:
        if verbose:
            print("  Starting container...")
        cid = start_container(instance_id, run_id)
        _, py_ver = exec_in(cid, "python --version", timeout=5)
        if verbose:
            print(f"  Container {cid} | {py_ver.strip()}")

        _, structure = exec_in(cid, "find . -maxdepth 3 -type f -name '*.py' | head -80", timeout=10)

        hints = instance.get("hints_text", "")
        hints_section = f"\n\n## Hints\n{hints}" if hints else ""
        fail_to_pass = instance.get("FAIL_TO_PASS", "[]")
        if isinstance(fail_to_pass, str):
            fail_to_pass = json.loads(fail_to_pass)
        test_section = ""
        if fail_to_pass:
            test_section = "\n\n## Tests That Should Pass After Fix\n" + "\n".join(
                f"- {t}" for t in fail_to_pass
            )

        test_cmd, eval_script, test_spec = build_test_cmd(instance, cid)
        if verbose and test_cmd:
            print(f"  Test: {test_cmd[:100]}")
        if verbose and eval_script:
            print(f"  Official eval_script available ({len(eval_script)} chars)")

        test_guidance = f"\n\n## Suggested Verification Command\n{test_cmd}" if test_cmd else ""
        task = f"""## Repository Structure
```
{structure[:3000]}
```

## Issue
{instance['problem_statement']}
{hints_section}{test_section}{test_guidance}
"""

        # Persist and optionally print the exact agent input for inspection.
        (log_dir / "agent_input.txt").write_text(task)
        if verbose:
            print("  Agent input (task prompt):")
            print(task)

        if agent_mode == "single":
            if verbose:
                print("  Running OpenCollab single-agent session...")
            result_text, used_tokens, steps = asyncio.run(
                run_opencollab_single_agent(
                    task=task,
                    container_id=cid,
                    cfg=cfg,
                    max_round=max_round,
                    max_steps=max_steps,
                )
            )
        else:
            if verbose:
                print("  Running OpenCollab team session...")
            result_text, used_tokens, steps = asyncio.run(
                run_opencollab_team_agent(
                    task=task,
                    container_id=cid,
                    cfg=cfg,
                    max_round=max_round,
                    max_steps=max_steps,
                    run_id=run_id,
                )
            )
        (log_dir / "agent_result.txt").write_text(result_text)
        (log_dir / "agent_stats.json").write_text(
            json.dumps({"used_tokens": used_tokens, "steps": steps, "agent_mode": agent_mode}, indent=2)
        )

        _, patch = exec_in(cid, "git diff", timeout=10)

        if not patch.strip():
            # Persist a compact diagnostic snapshot for no-patch outcomes.
            try:
                _, git_status = exec_in(cid, "git status --short", timeout=10)
            except Exception as status_err:
                git_status = f"<failed to capture git status: {type(status_err).__name__}: {status_err}>"
            result_excerpt = result_text[-1200:] if result_text else ""
            reason = (
                "No patch produced by agent.\n"
                f"instance_id: {instance_id}\n"
                f"used_tokens: {used_tokens}\n"
                f"steps: {steps}\n\n"
                "git status --short:\n"
                f"{git_status.strip() if git_status else '<empty>'}\n\n"
                "agent_result_tail:\n"
                f"{result_excerpt.strip() if result_excerpt else '<empty>'}\n"
            )
            (log_dir / "no_patch_reason.txt").write_text(reason)
            (log_dir / "grading_method.txt").write_text(
                "no_patch: agent finished without workspace changes (git diff empty)"
            )
            if verbose:
                print(f"  {instance_id}: no patch")
            return instance_id, False, "", "no_patch"

        (log_dir / "patch.diff").write_text(patch)
        if verbose:
            print(f"  Patch: {len(patch)} chars")

        if verbose:
            print("  Evaluating...")
        resolved = evaluate_patch(instance, cid, patch, eval_script, test_spec, log_dir, safe_model_name, timeout)

        grading_method = "official"
        if resolved is None and eval_script:
            try:
                resolved, _ = run_eval_script_rc_fallback(cid, eval_script, log_dir, timeout)
                grading_method = "eval_script_rc"
                (log_dir / "grading_method.txt").write_text(
                    "eval_script_rc: ran official eval_script, graded by return code only"
                )
            except Exception as e:
                if verbose:
                    print(f"    eval_script rc fallback failed: {e}")
                resolved = None

        if resolved is None and test_cmd:
            if "pytest" in test_cmd and not container_has_module(cid, "pytest"):
                test_output = "pytest not available in target container; skipped pytest fallback command."
                (log_dir / "test_output.txt").write_text(test_output)
                resolved = False
                grading_method = "no_eval_missing_pytest"
                (log_dir / "grading_method.txt").write_text(
                    "no_eval_missing_pytest: pytest fallback unavailable in container"
                )
            else:
                rc, test_output = exec_in(cid, test_cmd, timeout=timeout)
                (log_dir / "test_output.txt").write_text(test_output)
                resolved = rc == 0
                grading_method = "rc_fallback"
                (log_dir / "grading_method.txt").write_text("rc_fallback: non-comparable with official SWE-bench")
        elif resolved is None:
            grading_method = "no_eval"

        resolved = bool(resolved) if resolved is not None else False
        if verbose:
            tag = "" if grading_method == "official" else f" [{grading_method}]"
            print(f"  {instance_id}: {'RESOLVED' if resolved else 'FAILED'}{tag}")
        return instance_id, resolved, patch, grading_method

    except Exception as e:
        if verbose:
            print(f"  {instance_id}: ERROR - {e}")
        return instance_id, False, "", "error"
    finally:
        if cid:
            stop_container(cid)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench: OpenCollab agent in Docker")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument(
        "--output_path",
        type=str,
        default="logs/swe_predictions.jsonl",
        help="Output predictions file (use logs/ prefix to ensure it's mounted)",
    )
    parser.add_argument(
        "--progress_path",
        type=str,
        default=None,
        help="Optional JSONL progress log path. Defaults to <output_path>.progress.jsonl",
    )
    parser.add_argument("--max_round", type=int, default=2)
    parser.add_argument("--max_steps", type=int, default=12)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--provider", type=str, default=None)
    parser.add_argument(
        "--agent_mode",
        type=str,
        choices=["single", "team"],
        default="team",
        help="Execution mode: single agent or lead+teammates team (default: team)",
    )
    parser.add_argument("--instance_ids", type=str, nargs="+", default=None)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--arch",
        type=str,
        default=None,
        help="Image architecture override (default: auto-detect)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        import tqdm  # type: ignore[import-not-found]
        from datasets import load_dataset  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Missing dependency for SWE runner: install 'datasets' and 'tqdm' in this environment."
        ) from e

    try:
        config_mod = importlib.import_module("opencollab.core.config")
        get_config = config_mod.get_config
    except Exception as e:
        raise RuntimeError(
            "Failed to import OpenCollab framework. Use Python 3.11+ and install opencollab package first."
        ) from e

    if not _is_output_path_persisted(args.output_path):
        raise ValueError(f"output_path must be under logs/ (mounted to host). Got: {args.output_path}")
    if args.progress_path and not _is_output_path_persisted(args.progress_path):
        raise ValueError(f"progress_path must be under logs/ (mounted to host). Got: {args.progress_path}")

    if args.arch:
        _cfg["arch"] = args.arch

    env_cfg = get_config(str(_repo_root))
    model = args.model or env_cfg["model"] or "gpt-4o"
    provider = args.provider or env_cfg["provider"] or "openai"
    cfg = AgentRunConfig(
        model=model,
        provider=provider,
        api_key=env_cfg["api_key"],
        base_url=env_cfg["base_url"],
        budget=int(env_cfg["budget"] or "200000"),
    )

    safe_model_name = cfg.model.replace("/", "__").replace(":", "_")

    print("Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split=args.split)

    if args.instance_ids:
        id_set = set(args.instance_ids)
        instances = [inst for inst in dataset if inst["instance_id"] in id_set]
    else:
        instances = list(dataset)

    print(
        f"Instances: {len(instances)} | Model: {cfg.model} | Provider: {cfg.provider} "
        f"| AgentMode: {args.agent_mode} | Arch: {_cfg['arch']}"
    )
    if HAS_SWEBENCH_GRADE and HAS_SWEBENCH_SPEC:
        print("  swebench grading: available")
    elif HAS_SWEBENCH_SPEC:
        print("  swebench grading: partial (spec only, no grading)")
    else:
        print("  swebench grading: not available (fallback to rc check)")

    available = [inst for inst in instances if image_exists(inst["instance_id"])]
    missing = [inst["instance_id"] for inst in instances if not image_exists(inst["instance_id"])]
    if missing:
        print(f"  Missing images: {', '.join(missing[:5])}")
    print(f"  Ready: {len(available)}/{len(instances)}")

    if not available:
        print("No instances to run.")
        return

    run_id = datetime.datetime.now().strftime("collab_%Y%m%d_%H%M%S")
    progress_path = args.progress_path or f"{args.output_path}.progress.jsonl"
    print(f"\n=== Running {len(available)} instances ===")
    print(f"Progress log: {progress_path}")
    append_jsonl(
        Path(progress_path),
        {
            "event": "run_start",
            "run_id": run_id,
            "agent_mode": args.agent_mode,
            "model_name_or_path": safe_model_name,
            "split": args.split,
            "total_instances": len(available),
            "ts": datetime.datetime.now().isoformat(),
        },
    )

    results: list[tuple[str, bool, str]] = []
    Path(args.output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(progress_path).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_path, "w", encoding="utf-8") as f:
        for instance in tqdm.tqdm(available, desc="Instances"):
            iid = instance["instance_id"]
            append_jsonl(
                Path(progress_path),
                {
                    "event": "start",
                    "run_id": run_id,
                    "instance_id": iid,
                    "agent_mode": args.agent_mode,
                    "model_name_or_path": safe_model_name,
                    "ts": datetime.datetime.now().isoformat(),
                },
            )

            try:
                iid, resolved, patch, grading = run_instance_docker(
                    instance,
                    cfg,
                    run_id,
                    safe_model_name,
                    agent_mode=args.agent_mode,
                    max_round=args.max_round,
                    max_steps=args.max_steps,
                    timeout=args.timeout,
                    verbose=not args.quiet,
                )
            except Exception as e:
                # Per-item fail-safe: record and continue batch.
                iid, resolved, patch, grading = iid, False, "", "error"
                append_jsonl(
                    Path(progress_path),
                    {
                        "event": "fatal",
                        "run_id": run_id,
                        "instance_id": iid,
                        "agent_mode": args.agent_mode,
                        "model_name_or_path": safe_model_name,
                        "resolved": False,
                        "has_patch": False,
                        "grading_method": "error",
                        "error": f"{type(e).__name__}: {e}",
                        "ts": datetime.datetime.now().isoformat(),
                    },
                )

            results.append((iid, resolved, grading))
            append_jsonl(
                Path(progress_path),
                {
                    "event": "finish",
                    "run_id": run_id,
                    "instance_id": iid,
                    "agent_mode": args.agent_mode,
                    "model_name_or_path": safe_model_name,
                    "resolved": resolved,
                    "has_patch": bool(patch.strip()),
                    "grading_method": grading,
                    "ts": datetime.datetime.now().isoformat(),
                },
            )
            if patch.strip():
                f.write(
                    json.dumps(
                        {
                            "instance_id": iid,
                            "model_name_or_path": safe_model_name,
                            "model_patch": patch,
                            "grading_method": grading,
                        }
                    )
                    + "\n"
                )
                f.flush()

    print("\n=== Results ===")
    resolved_count = sum(1 for _, r, _ in results if r)
    for iid, resolved, grading in results:
        tag = "" if grading == "official" else f" [{grading}]"
        print(f"  {iid}: {'RESOLVED' if resolved else 'FAILED'}{tag}")
    print(f"\nResolved: {resolved_count}/{len(results)}")
    append_jsonl(
        Path(progress_path),
        {
            "event": "run_end",
            "run_id": run_id,
            "agent_mode": args.agent_mode,
            "model_name_or_path": safe_model_name,
            "resolved": resolved_count,
            "total": len(results),
            "ts": datetime.datetime.now().isoformat(),
        },
    )


if __name__ == "__main__":
    main()
