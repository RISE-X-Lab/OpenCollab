---
{
  "id": "first-run-workspace",
  "title": "安装并完成第一次运行",
  "category": "setup",
  "summary": "从源码装好 OC，在 configs/.env 里接上模型，用 --workspace 指向自己的项目跑通第一个任务，并找到运行记录。",
  "aliases": [
    "第一次怎么用",
    "在哪个目录启动",
    "项目路径",
    "跑一句话",
    "一次性任务",
    "--hold",
    "怎么装 OC",
    "环境要求",
    "支持 Windows 吗",
    "稳定版",
    "release",
    "current main",
    "虚拟环境",
    "源码安装",
    "安装前提与版本选择"
  ],
  "keywords": [
    "快速开始",
    "workspace",
    "--workspace",
    "configs/.env",
    "uv run opencollab",
    "--prompt",
    "--prompt-file",
    "安装",
    "Python",
    "Linux",
    "macOS",
    "uv sync --locked",
    "pip install -e .",
    "pyproject.toml",
    "版本"
  ],
  "questions": [
    "选好项目目录，接通模型，并完成一次可观察的终端任务？",
    "第一次用 OC 该敲什么命令？",
    "模型的 API key 写在哪里？",
    "运行记录保存在哪里？",
    "选定适合自己的发布版本或当前源码，准备能够启动 OpenCollab 的 Python 环境？",
    "OpenCollab 怎么安装？",
    "支持 Windows 吗？",
    "装好了但终端说找不到 opencollab 怎么办？"
  ],
  "related": [
    "interactive-cli-controls",
    "python-sdk-entrypoints",
    "configuration-precedence",
    "provider-endpoint-credentials",
    "run-registered-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 安装并完成第一次运行

目标：从零开始，十几分钟内在你自己的项目上跑通一个任务，并知道去哪里看结果。一共四步：装好 OC、接上模型、运行、看记录。

## 第一步：安装

OC 支持 Linux 和 macOS，需要 Python 3.10 及以上。

### 下载源码并选好版本

在你平时放代码的目录里执行：

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git
cd OpenCollab
# 切到本 Wiki 核对过的源码提交
git checkout 0c47f74a27441888286a22012b23b658185f8b8f
# 确认 Python 版本，应为 3.10 或更高
python3 --version
```

这个提交的包版本号仍是 0.9.5，和更早的 0.9.5 代码没法靠版本号区分。本 Wiki 介绍的 VM 隔离和 `manage_workflow describe` 都需要这个提交，请以提交号为准。之后所有命令都在这个源码根目录里执行。

### 安装依赖

两种方式选一种，之后一直用同一种。

方式 A，用 uv。它按仓库里的锁文件创建并同步环境：

```bash
uv sync --locked
uv run opencollab --help
```

如果你要修改框架源码并跑开发检查，改用 `uv sync --locked --extra dev`。

方式 B，用 Python 自带的 venv 和 pip。`-e` 是可编辑安装，改了源码马上生效：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/opencollab --help
```

用发布版 wheel 的话，在你选好的虚拟环境里按该发布提供的方式安装，装完同样做下面的检查。

### 确认装好了

下面三条命令都正常输出，就说明安装完成。这是 uv 的写法；用 pip 的话，把 `uv run python` 换成 `.venv/bin/python`，把 `uv run opencollab` 换成 `.venv/bin/opencollab`。

```bash
uv run python -c 'from importlib.metadata import version; print(version("opencollab"))'
uv run python -c 'from opencollab import OpenCollab, RunResult; print(OpenCollab, RunResult)'
uv run opencollab workflow --help
```

## 第二步：接上模型

OC 自己不带模型，需要接一个模型服务。在源码根目录复制配置示例：

```bash
cp configs/.env.example configs/.env
```

编辑 `configs/.env`，填四项：服务类型、地址、模型名和凭据。下面是 OpenAI 兼容服务的写法，占位值换成你的服务实际提供的内容：

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://your-endpoint.example/v1
OPENCOLLAB_MODEL=your-model-id
OPENCOLLAB_API_KEY=your-api-key
```

- 凭据文件只留在本地，不要提交到仓库。
- OC 会先找工作区里的 `configs/.env` 和 `.env`，再找启动目录里的同名文件，并按变量逐个合并。想只用一个文件，就在命令前设置 `OPENCOLLAB_CONFIG_FILE=/绝对路径`。见 [配置从哪来、哪个生效](../models/configuration-precedence.md)。
- 原生 Anthropic、Responses 协议、同时接多个服务，见 [接入模型服务](../models/provider-endpoint-credentials.md)。

## 第三步：在你的项目上运行

先分清两个目录：

| 目录 | 决定什么 |
| --- | --- |
| `--workspace` 指向的目录 | Agent 读写的项目文件，必须已经存在 |
| 执行命令时所在的目录 | 用哪个 OC 安装（`uv run` 会找当前目录的项目环境） |

### 交互式启动

在 OC 源码根目录执行：

```bash
uv run opencollab --workspace /path/to/your-project
```

这会以你的项目为工作区，启动内置的 Self-Collaboration 团队。进入后输入一个范围清楚的任务，比如“查看项目入口并说明测试命令”。

- 会话从入口成员 Analyst 开始。它可以把实现交给 Coder；Coder 在独立工作树里改代码，Analyst 采纳改动后再安排 Tester 验证。
- 实际怎么交接，取决于模型真正发出的工具调用。只是了解项目的任务，Analyst 可能自己就做完了。
- 用 Tab 和 Shift+Tab 切换查看各成员的消息。更多操作见 [在终端里使用 OC](interactive-cli-controls.md)。

### 或者：一次性任务

只跑一轮就退出，用 `--prompt`。下面的 `--trace` 会记录轨迹：

```bash
uv run opencollab --workspace /path/to/your-project \
  --prompt '阅读项目入口和测试配置，说明如何运行最小验证。' \
  --trace
```

| 选项 | 作用 |
| --- | --- |
| `--prompt` | 直接在命令行给任务 |
| `--prompt-file /absolute/path/task.txt` | 从 UTF-8 文件读任务，适合长任务，最大 4 MiB，和 `--prompt` 互斥 |
| `--hold` | 任务结束后保留查看界面，可以切换成员，输入 `q` 或 `exit` 离开；必须配合前两项之一 |
| `--trace` | 记录轨迹 |

## 第四步：看结果和记录

- 终端会显示本次会话保存在哪里。团队运行自动保存在项目的 `.opencollab/sessions/` 下：每个成员一个 JSON 会话文件，另有一个 `team.json` 描述这次运行。
- 检查顺序：先看 Agent 的回答，再核对文件改动和实际的验证输出。回答里说“测试通过”不算数，要看到命令真的跑过。
- 想接着主会话继续对话，见 [保存与恢复会话](../operations/session-save-resume.md)。

## 再试一下 Workflow

Workflow 走单独的子命令，选项要写在子命令后面。先列出这个工作区能用的 Workflow，再看某一个的帮助：

```bash
uv run opencollab workflow list --workspace /path/to/your-project
uv run opencollab workflow run duo --help
```

下面运行内置的 Duo，并允许它直接在本机项目里执行 shell 命令（只在你信任的项目上这样做）：

```bash
uv run opencollab workflow run duo --workspace /path/to/your-project \
  --task '修复指定问题，并运行项目验证。' \
  --args '{"allow_unisolated_shell":true}'
```

`--task` 会写进 Workflow 输入的 `goal` 字段；如果 `--args` 里已经有 `goal`，就别再写 `--task`。需要进程隔离时，先准备好执行环境再运行。下一步见 [运行一个 Workflow](../workflow/run-registered-workflow.md)。

## 常见问题

- **装好了，换一个终端却找不到 `opencollab`**：那个终端不在同一个环境里。直接用 `.venv/bin/opencollab` 或 `uv run opencollab`。
- **切换了源码版本**：重新同步一次依赖，让环境跟上该版本的依赖声明。
- **要复现别人的结果**：用对方记录的发布版本和依赖，而不是当前提交。
- **需要 Weave、`RunControl` 或 `ctx.agent_run`**：这些在 `v0.9.3` 之后才加入，要选更新的版本。已发布的标签和安装包在仓库的发布页。
- **报 capability 错误**：本机文件适配器依赖几项系统能力：基于目录描述符的相对路径操作、不跟随符号链接的文件检查、基于文件描述符的目录枚举、`fcntl.flock` 文件锁，以及不覆盖已有文件的原子重命名（Linux 用 `renameat2(RENAME_NOREPLACE)`，macOS 10.12 及以上用 `renameatx_np(RENAME_EXCL)`）。缺任何一项都会报这个错，请换一个满足要求的系统。
- **成员自己写的代码要在容器或 VM 里执行**：控制端和容器或 VM 里都要装同一份源码构建出的 wheel，见 [在现有 Linux VM 中运行任务](../isolation/vm-environment-extension.md)。

## 延伸阅读

- 源码依据：[pyproject.toml](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/pyproject.toml) · [README.zh-CN.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/README.zh-CN.md) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md) · [opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/cli/main.py) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py)
- 相关指南：[在终端里使用 OC](interactive-cli-controls.md) · [从 Python 调用 OC](python-sdk-entrypoints.md) · [配置从哪来、哪个生效](../models/configuration-precedence.md)
