---
{
  "id": "configure-team",
  "title": "Team 入门：用 team.yaml 组织多个 Agent",
  "category": "team",
  "summary": "写一份 team.yaml：有哪些角色、各自能用什么工具、谁能联系谁；再选按需成员或固定名册，并设置团队预算。",
  "aliases": [
    "team init",
    "team show",
    "--team-config",
    "OPENCOLLAB_TEAM_FILE",
    "组织结构",
    "通信拓扑",
    "谁能给谁派活",
    "prebuild_team",
    "run_collab_team.py",
    "固定名册启动",
    "--concurrent",
    "Team 完整指南与团队配置"
  ],
  "keywords": [
    "team.yaml",
    "团队配置",
    "角色",
    "topology",
    "入口成员"
  ],
  "questions": [
    "如何创建自己的多 Agent 团队？",
    "谁可以派任务、谁可以给谁发消息？",
    "固定团队和动态创建成员怎样选择？",
    "team.yaml 里每个字段是什么意思？",
    "怎样给团队设置 token 预算？"
  ],
  "related": [
    "dispatch-team-agents",
    "team-run-workflow",
    "isolation-overview",
    "tool-selection",
    "token-step-concurrency-limits"
  ],
  "verified_ref": "0c47f74a"
}
---

# Team 入门：用 team.yaml 组织多个 Agent

Team 是 OC 里“由模型自己分工”的多 Agent 方式：入口成员（agent 0）接到任务后，在运行中自己决定把活派给谁、给谁发消息，最后汇总交付。你要做的，是在一份 `team.yaml` 里写清三件事：有哪些角色，每个角色能用哪些工具，谁可以联系谁。

