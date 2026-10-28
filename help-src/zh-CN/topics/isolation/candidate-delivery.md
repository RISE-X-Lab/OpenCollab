---
{
  "id": "candidate-delivery",
  "title": "隔离改动怎样回到项目：采纳、找回与清理",
  "category": "isolation",
  "summary": "隔离环境里的改动不会自动回到项目：怎样挑一个候选采纳到交付目录；任务被取消、补丁不完整或清理失败时，怎样先找回完整改动再清理资源。",
  "aliases": [
    "改动去哪了",
    "结果没有写回",
    "candidate_agent",
    "candidate_workflow",
    "CandidateRun",
    "adopt_candidate",
    "adopt_agent",
    "entry_patch",
    "文件找不到",
    "容器残留",
    "worktree retained",
    "recovery_location",
    "retain_changes",
    "CandidateCaptureError",
    "CandidateWorkspaceTrackingError",
    "VM 租约保留",
    "pending_operations",
    "recover_operation",
    "VMTransportError"
  ],
  "keywords": [
    "候选结果",
    "采纳改动",
    "补丁交付",
    "写回项目",
    "恢复工作区",
    "清理失败",
    "补丁保留",
    "找回改动"
  ],
  "questions": [
    "找到 Agent 的实际改动，选择要保留的候选，并在交付目录验证结果？",
    "Agent 改完了，为什么我的项目里没变化？",
    "怎么从几个候选里挑一个合并进来？",
    "Team 里 Coder 的改动怎么写回？",
    "在取消、输出截断或清理失败后找到完整改动，并继续处理属于该运行的资源？",
    "任务中途停了，改的代码还在吗？",
    "补丁显示被截断了，完整的去哪找？",
    "容器或工作树残留了怎么清理？"
  ],
  "related": [
    "isolation-overview",
    "dispatch-team-agents",
    "workflow-composition-hierarchy",
    "vm-environment-extension",
    "timeouts-retries"
  ],
  "verified_ref": "v0.9.6"
}
---

# 隔离改动怎样回到项目：采纳、找回与清理

Agent 在工作树、容器或 VM 里改的代码，**不会自动写回你的项目**。它们先作为“候选”保存，由你的代码或入口成员显式采纳之后，才进入交付目录。这篇讲清楚这条交付路径：正常情况下怎样采纳，出了问题（任务被取消、补丁不完整、清理失败）怎样先保住改动、再清理资源。

## 先理解：为什么要多一步采纳

- **候选（candidate）**：一次在独立工作区里完成的实现，带着它的改动。
- **采纳（adopt）**：把候选的文件改动应用到交付目录。
- **交付目录**：最终要拿到改动的目录，通常就是你的项目。

多一步采纳，换来的是：几个 Agent 可以各改各的、互不干扰，你比较之后只采纳一个；某个候选做坏了，也不会弄乱项目。

三条交付路径：

