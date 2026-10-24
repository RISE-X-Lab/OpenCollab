---
{
  "id": "configure-team",
  "title": "Team 入门：配置你的多 Agent 团队",
  "category": "team",
  "summary": "写一份 team.yaml，规定团队里有哪些角色、各自能用什么工具、谁能给谁派活。",
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
    "choose-working-mode",
    "dispatch-team-agents",
    "message-team-agents",
    "wait-for-team-results",
    "isolation-configuration",
    "tool-selection",
    "token-step-concurrency-limits"
  ],
  "verified_ref": "0c47f74a"
}
---

# Team 入门：配置你的多 Agent 团队

Team 是 OC 里由模型自主调度的多 agent 模式，思路接近 AutoGen 的 group 或 Claude Code 的 subagent：入口成员（agent 0）接收任务，在运行中用 `spawn_agent` 派发子成员、用 `message_agent` 互发消息，最后汇总。团队结构写在一份 `team.yaml` 里：有哪些角色、每个角色的工具白名单、以及一张有向图（topology）规定谁能派发或联系谁。最小用法是 `opencollab team init team.yaml` 导出内置团队，改完后用 `--team-config` 运行。

和 Workflow 的取舍：

- Team：下一步找谁、做几轮，由入口成员的模型决定。
- Workflow：用 Python 显式编排 agent 调用，步骤、分支和重试由代码决定，见 [我该用默认 CLI、Base、Team 还是 Workflow](../orientation/choose-working-mode.md)。

## 三条命令跑起来

1. 导出内置团队作为模板：

   ```bash
   opencollab team init team.yaml
   ```

   如果 `team.yaml` 已经存在，这条命令不会覆盖它。

2. 检查配置是否读得懂：

   ```bash
   opencollab team show --team-config team.yaml
   ```

   它会列出入口成员、每个角色的工具和通信方向。前两步还不需要配置 API 凭据。

3. 正式启动协作：

   ```bash
   opencollab --team-config team.yaml --workspace .
   ```

选团队文件的优先顺序是：CLI 的 `--team-config` 或 SDK 的 `team(config=...)` 最优先，其次是环境变量 `OPENCOLLAB_TEAM_FILE`。使用自建的 `team.yaml` 时，要通过上面某种方式明确指定。

### 不写团队文件时的内置团队

| 角色 | 负责什么 |
| --- | --- |
| Analyst（入口，`analyst`） | 接收任务，拥有派发和采纳工具 |
| Coder | 修改代码 |
| Tester | 执行验证 |

内置团队只允许 Analyst 给 Coder 和 Tester 派活。配置里声明的角色一开始只出现在团队组织记录里，真正被派发后才会出现对应的活跃成员。

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
| `entry` | 入口成员，也叫 agent 0，第一个收到用户任务。省略时先找名为 `lead` 的角色，找不到就用第一个声明的角色。 |
| `roles` | 角色列表。键就是角色名。 |
| `prompt` / `prompt_file` | 角色的职责说明，二选一必填。两者都写时，内联 `prompt` 优先。 |
| `tools` | 这个角色能调用的工具。 |
| `model`、`temperature`、`thinking`、`thinking_params`、`profile` | 可选，单独给角色设置模型和采样参数。 |
| `topology` | 通信方向，见下一节。 |

几点需要注意：

- 角色名就是成员身份。它不能包含路径分隔符、首尾空格或控制字符。两个名字在 Unicode 与大小写归一后如果相同，也会报错。
- 改了角色名，要同步改 `topology` 和发消息时用的名字。
- `prompt_file` 的路径相对于团队文件所在目录，读取时会做安全检查和目录范围检查。职责写得比较长时，放进单独的文件更好维护。
- 没设置的模型和采样项会沿用客户端的全局值。写 `temperature: 0.0` 会明确覆盖全局值。
- profile：OC 的角色预设，提供基础提示词、上下文处理方式和安全配置。角色最终能做什么，由职责和工具共同决定。
- 给了 `bash` 不代表一定能执行命令，还取决于执行环境和权限设置，见 [管理终端权限并确认命令结果](../extensions/shell-permissions-execution.md)。

## 拓扑：谁能联系谁

拓扑（topology）是一张有方向的图。`manager: [coder, tester]` 表示 manager 可以联系 coder 和 tester，反过来不行。

它同时管两件事：

1. 派发：manager 可以创建 coder 和 tester。
2. 发消息：manager 如果有 `message_agent` 工具，可以给这两类角色发消息。

和很多框架不同，OC 的工具白名单和拓扑是两道独立检查：有工具但拓扑没连线，调用会被拒绝；反之亦然。

想让 coder 回信给 manager，要做两处改动：

1. 给 coder 的 `tools` 加上 `message_agent`。
2. 把 `coder: []` 改成 `coder: [manager]`。

拓扑只管成员之间的显式协作。成员是否共用同一个工作目录，由隔离配置决定。如果某个角色调用了 Workflow，Workflow 内部由它自己的 Python 代码组织。具体用法见 [派发 Agent 任务并安排审阅](dispatch-team-agents.md) 和 [向队友发消息并继续协作](message-team-agents.md)。

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

这个示例用独立容器执行各成员的任务，需要提前准备好 Docker daemon 和任务镜像。隔离细节见 [默认隔离与单次覆盖怎么配](../isolation/isolation-configuration.md)。

