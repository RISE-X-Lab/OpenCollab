---
{
  "id": "interactive-cli-controls",
  "title": "在终端里使用 OC",
  "category": "setup",
  "summary": "在交互终端里切换成员、追问、保存和中断，并把选项写在正确的命令层级上；了解 --yolo 等三个执行开关。",
  "aliases": [
    "切换 agent",
    "输入排队",
    "追问队友",
    "命令写哪里",
    "参数位置错误",
    "默认终端交互与命令参数位置",
    "快捷键",
    "--yolo",
    "--allow-local-child-shell",
    "--no-worktrees"
  ],
  "keywords": [
    "CLI",
    "opencollab",
    "Tab",
    "Shift+Tab",
    "/help",
    "/exit",
    "--team-config",
    "workflow run"
  ],
  "questions": [
    "在终端给指定 agent 发后续任务，查看团队状态，并正确组织根命令与子命令参数？",
    "怎么把消息发给 Tester 而不是 Analyst？",
    "为什么提示我参数位置不对？",
    "Ctrl+C 会发生什么？"
  ],
  "related": [
    "first-run-workspace",
    "session-save-resume",
    "timeouts-retries",
    "tool-selection"
  ],
  "verified_ref": "v0.9.6"
}
---

# 在终端里使用 OC

`opencollab` 根命令打开一个交互式团队终端。它和单线程的聊天界面不同：团队里每个成员各有一条会话，你的输入发给当前选中的那个成员。这篇讲三件事：怎么操作，命令参数该写在哪一层，以及三个和命令执行权限有关的开关。

## 常用操作

| 操作 | 作用 |
| --- | --- |
| Tab / Shift+Tab | 切换当前查看和输入的成员 |
| 直接输入文本 | 发给当前选中的成员；它正在运行时，消息会排队 |
| `/help` | 显示操作说明 |
| `/save` | 保存入口成员（lead）的主会话，终端打印文件位置 |
| `/exit`、`/quit`、`exit`、`quit` | 离开交互会话 |
| `q` | 一次性运行加了 `--hold` 时，退出查看界面 |
| Ctrl+C | 有正在进行的轮次或待回答的问题时：中断，并清空排队的输入；已经空闲时：退出 |

## 单独和某个成员说话

比如想问 Tester 测试结果：按 Tab 或 Shift+Tab 切到 Tester，输入问题并提交就行。

几条需要知道的规则：

- 启动后默认选中入口成员（agent 0）。
- 每个成员有独立的显示历史。切换时，界面会重画该成员在本次终端里收到的消息；输入框里还没提交的文字会保留。
- 配置里声明了、但还没真正创建的角色（标为 `available`）也在切换顺序里。它产生会话之后，界面才开始显示它的消息。
- 消息在**提交那一刻**就绑定到当前成员。排队期间你切到别人，这条消息仍然发给原来那个。
- 如果成员正在向你提问，你的下一次输入会被当作回答，`/save` 这类文字也一样。回答之后，才能继续用本地命令或提交新任务。按 Ctrl+C 中断这一轮后，这一轮剩下的提问不会再出现，记为用户拒绝回答（`User declined to answer.`），待确认的命令按拒绝处理，输入框交还给你。

Ctrl+C 中断之后，先看终端报告的停止原因和保留下来的文件，再决定下一步。见 [运行怎样停下](../operations/timeouts-retries.md) 和 [保存与恢复会话](../operations/session-save-resume.md)。

## 参数要跟着自己的命令写

`opencollab` 有几个子命令，每一层有自己的选项。下面四条分别是：启动交互团队、检查团队文件、列出 Workflow、运行指定的 Workflow。

```bash
opencollab --workspace /path/to/application --team-config team.yaml
opencollab team show --team-config team.yaml
opencollab workflow list --workspace /path/to/application
opencollab workflow run duo --workspace /path/to/application \
  --task '检查指定问题' --args '{"allow_unisolated_shell":true}'
```

- Workflow 的 `--workspace`、`--model`、`--budget`、`--concurrency` 等选项，要写在 `workflow run NAME` **后面**。写在 `workflow` 前面，会被当成交互团队的根选项，并出现参数位置提示。
- `team show` 只读取角色、工具和通信关系，不启动会话，也不需要凭据。适合先确认实际会用哪个团队。
- 提示选项未知时，看那一层的 `--help`。
- 示例里的 Duo 允许直接在本机工作区执行 shell 命令，只在可信项目上这样做。

## 三个与命令执行有关的开关

| 选项 | 作用 | 注意 |
| --- | --- | --- |
| `--yolo` | 自动批准被判定为有风险的命令 | 按任务的授权要求决定开不开 |
| `--allow-local-child-shell` | 允许运行中派生的子成员在本机执行命令 | 这些命令没有操作系统沙箱保护 |
| `--no-worktrees` | 关闭子成员的 Git 工作树隔离 | 子成员不再各用一个独立工作树 |

要分清两件事：Git 工作树只把成员的**文件改动**分开；命令能访问哪些进程、网络和系统资源，由执行环境决定。两者的关系见 [隔离](../isolation/isolation-overview.md)，命令权限的完整规则见 [内置工具](../extensions/tool-selection.md#bash命令在哪里执行谁允许它执行)。

## 延伸阅读

- 源码依据：[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/cli/main.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/README.md)
- 相关指南：[安装并完成第一次运行](first-run-workspace.md) · [保存与恢复会话](../operations/session-save-resume.md) · [运行怎样停下](../operations/timeouts-retries.md)
