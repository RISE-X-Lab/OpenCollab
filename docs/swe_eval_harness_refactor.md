# SWE Eval Harness Refactor

这次重构把评测 harness 分成三层。第一层读取 prediction、metric、checkpoint 和 eval report 等事实记录。第二层用纯函数判定任务状态，例如 empty patch、metric 与 patch 不匹配、ready for eval、eval done。第三层才做副作用，例如启动评测命令或写状态文件。

恢复策略采用有限损失 checkpoint。`run_eval_task(..., checkpoint_interval_seconds=300)` 会在 workflow 运行期间周期捕获工作树 diff，并在退出前再捕获一次最终 diff。捕获文件位于每个任务的 workflow run 目录，例如 `trajectories/<task_id>/checkpoint.worktree.patch` 和 `checkpoint.worktree.json`。`checkpoint.worktree.json` 记录 `loss_bound_seconds`，默认值是 300 秒。

这个 checkpoint 保存的是文件系统里的工作树变化。恢复时，`resume_from_checkpoint=True` 会在新的环境建立后、注入 SWE-bench 测试 patch 前，把最近的 `checkpoint.worktree.patch` apply 回去。这样可以恢复上一次已经写入磁盘的代码改动。模型尚未写入文件的推理过程、正在进行的工具调用和外部进程瞬时状态会重新执行。

恢复前会先检查新环境的工作树是否已经有改动；若已有 diff，恢复会跳过并记录 `skipped_dirty_worktree`。如果最近一次捕获失败或捕获到空 diff，旧的非空 `checkpoint.worktree.patch` 会保留在磁盘上，但元数据会写 `submission_eligible=false` 和 `preserved_previous_patch=true`，避免旧 patch 被误当成当前成功结果。

SWE-bench 的官方测试 patch 会在运行开始时注入环境，但 checkpoint 捕获时会把这些注入路径从临时 index 里移除。最终提交 patch 和恢复 patch 都只围绕模型产生的代码改动，避免把 grader 测试混入提交。

`scripts/swe_auto_eval_driver.py` 和 `scripts/swe_v3_wave_watchdog.py` 都是薄入口。默认行为只读，输出 JSON/Markdown 状态。自动评测启动需要显式传 `--start-eval` 和 `--eval-command-template`，这样状态判断和副作用有清楚边界。
