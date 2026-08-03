# Mini Edict

[English](README.md) | [简体中文](README.zh-CN.md)

Mini Edict 把三省六部组织方式写成了一套紧凑且可执行的协作协议。这个目录包含示例所需的全部配置、工作流、说明和测试。Agent 会话、模型接入、并发、预算、追踪和持久化由 OpenCollab 提供。

```text
任务
  -> 中书省起草方案
  -> 门下省审议并准奏或封驳
       -> 封驳时由中书省修订，最多两轮
       -> 准奏后由尚书省选择相关部门
            -> 被选中的六部并行执行
  -> 尚书省汇总带有证据的奏报
  -> 门下省进行最终复核
       -> 形成完成或阻断结果
```

工作流用 Python 控制流程保证职责分离。门下省批准前无法进入执行阶段。封驳意见会交回中书省用于修订。尚书省根据任务选择有关部门，并为每个部门给出独立任务与验收条件。最终复核会同时检查批准后的方案、部门分工、执行报告和证据。

## 与中书省对话

使用 Mini Edict 的团队配置启动 OpenCollab。

```bash
uv run opencollab \
  --team-config examples/mini-edict/team.yaml \
  --workspace .
```

进入 CLI 后，常驻入口角色就是中书省。普通问题会由中书省直接回答。给出一项具体任务，或者输入 `convene the court`，中书省就会起草方案并请门下省审议。方案获准后，尚书省会通过 OpenCollab 的团队调度器选择并启动有关部门，最后返回经过复核的奏报。团队拓扑禁止中书省绕过尚书省直接派发六部，也没有为门下省开放派发路径。

## 执行一道政令

同一套组织方式也可以作为流程固定的单次工作流运行。

```bash
OPENCOLLAB_WORKFLOWS_DIR=examples/mini-edict/workflows \
  uv run opencollab workflow run three-departments-six-ministries \
  --concurrency 6 \
  --args '{"task":"Design a six-month open-source community growth plan."}'
```

命令会显示每个阶段，并返回中书方案、审议记录、派发决定、六部报告、最终奏报、复核结果和 token 用量。

## 设计参考

工作流吸收了 Edict 的[门下省强制审议](https://github.com/cft0808/edict/blob/main/agents/menxia/SOUL.md)和[尚书省动态派发](https://github.com/cft0808/edict/blob/main/agents/shangshu/SOUL.md)设计，也参考了 [Agent-Team](https://github.com/EthanHuangEbor/Agent-Team) 对技能封装、有限修订、按需派发和证据化输出的处理。这里通过 OpenCollab 的工作流 API 表达这些规则，没有复制两个项目的运行时和运维代码。

## 为什么它很小

Mini Edict 的组织逻辑集中在一个团队文件和一个工作流文件中。修改角色职责时编辑 `team.yaml`，调整阶段顺序时编辑工作流即可。其余通用能力由 OpenCollab 统一提供。

这个示例展示了 OpenCollab 的核心价值。一种原本需要完整多智能体仓库表达的协作构想，可以缩成一套容易阅读、运行和修改的小型协议。