`serialize_turns=True` 会让成员轮流执行。发消息的一方结束当前回合后，队友才有机会继续。

## 按需成员还是固定名册

| 方式 | 设置 | 特点 |
| --- | --- | --- |
| 按需成员（默认） | `prebuild_team=False` | 只启动入口，其他成员在派发时才创建。同一角色可以派多次，每个成员有自己的 aid 和独立会话。 |
| 固定名册 | `prebuild_team=True` | 第一次调用模型前，就为每个声明的角色建好成员。之后再派发创建会被拒绝，靠消息反复交接任务。 |

aid：成员编号，用来区分同一角色的多个成员。按需团队里同一角色有多个成员时，发消息要指定具体 aid。

两种方式的上下文积累和预算消耗不同：按需成员每次从新会话开始，固定名册让同一成员持续接新活。用固定名册时，各角色需要有消息工具，拓扑也要允许对应方向。

## 设置 token 预算

| 写法 | 效果 |
| --- | --- |
| 所有角色都不写额度，SDK 传 `budget=120_000` | 整个团队共享一个 token 池 |
| 团队文件顶层写 `budget: { tokens: 40_000 }` | 每个角色各有独立额度 |
| 单个角色写自己的 `budget` | 覆盖顶层给它的额度 |

使用角色额度时有三条规则：

- 只要声明了一个角色的额度，就必须给全部角色都声明。
- 必须使用 `prebuild_team=True`。
- 总额由文件给出，SDK 再传 `budget=` 会报错。

更多预算设置见 [设置 token 预算、步数与运行并发](../operations/token-step-concurrency-limits.md)。

## 运行自带的固定名册团队

源码里的 `configs/team.collab.yaml` 带有 Analyst、Coder、Tester 三个固定成员，它们通过双向消息交接任务。在源码根目录运行：

```bash
uv run python scripts/run_collab_team.py \
  --workspace /path/to/application \
  --prompt '修复指定问题，由 Coder 实现、Tester 验证，再交付结果。' \
  --budget 300000 --max-steps 100 --timeout 900 \
  --artifacts /absolute/path/artifacts/collab-001 \
  --allow-unisolated-shell
```

脚本会为三个角色建立会话，默认按串行回合运行。示例中的 `--allow-unisolated-shell` 明确允许这些成员在可信的宿主项目里执行 Git 和测试命令。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--prompt-file task.md` | 替代 `--prompt`，任务较长时存进 UTF-8 文件 |
| `--config` | 换一份固定名册文件 |
| `--concurrent` | 允许已唤醒的成员和当前成员同时运行 |
| `--max-steps` | 每个角色的步数上限，默认 100 |
| `--budget` | 共享 token 池，默认 1,000,000。团队文件已声明全部角色额度时，由文件决定，调用时省略 `--budget` |

这份团队怎样交付，写在角色提示里：

1. Coder 在自己的 Git 工作树（worktree）里提交改动，然后用消息发出 commit SHA。
2. Tester 和 Analyst 用这个提交拿到同样的文件。
3. 最终交付目录是 Analyst 的工作目录。

使用独立工作树时，源仓库要满足工作树的准备要求，交接的提交也必须真正进入入口目录。按需团队用 `adopt_agent` 交付，见 [采用队友改动并验证交付](adopt-team-changes.md)。

用 SDK 跑这份文件时，传 `config="configs/team.collab.yaml"` 和 `prebuild_team=True`。要串行交接，再加 `serialize_turns=True`。允许宿主 shell 用 `allow_unisolated_shell=True`。根命令的 `--team-config` 只负责选择文件，固定名册要由 SDK 或这个专用脚本来创建。

脚本会给成员的 shell 设置默认环境值，关闭 pytest 缓存和 Python 字节码，方便导出工作树改动。调用方已有的设置会保留。

启动参数与结果摘要见[固定名册启动脚本](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/scripts/run_collab_team.py)，角色交接职责见[自带团队文件](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/team.collab.yaml)。

## 进阶：按业务改造团队

- 角色名和职责可以随意改写。入口也可以换成研究者、分诊员或调度员。
- 入口要直接处理文件时，给它文件工具。
- 要调用已注册的工作流，加上 `run_workflow` 工具。
- 成员要自己编写并运行工作单元，使用工作流管理能力，见 [让成员自己写并运行子 Workflow](../workflow/member-authored-workflow.md)。

## 运行完怎么确认结果

1. 先看 `result.status` 和 `result.reason`。
2. 再核对 `output` 里的完成说明和产物目录。
3. 工作树里的实现不会自动生效，要显式采纳，采纳后再安排新的测试成员验证。
4. 如果入口自己也被隔离，交付补丁还要由你应用到最终项目。
5. 执行记录可以看到每个成员实际调用了哪些工具、交付了哪些改动。

配置字段与默认值见[团队配置实现](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/team_config.py)和[配置指南](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md)。

## 下一步读什么

- [派发 Agent 任务并安排审阅](dispatch-team-agents.md)
- [向队友发消息并继续协作](message-team-agents.md)
- [等待队友结果并读懂团队状态](wait-for-team-results.md)
- [选择 Agent 能使用的工具](../extensions/tool-selection.md)