| 谁在交付 | 怎么采纳 | 详见 |
| --- | --- | --- |
| Workflow | `ctx.candidate_agent` 或 `ctx.candidate_workflow` 拿到候选，再 `ctx.adopt_candidate(candidate)` | [候选：在隔离环境里试改动](../workflow/workflow-composition-hierarchy.md#候选在隔离环境里试改动) |
| Team 入口成员 | 用 `adopt_agent` 工具传入已完成 Coder 的 aid | [Team 协作：采纳改动](../team/dispatch-team-agents.md#第四步采纳改动再验证) |
| 入口成员自己被隔离 | 从 `result.metrics["entry_patch"]` 取补丁，由你应用 | 见下文 |

无论哪条路径，采纳之后都要在交付目录里运行项目验证，检查最终文件是否满足任务要求。

### Workflow 里的最短写法

```python
from opencollab import workflow
from opencollab.tools import builtin_tools

@workflow(name="adopt-when-clean")
async def adopt_when_clean(ctx, inputs):
    candidate = await ctx.candidate_agent(
        inputs["task"], label="implementation",
        tools=builtin_tools("file_read", "apply_patch", "grep"),
    )
    # 出错或没有输出就不采纳
    if candidate.lifecycle_errors or candidate.output is None:
        return {"adopted": False}
    await ctx.adopt_candidate(candidate)
    return {"adopted": True, "diff": candidate.diff}
```

想采纳时保留某些文件不被覆盖，传 `preserve_paths=("notes/local.txt",)`。有多个候选时，先比较完完整差异、测试记录和生命周期信息，再按选定的顺序采纳。字段含义见 [读取候选结果](../workflow/workflow-composition-hierarchy.md#读取候选结果)。

### 入口成员自己被隔离时

入口 Agent 自己也可以跑在工作树或容器里，它的改动会在资源回收之前保存下来：

1. 从 `result.metrics["entry_patch"]` 拿到补丁位置。
2. 先通读完整改动。
3. 在源仓库里显式应用这个补丁。
4. 运行项目测试。

即使关闭了轨迹记录，入口补丁仍会保存；没指定产物目录时，会自动创建一个恢复目录。

## 出了问题：先找回改动，再清理

隔离任务被取消、补丁导出失败或清理出错时，改动通常还在，只是放在了被保留的工作树、容器或 VM 工作区里。处理顺序永远是：**先把完整改动保存下来，再清理工作树、容器或 VM 租约。**

还要注意：OC 给父成员的摘要、工具输出都有显示长度限制，看到的可能只是半截补丁。完整的补丁是另外保存的，别直接用摘要，去读完整补丁或保留目录里的实际文件。

### 步骤一：找到改动在哪

| 来源 | 去哪找 |
| --- | --- |
| 显式隔离的 Team 入口 | `result.metrics.get("entry_patch")`，回收前就保存了 |
| 失败或停止的子任务 | 结果里会尽可能带上已有改动；补丁较大时，会报告保留的工作区 |
| 采纳冲突或捕获失败 | 错误信息直接给出完整候选材料或恢复补丁的位置；没启用产物目录时，存进私有的临时恢复目录 |

恢复位置的形式：本地是工作树路径；容器是完整容器 ID 加容器内目录；VM 的完整工作区会导出到控制端运行产物下的 `vm/<租约 token>/<角色目录>`，导出成功后直接在本机看文件和 Git 差异。VM 导出失败时，记下错误里的机器 ID、来宾目录、操作 ID 和本机产物路径，等 SSH 恢复后再继续导出。

### 步骤二：导出

从容器导出到一个新的恢复目录：

```bash
# CONTAINER_ID 和 /reported/workspace 换成报错给出的真实值
docker cp CONTAINER_ID:/reported/workspace ./recovered-workspace
```

然后检查里面的 Git 差异和任务产物。

### 步骤三：应用补丁

1. 先通读补丁内容。
2. 在目标目录检查能否应用：

   ```bash
   git apply --check /path/to/recovery.patch
   ```

3. 检查通过后再实际应用。

本机还有正在改的文件要保留时，先把恢复材料放到单独的目录里比较，再决定怎么合并。

### 步骤四：清理残留资源

1. 保存报错、完整资源 ID 和已经导出的材料。
2. 确认资源状态：`git worktree list`、目标目录里的文件、`docker inspect`。
3. 停止还在运行的任务，导出改动。
4. 重试清理对应的资源。

## VM 断线与租约恢复

租约是 OC 在控制端 SQLite 文件里写的一条记录，表示某台 VM 正被占用。VM 的问题分三种情况：

**你直接用 `VMEnvironment`，SSH 断了。** SSH 出错时 OC 抛出 `VMTransportError`，里面带着 `operation_id`，远端命令不会因为断线而丢失。等连接恢复后，调用 `await environment.recover_operation(error.operation_id)` 拿回这次操作的原始结果，用 `pending_operations` 看还有哪些操作没处理完。控制端要重建环境对象时，用原来的连接、工作区、状态目录和 `run_id`，新对象就会读到同一份远端记录。操作状态还不确定时，环境会先等恢复结果，再接受新的工作。

**VM 池归还机器时失败。** 归还一台 VM 要依次做完：导出内容、停止自己发起的操作、清理工作树、运行重置。管理员的 `reset_command` 成功、任务目录删掉之后，池才释放租约。其中任何一步失败，租约和恢复资料都会保留；只要池对象还活着，就可以重试清理。

**控制进程已经退出。** SQLite 里的租约不会自动消失。要手动释放一台机器，按顺序做：

1. 确认这台机器上对应的远端进程已经结束。
2. 导出需要保留的文件。
3. 完成管理员的来宾恢复程序。
4. 打开配置里 `lease_database` 指定的文件，按 `resource_id`、`owner` 和 `workspace` 核对，只删除这台资源对应的那一条记录。

```bash
# 表名和列名来自 OC 源码；先查看，再只删确认过的那一条
sqlite3 /home/controller/oc-state/vm-leases.sqlite3 \
  "SELECT resource_id, owner, workspace FROM vm_leases;"
sqlite3 /home/controller/oc-state/vm-leases.sqlite3 \
  "DELETE FROM vm_leases WHERE resource_id = 'worker-01' AND owner = '<租约 token>';"
```

还有机器在正常使用时，不要删它们的记录。新建的池只要继续用原数据库和同样的 `resource_id`，就能认出当前哪些机器被占用。VM 的准备和配置见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

## 保留、取消和资源归属的规则

**保留与释放**：

- 带采纳能力的工作区，交付没完成时会保留实际文件，并拒绝过早释放。
- 原生工作树之后如果成功拿到完整的 `get_diff()`，可以清除保留状态，继续清理。
- Team 采纳冲突时会给出完整的恢复补丁，释放运行资源之前把它另外存好。
- 自定义提供器也应该提供对应的导出和重试流程。

**取消时发生了什么**：先撤销环境的后续操作，再处理正在进行的命令和文件操作，最后释放资源。环境被撤销后，后续模型调用不会再产生工具副作用；Workflow 里排队等待的调用也会检查环境状态。

**资源归谁**：

| 资源 | 归谁 |
| --- | --- |
| 运行自己创建的工作树或容器 | 对应的环境池 |
| 外部传入的父环境 | 调用方 |
| 连接的已有容器 | 原来创建它的服务 |
| VM 池分配的机器租约 | 对应的环境池，归还前运行管理员的重置程序；VM 本身由你的基础设施管理 |

某个审阅环境依赖另一个候选目录时，清理会先处理依赖它的一方，被依赖的来源会保留到导出和释放都结束。

## 常见问题

- **Agent 改完了，项目里却没变化**：改动还在候选里，需要采纳。入口被隔离时，去 `entry_patch` 拿补丁。
- **看到的 diff 不完整**：显示用的 diff 有长度限制，完整采纳和恢复用的是另外捕获的完整材料。
- **报 source tracking 错误**：候选执行时会跟踪源工作区，采纳时也会检查来源版本，运行期间改了源目录就可能触发。候选位置会保留下来；来源变了以后再采纳，可能覆盖后来的修改。先比较当前源文件和候选起点，再决定重跑还是重新准备候选。
- **出现捕获错误**：按上面的“先找回改动，再清理”处理。

## 延伸阅读

- 源码依据：[隔离指南](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/session-isolation.zh-CN.md) · [Workflow 候选实现](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/workflow_candidates.py) · [调度器采纳逻辑](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/_scheduler_team.py) · [候选环境保留机制](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/_team_candidate_environment.py) · [Docker 池清理](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/docker_isolation.py)
- 相关指南：[隔离](isolation-overview.md) · [Team 协作](../team/dispatch-team-agents.md) · [组合与嵌套](../workflow/workflow-composition-hierarchy.md) · [运行怎样停下](../operations/timeouts-retries.md)
