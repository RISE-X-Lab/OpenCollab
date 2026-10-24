---
{
  "id": "interactive-cli-controls",
  "title": "在终端里和 Agent 对话，以及命令参数该写在哪",
  "category": "setup",
  "summary": "在交互终端里切换成员、追问、保存和中断，并把选项写在正确的命令层级上。",
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
    "cancellation-cleanup"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在终端里和 Agent 对话，以及命令参数该写在哪

`opencollab` 根命令打开一个交互式团队终端。和单线程聊天界面不同，这里每个成员各有一条会话：你的输入发给当前选中的成员，用 Tab / Shift+Tab 切换；运行中还能继续输入，后续任务会排队。工作流和团队配置检查走各自的子命令，选项要写在对应子命令后面。

## 快速参考

| 操作 | 作用 |
| --- | --- |
| Tab / Shift+Tab | 切换查看和投递的对象 |
| 直接输入文本 | 发给当前选中的 Agent；运行中会排队 |
| `/help` | 在本地显示操作说明 |
| `/save` | 保存 lead 主会话，终端打印文件位置 |
| `/exit`、`/quit`、`exit`、`quit` | 离开交互会话 |
| `q` | 一次性运行加了 `--hold` 时，退出查看界面 |
| Ctrl+C | 有当前轮次或待回答问题时发起中断，并清空排队的轮次；已经空闲时退出 |

## 给某个成员追问

例：想问 Tester 测试结果。

1. 按 Tab 或 Shift+Tab 切到 Tester。
2. 输入问题并提交。

需要知道的默认行为：

- 启动后先选中 agent 0。
- 每个 Agent 有独立的显示历史。切换成员时，界面重画该成员在本次终端会话里收到的消息；输入框里没提交的内容会保留。
- 配置里标为 `available` 的角色也在切换顺序里。它真正产生会话之后，界面才开始显示它的消息。
- 消息在提交那一刻就绑定到当前选中的 Agent。排队期间你切到别的成员，消息仍发给原来那个。
- 如果 Agent 正在向你提问，你的下一次输入会被当作回答，`/save` 这类文本也一样。回答完之后，才能继续用本地控制命令或提交普通任务。

## 中断之后

Ctrl+C 停下后，先看终端报告的停止原因和保留下来的文件，再决定下一步。保存恢复和资源清理的细节见相应指南。

## 选项要跟着自己的命令写

下面四条分别是：启动交互团队、检查团队文件、列出工作流、运行指定工作流。

```bash
opencollab --workspace /path/to/application --team-config team.yaml
opencollab team show --team-config team.yaml
opencollab workflow list --workspace /path/to/application
opencollab workflow run duo --workspace /path/to/application \
  --task '检查指定问题' --args '{"allow_unisolated_shell":true}'
```

- 如果把 `--workspace` 或模型选项写在 `workflow` 前面，它会被当成交互团队的根选项，并出现参数位置提示。
- 工作流的 `--model`、`--budget`、`--concurrency` 等选项要写在 `workflow run NAME` 后面。
- `team show` 只读取角色、工具和有向关系配置，不启动 Agent 会话。适合先确认实际会用哪个团队。
- 提示选项未知时，先看那一层的 `--help`。
- 示例里的 Duo 允许直接在宿主工作区执行 shell 命令。

## 进阶：三个执行相关的选项

| 选项 | 作用 | 注意 |
| --- | --- | --- |
| `--yolo` | 自动批准被判定为有风险的命令 | 按任务的授权方式决定要不要开 |
| `--allow-local-child-shell` | 允许运行时动态创建的子 Agent 在宿主机执行命令 | 这些命令没有操作系统沙箱保护 |
| `--no-worktrees` | 关闭子成员的 Git 工作树隔离 | 子成员不再各自使用独立的 Git 工作树 |

前两个按任务的授权方式和执行环境分别设置。Git 工作树只负责把成员的文件改动分开；命令能访问哪些进程、网络和系统资源，由执行环境决定。具体共享范围见隔离指南。

## 来源

[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L58)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L182)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L227)　[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L77)

## 相关指南

[在自己的项目中完成第一次运行](first-run-workspace.md)　[保存会话与恢复历史](../operations/session-save-resume.md)　[停止任务并确认清理完成](../operations/cancellation-cleanup.md)