和 Workflow 的区别只有一句话：Team 里“下一步找谁、做几轮”由入口成员的模型决定；Workflow 里由你写的 Python 代码决定。见 [OC 是什么](../orientation/concepts-and-modes.md#多个会话怎样组织三种方式)。

## 三条命令跑起来

1. 导出内置团队作为模板（`team.yaml` 已存在时不会覆盖）：

   ```bash
   opencollab team init team.yaml
   ```

2. 检查配置能不能读懂。它会列出入口成员、每个角色的工具和通信方向，不需要 API 凭据：

   ```bash
   opencollab team show --team-config team.yaml
   ```

3. 正式启动：

   ```bash
   opencollab --team-config team.yaml --workspace .
   ```

选团队文件的优先顺序：CLI 的 `--team-config` 或 SDK 的 `team(config=...)` 最优先，其次是环境变量 `OPENCOLLAB_TEAM_FILE`。用自建的 `team.yaml` 时，要通过其中一种方式明确指定。

### 不写团队文件时的内置团队

| 角色 | 负责什么 |
| --- | --- |
| Analyst（入口，`analyst`） | 接收任务，拥有派发和采纳工具 |
| Coder | 修改代码 |
| Tester | 执行验证 |

内置团队只允许 Analyst 给 Coder 和 Tester 派活。配置里声明的角色一开始只出现在团队组织记录里，真正被派发之后才会出现对应的活跃成员。

## 写一份自己的 team.yaml

下面的团队有三个角色：manager 负责分派和交付，coder 改文件，tester 读文件并运行项目命令。

```yaml
entry: manager            # 接收用户任务的成员
roles:
  manager:
    prompt: |
      Delegate implementation to coder. Adopt the completed coder's changes,
      then spawn a fresh tester. Summarize the delivered files and test evidence.
    tools: [spawn_agent, adopt_agent, file_read, team_status]
  coder:
    prompt: Implement the assigned change and report the affected files.
    tools: [file_read, apply_patch, grep]
  tester:
    prompt: Run the project's relevant tests against the delivered files.
    tools: [bash, file_read, git_diff, grep]
topology:                 # 谁可以联系谁
  manager: [coder, tester]
  coder: []
  tester: []
```

### 字段说明

| 字段 | 作用 |
| --- | --- |
| `entry` | 入口成员（agent 0），第一个收到用户任务。省略时先找名为 `lead` 的角色，找不到就用第一个声明的角色 |
| `roles` | 角色表，键就是角色名 |
| `prompt` / `prompt_file` | 角色职责说明，二选一必填；两者都写时用内联的 `prompt` |
| `tools` | 这个角色能调用的工具 |
| `model`、`temperature`、`thinking`、`thinking_params`、`profile` | 可选，单独给这个角色设置模型和采样参数 |
| `topology` | 通信方向，见下一节 |

几点注意：

- 角色名就是成员身份。不能含路径分隔符、首尾空格或控制字符；两个名字在 Unicode 与大小写归一后相同，也会报错。改了角色名，要同步改 `topology` 和发消息时用的名字。
- `prompt_file` 的路径相对于团队文件所在目录，读取时会做安全检查和范围检查。职责写得长时，放进单独文件更好维护。
- 没设置的模型和采样项沿用客户端的全局值。写 `temperature: 0.0` 会明确覆盖全局值。
- `profile` 是 OC 的角色预设，提供基础提示词、上下文处理方式和安全配置。角色最终能做什么，由职责和工具共同决定。
- 给了 `bash` 不代表一定能执行命令，还要看执行环境和权限设置，见 [内置工具](../extensions/tool-selection.md#bash命令在哪里执行谁允许它执行)。

## 拓扑：谁能联系谁

拓扑（topology）是一张有方向的图。`manager: [coder, tester]` 表示 manager 可以联系 coder 和 tester，反过来不行。它同时管两件事：

1. 派发：manager 可以创建 coder 和 tester 成员。
2. 发消息：manager 如果有 `message_agent` 工具，可以给这两类角色发消息。

和很多框架不同，OC 的**工具白名单和拓扑是两道独立的检查**：有工具但拓扑没连线，调用会被拒绝；拓扑连了线但没有工具，也做不了。所以想让 coder 回信给 manager，要改两处：给 coder 的 `tools` 加上 `message_agent`，并把 `coder: []` 改成 `coder: [manager]`。

拓扑只管成员之间的显式协作。成员是否共用同一个工作目录，由隔离配置决定，见 [隔离](../isolation/isolation-overview.md)。派发、发消息、等待和采纳的具体用法见 [Team 协作](dispatch-team-agents.md)。

## 用 SDK 启动团队

```python
import asyncio
from opencollab import OpenCollab

async def main():
    client = OpenCollab(".")
    result = await client.team(
        "实现目标修改并验证交付",
        config="team.yaml",                    # 团队文件
        budget=120_000,                        # 全队共享的 token 池
        isolation="docker-session",            # 每个成员在独立容器里执行
        artifacts="artifacts/team-change-001", # 产物目录，用一个新目录
    )
    print(result.status, result.output)

asyncio.run(main())
```

这个例子让每个成员在独立容器里执行，需要提前准备好 Docker 和任务镜像，见 [在容器中运行任务](../isolation/docker-isolation.md)。

另一个常用参数是 `serialize_turns=True`：成员轮流执行，发消息的一方结束当前回合后，队友才有机会继续。

## 按需成员还是固定名册

| 方式 | 设置 | 特点 |
| --- | --- | --- |
| 按需成员（默认） | `prebuild_team=False` | 只启动入口，其他成员在被派发时才创建。同一角色可以派多次，每个成员有自己的编号（aid）和独立会话 |
| 固定名册 | `prebuild_team=True` | 第一次调用模型前，就为每个声明的角色建好成员。之后再派发创建会被拒绝，靠消息反复交接任务 |

aid 是 OC 给每个成员分配的整数编号，用来区分同一角色的多个成员。按需团队里同一角色有多个成员时，发消息要指定具体 aid。

两种方式积累上下文和消耗预算的方式不同：按需成员每次从新会话开始；固定名册让同一个成员持续接新活。用固定名册时，各角色需要有消息工具，拓扑也要允许对应方向。

## 设置团队预算

| 写法 | 效果 |
| --- | --- |
| 所有角色都不写额度，SDK 传 `budget=120_000` | 整个团队共享一个 token 池 |
| 团队文件顶层写 `budget: { tokens: 40_000 }` | 每个角色各有独立额度 |
| 单个角色写自己的 `budget` | 覆盖顶层给它的额度 |

使用角色额度时有三条规则：声明了一个角色的额度，就必须给全部角色都声明；必须使用 `prebuild_team=True`；总额由文件给出，SDK 再传 `budget=` 会报错。共享池怎样分给成员，见 [预算、步数与并发](../operations/token-step-concurrency-limits.md#团队怎样分配-token)。

## 自带的固定名册团队

源码里的 `configs/team.collab.yaml` 有 Analyst、Coder、Tester 三个固定成员，通过双向消息交接任务。在源码根目录运行：

```bash
uv run python scripts/run_collab_team.py \
  --workspace /path/to/application \
  --prompt '修复指定问题，由 Coder 实现、Tester 验证，再交付结果。' \
  --budget 300000 --max-steps 100 --timeout 900 \
  --artifacts /absolute/path/artifacts/collab-001 \
  --allow-unisolated-shell
```

脚本为三个角色建立会话，默认按串行回合运行。`--allow-unisolated-shell` 明确允许这些成员在可信的本机项目里执行 Git 和测试命令。

| 参数 | 作用 |
| --- | --- |
| `--prompt-file task.md` | 代替 `--prompt`，长任务存进 UTF-8 文件 |
| `--config` | 换一份固定名册文件 |
| `--concurrent` | 允许已唤醒的成员和当前成员同时运行 |
| `--max-steps` | 每个角色的步数上限，默认 100 |
| `--budget` | 共享 token 池，默认 1,000,000；团队文件已声明全部角色额度时省略 |

这份团队的交付方式写在角色提示里：Coder 在自己的 Git 工作树里提交改动，然后用消息发出 commit SHA；Tester 和 Analyst 用这个提交拿到同样的文件；最终交付目录是 Analyst 的工作目录。按需团队则用 `adopt_agent` 交付，见 [Team 协作](dispatch-team-agents.md#第四步采纳改动再验证)。

用 SDK 跑这份文件时，传 `config="configs/team.collab.yaml"` 和 `prebuild_team=True`；要串行交接再加 `serialize_turns=True`；允许本机 shell 用 `allow_unisolated_shell=True`。根命令的 `--team-config` 只负责选文件，固定名册要由 SDK 或这个专用脚本创建。脚本还会给成员的 shell 设置默认环境值，关闭 pytest 缓存和 Python 字节码，方便导出工作树改动；调用方已有的设置会保留。

## 按业务改造团队

- 角色名和职责可以随意改写，入口也可以换成研究者、分诊员或调度员。
- 入口要直接处理文件时，给它文件工具。
- 要调用已注册的 Workflow，加 `run_workflow`；要让成员自己写并运行子 Workflow，加 `manage_workflow`。见 [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md)。
- 要按需加载说明材料，加 `use_skill`，见 [Skill](../extensions/skills-on-demand.md)；要在工具调用前后运行命令，见 [Hook](../extensions/team-hooks.md)。

## 运行完怎么确认结果

1. 先看 `result.status` 和 `result.reason`。
2. 再核对 `output` 里的完成说明和产物目录。
3. 工作树里的实现不会自动生效，要显式采纳，采纳后再安排新的 Tester 验证。
4. 如果入口自己也被隔离，交付补丁还要由你应用到最终项目，见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md)。
5. 想核对成员是否按分工协作，见 [检查团队是否真的按分工协作](../observability/collaboration-adherence.md)。

## 延伸阅读

- 源码依据：[opencollab/bootstrap/team_config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/team_config.py) · [configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/README.md) · [scripts/run_collab_team.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/scripts/run_collab_team.py) · [configs/team.collab.yaml](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/team.collab.yaml)
- 相关指南：[Team 协作](dispatch-team-agents.md) · [内置工具](../extensions/tool-selection.md) · [隔离](../isolation/isolation-overview.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md)
