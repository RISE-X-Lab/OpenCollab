import asyncio
import json

from opencollab.adapters.env import Environment, ExecResult
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.harness import evaluator
from opencollab.harness.evaluator import (
    EvalResult,
    EvalTask,
    run_eval_task,
    save_results,
)


def run(coro):
    return asyncio.run(coro)


def is_worktree_diff_cmd(cmd: str) -> bool:
    return "git diff --cached --binary HEAD" in cmd


class FakeLLMClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


class FakeEnv(Environment):
    def __init__(self, diff="diff --git a/x b/x\n+new\n"):
        self.diff = diff
        self.cleaned_up = False
        self.cmds = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


def test_run_eval_task_produces_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(run_eval_task(
        EvalTask(task_id="t1", description="fix the bug"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert isinstance(result, EvalResult)
    assert result.task_id == "t1"
    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None
    assert env.cleaned_up is True


def test_run_eval_task_empty_diff_not_produced(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    async def env_factory(task):
        return FakeEnv(diff="")

    result = run(run_eval_task(
        EvalTask(task_id="t2", description="noop"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert result.patch_produced is False
    assert result.patch == ""


def test_run_eval_task_staged_extraction_includes_new_files(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv(
        diff=(
            "diff --git a/new_module.py b/new_module.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_module.py\n"
            "@@ -0,0 +1 @@\n"
            "+value = 1\n"
        )
    )

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="new-file", description="add file"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
    assert "new file mode" in result.patch
    assert result.patch_produced is True


def test_run_eval_task_honors_injected_params(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    captured = {}
    sentinel_tool = object()

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["prompt"] = agent.system_prompt
        captured["tools"] = list(agent.tools)
        captured["max_steps"] = max_steps
        captured["temperature"] = agent.temperature
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="t3", description="task"),
        output_dir=str(tmp_path),
        prompt="CUSTOM PROMPT",
        tools_factory=lambda: [sentinel_tool],
        env_factory=env_factory,
        max_steps=7,
        temperature=0.55,
    ))

    assert captured["prompt"] == "CUSTOM PROMPT"
    assert captured["tools"] == [sentinel_tool]
    assert captured["max_steps"] == 7
    assert captured["temperature"] == 0.55


class CapturingLLMClient:
    """Fake LLM client that records every kwarg passed to ``complete``.

    Accepts ``**kwargs`` so a forwarded ``top_p`` (or ``thinking`` etc.) does
    not raise — lets a test assert the sampling knob reaches the provider call.
    """

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        CapturingLLMClient.last_kwargs = {"temperature": temperature, **kwargs}
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


def test_run_eval_task_forwards_top_p_to_agent_and_provider(monkeypatch, tmp_path):
    # The eval path must put top_p on the Agent AND carry it into the provider
    # ``complete`` call, mirroring temperature. This is the latent eval-gap fix:
    # a configured top_p (like OPENCOLLAB_TOP_P) actually takes effect.
    CapturingLLMClient.last_kwargs = {}
    monkeypatch.setattr(container, "LLMClient", CapturingLLMClient)
    captured = {}

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["temperature"] = agent.temperature
        captured["top_p"] = agent.top_p
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="tp", description="task"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
        temperature=0.3,
        top_p=0.85,
    ))

    assert captured["temperature"] == 0.3
    assert captured["top_p"] == 0.85
    # And it actually reached the provider call (not just stored on the Agent).
    assert CapturingLLMClient.last_kwargs.get("top_p") == 0.85


def test_run_eval_task_top_p_unset_omits_it_from_provider_call(monkeypatch, tmp_path):
    # Default top_p (None) leaves the Agent default None and is NOT forwarded to
    # the provider call — so the request is byte-identical to today's behavior.
    CapturingLLMClient.last_kwargs = {}
    monkeypatch.setattr(container, "LLMClient", CapturingLLMClient)
    captured = {}

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["top_p"] = agent.top_p
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="tp0", description="task"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert captured["top_p"] is None
    assert "top_p" not in CapturingLLMClient.last_kwargs


def test_default_tools_match_curated_team_surface():
    # The headless eval agent must exercise the same curated toolset as team
    # roles — in particular run_tests/git_diff/apply_patch, which the bash
    # description deflects to. Guards against the two paths drifting apart.
    names = [t.name for t in evaluator.default_tools()]
    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "git_diff",
        "grep",
    ]


# --------------------------------------------------------------------------- #
# inject-f2p: extras passthrough, test injection, diff-exclusion
# --------------------------------------------------------------------------- #


def test_eval_task_round_trips_extras():
    # extras defaults to None and carries an arbitrary benchmark dict unchanged.
    assert EvalTask(task_id="t", description="d").extras is None
    extras = {"test_patch": "diff...", "fail_to_pass": ["pkg::test_a"]}
    task = EvalTask(task_id="t", description="d", extras=extras)
    assert task.extras == extras


class InjectFakeEnv(Environment):
    """Env that faithfully models git's per-path revert of injected test files.

    Models the real driver's contamination surface: the submitted patch is
    extracted with ``git add -A && git diff --cached`` (``staged_diff`` here), so
    any injected test edit still in the tree at extraction time LEAKS. Each
    injected path can be a tracked modification (revertible with
    ``git checkout --``) or a brand-new untracked file (which ``git checkout``
    canNOT remove — it errors rc=1 — and only ``git clean -fq`` deletes). A path
    is excluded from the extracted diff only once it has been BOTH checked out and
    cleaned per-path, matching the production exclusion. This exposes the new-file
    leak the old always-succeeds fake hid.
    """

    def __init__(self, src_path="src/app.py", mod_path=None, new_path=None):
        self.src_path = src_path
        self.mod_path = mod_path  # injected tracked-file modification (or None)
        self.new_path = new_path  # injected brand-new untracked file (or None)
        # A path is "present in the working tree" (and thus leaks into the staged
        # diff) until reverted. Tracked mods clear on checkout; new files clear
        # only on clean (checkout errors on them).
        self.checked_out: set[str] = set()
        self.cleaned: set[str] = set()
        self.cmds: list[str] = []
        self.cleaned_up = False

    async def write_file(self, path: str, content: str) -> None:
        pass

    def _leaks(self, path: str | None, *, untracked: bool) -> bool:
        if not path:
            return False
        if untracked:
            return path not in self.cleaned  # only `git clean` removes it
        return path not in self.checked_out  # `git checkout` reverts it

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith("git checkout -- "):
            path = cmd[len("git checkout -- "):].strip().strip("'\"")
            # git checkout errors (rc=1) on an untracked/new path and reverts
            # nothing; it restores a tracked modification.
            if path == self.new_path:
                return ExecResult(
                    returncode=1,
                    stdout="",
                    stderr=f"error: pathspec '{path}' did not match any file(s) known to git",
                )
            self.checked_out.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith("git clean -fq -- "):
            path = cmd[len("git clean -fq -- "):].strip().strip("'\"")
            self.cleaned.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            parts = [f"diff --git a/{self.src_path} b/{self.src_path}\n+fix\n"]
            if self._leaks(self.mod_path, untracked=False):
                parts.append(f"diff --git a/{self.mod_path} b/{self.mod_path}\n+assert thing\n")
            if self._leaks(self.new_path, untracked=True):
                parts.append(
                    f"diff --git a/{self.new_path} b/{self.new_path}\n"
                    f"new file mode 100644\n+brand new test\n"
                )
            return ExecResult(returncode=0, stdout="".join(parts), stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


def test_diff_exclusion_omits_injected_test_paths(tmp_path):
    # Injected test_patch that MODIFIES an existing tracked test file.
    env = InjectFakeEnv(mod_path="tests/test_app.py")

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
                        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                    ),
                    "fail_to_pass": ["tests/test_app.py::test_thing"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected test path was checked out before extraction.
    assert "tests/test_app.py" in env.checked_out
    checkout_cmds = [c for c in env.cmds if c.startswith("git checkout --")]
    assert checkout_cmds and "tests/test_app.py" in checkout_cmds[0]
    # The submitted patch contains the source edit but NOT the injected test.
    assert "src/app.py" in result.patch
    assert "tests/test_app.py" not in result.patch
    # The workflow saw fail_to_pass and the injected paths in its args dict.
    assert seen["args"]["fail_to_pass"] == ["tests/test_app.py::test_thing"]
    assert seen["args"]["injected_test_paths"] == ["tests/test_app.py"]


def test_diff_exclusion_omits_injected_new_test_file(tmp_path):
    # Regression for the new-file leak: SWE-bench test_patches commonly ADD a new
    # test file. `git checkout --` cannot remove an untracked file (errors rc=1);
    # the production exclusion must also `git clean -fq` it. The submitted patch is
    # extracted with `git add -A && git diff --cached`, so a surviving new file
    # would otherwise be staged and leak -> double-apply at grading time (D1).
    env = InjectFakeEnv(new_path="tests/test_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-new",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected new file was cleaned (checkout alone cannot remove it).
    assert "tests/test_new.py" in env.cleaned
    # The submitted patch contains the source edit but NOT the injected new test.
    assert "src/app.py" in result.patch
    assert "tests/test_new.py" not in result.patch


def test_diff_exclusion_one_new_file_does_not_strand_other_injected_edits(tmp_path):
    # A mixed test_patch (new file + modified existing file) must not let the new
    # file abort reverting the rest. The old single-command `git checkout -- p1 p2`
    # aborted entirely (rc=1) on the untracked path, stranding the tracked edit in
    # the submitted patch. Per-path revert keeps each independent.
    env = InjectFakeEnv(mod_path="tests/test_exist.py", new_path="tests/test_brand_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-mixed",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_exist.py b/tests/test_exist.py\n"
                        "--- a/tests/test_exist.py\n+++ b/tests/test_exist.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                        "diff --git a/tests/test_brand_new.py b/tests/test_brand_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_brand_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_brand_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # Both injected paths reverted: the tracked mod checked out, the new file cleaned.
    assert "tests/test_exist.py" in env.checked_out
    assert "tests/test_brand_new.py" in env.cleaned
    # Neither injected test leaks; only the source edit remains.
    assert "src/app.py" in result.patch
    assert "tests/test_exist.py" not in result.patch
    assert "tests/test_brand_new.py" not in result.patch


def test_workflow_args_drop_fail_to_pass_when_not_injected(tmp_path):
    # With no test_patch (nothing injected), fail_to_pass MUST NOT reach the
    # workflow: the F2P hard-gate keys on it, and the named tests do not exist at
    # the base commit, so forwarding the ids would make the gate unsatisfiable
    # rather than bypassed. Coupling fail_to_pass to injection success keeps the
    # gate's documented "no injection -> trust the verdict" invariant.
    env = FakeEnv()

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    run(
        run_eval_task(
            EvalTask(
                task_id="t-f2p",
                description="x",
                extras={"fail_to_pass": ["pkg::test_a", "pkg::test_b"]},
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # No test_patch -> nothing injected -> fail_to_pass dropped, gate bypassed.
    assert "fail_to_pass" not in seen["args"]
    assert "injected_test_paths" not in seen["args"]


def test_save_results_writes_patch_produced_key(tmp_path):
    results = [
        EvalResult(
            task_id="t1",
            patch="diff --git\n+x\n",
            patch_produced=True,
            tokens_used=5,
            steps=1,
            duration=0.123,
        ),
    ]
    out = tmp_path / "results.jsonl"
    save_results(results, str(out))

    record = json.loads(out.read_text().strip())
    assert record["patch_produced"] is True
    assert "success" not in record
    assert record["task_id"] == "t1"
    assert record["patch_lines"] == 2
