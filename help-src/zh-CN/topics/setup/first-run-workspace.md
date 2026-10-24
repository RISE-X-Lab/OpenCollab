---
{
  "id": "first-run-workspace",
  "title": "在自己的项目中完成第一次运行",
  "category": "setup",
  "summary": "配好模型，用 --workspace 指向自己的项目，在终端跑通第一个任务并找到运行记录。",
  "aliases": [
    "第一次怎么用",
    "在哪个目录启动",
    "项目路径",
    "跑一句话",
    "一次性任务",
    "--hold"
  ],
  "keywords": [
    "快速开始",
    "workspace",
    "--workspace",
    "configs/.env",
    "uv run opencollab",
    "--prompt",
    "--prompt-file"
  ],
  "questions": [
    "选好项目目录，接通模型，并完成一次可观察的终端任务？",
    "第一次用 OC 该敲什么命令？",
    "模型的 API key 写在哪里？",
    "运行记录保存在哪里？"
  ],
  "related": [
    "install-source-checkout",
    "provider-endpoint-credentials",
    "interactive-cli-controls",
    "session-save-resume"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在自己的项目中完成第一次运行

最小用法：在 OpenCollab 源码根目录执行 `uv run opencollab --workspace /path/to/your-project`。这会以你的项目为工作区，启动内置的 Self-Collaboration 团队：入口角色是 Analyst，它可以把实现交给 Coder、把验证交给 Tester。下面依次是配模型、启动、找运行记录和试一个 Workflow。

两个目录别搞混：

| 目录 | 决定什么 |
| --- | --- |
| `--workspace` 指向的目录 | Agent 读写的项目文件。必须已经存在。 |
| 执行命令时所在的目录 | 用哪个 OpenCollab 源码环境（`uv run` 找哪个项目环境）。 |

## 第一步：配置模型

前提：已按安装指南装好源码环境。

1. 在 OpenCollab 源码根目录复制配置示例：

```bash
cp configs/.env.example configs/.env
```

2. 编辑 `configs/.env`，填写 provider、base URL、物理模型名称和凭据。下面是兼容 Chat Completions 的服务的写法，占位值换成服务实际提供的内容。凭据文件只留在本地。

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://your-endpoint.example/v1
OPENCOLLAB_MODEL=your-model-id
OPENCOLLAB_API_KEY=your-api-key
```

配置文件的查找顺序：先找工作区下的 `configs/.env` 和 `.env`，再找启动目录下的同名文件。两处设置不同时，按配置优先级指南确认哪个生效。想直接指定某个文件，在命令前设置 `OPENCOLLAB_CONFIG_FILE`。

## 第二步：交互式启动

```bash
uv run opencollab --workspace /path/to/application
```

进入后输入一个范围清楚的任务，例如"查看项目入口并说明测试命令"。

- 会话从 agent 0（入口角色 Analyst）开始。
- Analyst 可以把实现交给 Coder。Coder 在独立工作区里改代码，Analyst 采用改动后再安排 Tester。
- 实际怎么交接，取决于 Agent 真正发出的工具调用。只是理解项目的任务，入口角色也可能自己就做完了。
- 用 Tab 和 Shift+Tab 切换查看各成员的消息。

## 或者：一次性任务

只跑一轮就退出，用 `--prompt`。下面的 `--trace` 开启轨迹记录。

```bash
uv run opencollab --workspace /path/to/application \
  --prompt '阅读项目入口和测试配置，说明如何运行最小验证。' \
  --trace
```

相关选项：

| 选项 | 作用 |
| --- | --- |
| `--prompt` | 直接在命令行给任务。 |
| `--prompt-file /absolute/path/task.txt` | 从 UTF-8 文本文件读任务，适合长任务。文件最大 4 MiB。与 `--prompt` 互斥。 |
| `--hold` | 一次性任务结束后保留查看界面，可以切换成员，输入 `q` 或 `exit` 离开。必须配合 `--prompt` 或 `--prompt-file` 之一。 |
| `--trace` | 开启轨迹记录。 |

## 第三步：找到运行记录

- 终端会显示本次会话的保存位置。
- 团队运行自动保存在项目的 `.opencollab/sessions/` 下。每个成员一个 JSON 会话文件，另有 `team.json` 描述这次团队运行。
- 检查顺序：先看 Agent 的回答，再核对文件改动和实际的验证输出。
- 想接着主会话继续，用保存恢复指南里的 `--session`。

## 试一下 Workflow 入口

Workflow 走单独的子命令，选项要写在子命令后面。先列出这个工作区能发现的工作流，再看具体某个的帮助：

```bash
uv run opencollab workflow list --workspace /path/to/application
uv run opencollab workflow run duo --help
```

下面的命令运行 Duo，并允许它直接在宿主项目里执行 shell 命令：

```bash
uv run opencollab workflow run duo --workspace /path/to/application \
  --task '修复指定问题，并运行项目验证。' \
  --args '{"allow_unisolated_shell":true}'
```

- `--task` 会写入工作流的 `goal` 参数。如果 `--args` 里已经给了 `goal`，就别再写 `--task`。
- 需要进程隔离时，先按执行环境指南准备好环境再运行。

## 来源

[README.zh-CN.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/README.zh-CN.md#L47)　[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L44)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L58)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L123)

## 相关指南

[安装 OpenCollab](install-source-checkout.md)　[接入模型端点与认证凭据](../models/provider-endpoint-credentials.md)　[在终端里和 Agent 对话，以及命令参数该写在哪](interactive-cli-controls.md)　[保存会话与恢复历史](../operations/session-save-resume.md)
