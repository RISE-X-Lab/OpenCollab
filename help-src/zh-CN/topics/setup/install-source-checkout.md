---
{
  "id": "install-source-checkout",
  "title": "安装 OpenCollab",
  "category": "setup",
  "summary": "下载源码、选好版本，装出一个能运行 OpenCollab 的 Python 环境。",
  "aliases": [
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
    "选定适合自己的发布版本或当前源码，准备能够启动 OpenCollab 的 Python 环境？",
    "OpenCollab 怎么安装？",
    "支持 Windows 吗？",
    "装好了但终端说找不到 opencollab 怎么办？"
  ],
  "related": [
    "first-run-workspace",
    "python-sdk-entrypoints",
    "configuration-diagnostics"
  ],
  "verified_ref": "0c47f74a"
}
---

# 安装 OpenCollab

OpenCollab 支持 Linux 和 macOS，需要 Python 3.10 及以上。从源码安装分三步：clone 并切到版本，用 uv 或 pip 装依赖，跑几条命令确认。

## 第一步：下载源码并选好版本

在你平时放代码的目录里执行：

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git
cd OpenCollab
# 切到这些指南所对应的源码提交
git checkout 0c47f74a27441888286a22012b23b658185f8b8f
# 确认 Python 版本，应为 3.10 或更高
python3 --version
```

如果你已经有这个仓库，进入仓库后切到目标标签或提交即可。后面所有安装命令都在仓库根目录执行。

该提交包含本 Wiki 介绍的 VM 隔离与 `manage_workflow describe`。源码包版本仍为 0.9.5，安装这些新功能时以提交为准。控制端与执行成员自编代码的容器或 VM 使用同一份源码构建的 wheel。切换源码版本后，请看该版本自带的文档和锁文件。

## 第二步：安装依赖

两种方式选一种，之后一直用同一种。

### 方式 A：用 uv（推荐已经装了 uv 的人）

`uv sync --locked` 会按仓库里的锁文件创建并同步项目环境。

```bash
uv sync --locked
uv run opencollab --help
```

如果你打算修改框架源码并运行开发检查，加上开发依赖：

```bash
uv sync --locked --extra dev
```

### 方式 B：用 Python 自带的虚拟环境和 pip

`-e` 是可编辑安装。之后你改了源码，这个环境里马上生效，适合从源码开发和调试。

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -c 'from opencollab import OpenCollab; print(OpenCollab.__name__)'
.venv/bin/opencollab --help
```

如果你用的是发布版 wheel：在你选好的虚拟环境里按该发布提供的方式安装，装完同样检查包版本和帮助信息。

## 第三步：检查装好没有

下面的命令会显示包版本，检查公开导入是否正常，并打开 CLI 帮助。这是 uv 的写法；用 pip 的话，把 `uv run python` 换成 `.venv/bin/python`。

```bash
uv run python -c 'from importlib.metadata import version; print(version("opencollab"))'
uv run python -c 'from opencollab import OpenCollab, RunResult; print(OpenCollab, RunResult)'
uv run opencollab workflow --help
```

三条命令都正常输出，就说明安装完成。下一步：在 `configs/.env` 里填好模型端点和凭据，选一个自己的项目作为工作区，完成第一次运行。

## 常见问题

如果安装显示成功，但另一个终端提示找不到 `opencollab`：

- 多半是那个终端没在同一个环境里。检查当前环境和可执行文件路径。
- 直接用 `.venv/bin/opencollab`，可以确保用的是这个环境里的入口。

如果切换了源码标签：

- 重新同步依赖，让环境跟上该版本的依赖声明。

如果要重现别人已有的程序：

- 使用它记录的发布版本和依赖。

如果你需要 Weave、`RunControl` 或 `WorkflowContext.agent_run`：

- 这些功能在 `v0.9.3` 之后才加入，要选更新的版本。
- 当前提交的 `pyproject.toml` 声明包版本为 `0.9.5`。已发布的标签和安装包可以在仓库的发布页找到。

如果出现 capability 错误：

- 说明你的系统缺少本地文件适配器需要的某些能力，请换一个符合要求的宿主环境。见下一节。

## 进阶：系统能力要求

本地文件适配器依赖以下系统能力：

- 基于目录描述符的相对路径操作。
- 不跟随符号链接的文件检查。
- 基于文件描述符的目录枚举。
- `fcntl.flock` 文件锁。
- 原子的、不覆盖已有文件的重命名。Linux 使用 `renameat2(RENAME_NOREPLACE)`，macOS 10.12 及以上使用 `renameatx_np(RENAME_EXCL)`。

缺少其中任何一项，都会出现 capability 错误。

## 来源

[pyproject.toml](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/pyproject.toml#L5)　[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L17)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L40)

## 相关指南

[在自己的项目中完成第一次运行](first-run-workspace.md)　[从 Python 调用 OpenCollab](python-sdk-entrypoints.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)
