from __future__ import annotations

from evaluator_test_support import (
    CapturingLLMClient,
    EvalTask,
    FakeEnv,
    FakeLLMClient,
    container,
    evaluator,
    is_worktree_diff_cmd,
    pytest,
    run,
    run_eval_task,
)


@pytest.mark.parametrize(
    "cleanup_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_cleanup_timeout_without_side_effects(tmp_path, cleanup_timeout):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="cancellation_cleanup_timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-cleanup-timeout", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=cleanup_timeout,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "task_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_task_timeout_without_side_effects(tmp_path, task_timeout):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="task timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(
                    task_id="invalid-task-timeout",
                    description="fix",
                    timeout=task_timeout,
                ),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "checkpoint_interval",
    [float("nan"), float("inf"), float("-inf"), -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_checkpoint_interval_without_side_effects(tmp_path, checkpoint_interval):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="checkpoint_interval_seconds must be finite and non-negative",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-checkpoint-interval", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                checkpoint_interval_seconds=checkpoint_interval,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


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

    run(
        run_eval_task(
            EvalTask(task_id="t3", description="task"),
            output_dir=str(tmp_path),
            prompt="CUSTOM PROMPT",
            tools_factory=lambda: [sentinel_tool],
            env_factory=env_factory,
            max_steps=7,
            temperature=0.55,
        )
    )

    assert captured["prompt"] == "CUSTOM PROMPT"
    assert captured["tools"] == [sentinel_tool]
    assert captured["max_steps"] == 7
    assert captured["temperature"] == 0.55


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

    run(
        run_eval_task(
            EvalTask(task_id="tp", description="task"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            temperature=0.3,
            top_p=0.85,
        )
    )

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

    run(
        run_eval_task(
            EvalTask(task_id="tp0", description="task"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

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
    by_name = {tool.name: tool for tool in evaluator.default_tools()}
    assert by_name["bash"].require_process_isolation is True
    assert by_name["run_tests"].require_process_isolation is True
    assert by_name["run_tests"].allow_runner_override is False
    assert by_name["run_tests"].allow_extra_args is False


def test_eval_task_round_trips_extras():
    # extras defaults to None and carries an arbitrary benchmark dict unchanged.
    assert EvalTask(task_id="t", description="d").extras is None
    extras = {"test_patch": "diff...", "fail_to_pass": ["pkg::test_a"]}
    task = EvalTask(task_id="t", description="d", extras=extras)
    assert task.extras == extras
