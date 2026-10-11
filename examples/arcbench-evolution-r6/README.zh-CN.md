# ARC-Bench r6 Weave 多实例协作工作流

这个示例把 r6 比赛 Harness 接入 OC 内置 Weave（`weave`）工作流。核心工作流安排多个 Single2 实例串行接续同一个 Web 应用，每组需求和每轮修复使用独立对话，通过项目文件、需求卡、进度与检查报告交接。

赛事适配层读取公开 YAML，生成需求卡和共享资源提示，并向 `run_weave` 提供原提示、工具、浏览器检查和修复证据。核心工作流安排分组顺序、分配执行额度、根据实际进展扩展当前会话软额度，并控制修复轮次。每组实现后检查当前功能和可能受影响的旧功能，最后执行完整检查与稳定性复查。平台运行库、模型兼容包装和 `.arc` 报告留在赛事示例中。GitHub 协作应用和 Sheet 表格应用的检查器、历史反馈适配及材料归属见 [SOURCES.md](SOURCES.md)。

## 生成赛事提交包

把这个 example 目录当作一个独立的参赛小项目。打包后，它现有的 `main.py` 就在 ZIP 根目录，赛事平台直接执行这个文件。通用 OC 代码会从同一个 Git 提交构建成 wheel 安装包，放进提交包的 `wheels/`。这样 example 负责赛事适配，wheel 提供对应版本的 OC 和 Weave。

打包电脑需要 Python 3.10 以上、Git、tar 和 `uv`，并能访问 GitHub 与 Python 包下载源。macOS、Linux 或 Windows 的 WSL2 都可以执行下面的流程。打包阶段读取源码并构建 Python wheel，Node、Chromium、赛题与模型凭据在平台实际运行时准备。

### 准备打包工具

先查看电脑已有的工具。

```bash
python3 --version
git --version
tar --version
```

Python 显示 3.10 或更高版本，另外两条命令显示版本信息时，可以继续创建打包环境。缺少工具时，macOS 可通过已安装的 [Homebrew](https://brew.sh/) 安装。

```bash
brew install python@3.12 git
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$HOME/.venvs/oc-package"
```

Ubuntu 22.04 及更新版本，或者对应的 WSL2 Ubuntu，可执行下面的命令。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git tar
python3 -m venv "$HOME/.venvs/oc-package"
```

工具已经齐全时，直接创建环境即可。

```bash
python3 -m venv "$HOME/.venvs/oc-package"
```

上述三种方式任选对应的一种。随后激活环境并安装打包工具，每次打开新终端时重新执行激活命令。

```bash
source "$HOME/.venvs/oc-package/bin/activate"
python -m pip install uv
python --version
uv --version
```

### 获取仓库并生成 ZIP

首次获取仓库时执行下面的命令。已有仓库时，在对应 Git 工作区中进入包含打包脚本的已提交版本。

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git
cd OpenCollab
```

在这个仓库根目录执行打包命令。输出路径选择一个尚不存在的文件，重复打包时可以换一个文件名。

```bash
python examples/arcbench-evolution-r6/build_submission.py \
  --output /tmp/weave-submission.zip
```

脚本读取已提交的 `HEAD`。需要指定其他已提交版本时，增加 `--ref <提交号或标签>`。脚本导出该版本的 example，构建同一版本的 OC wheel，生成依赖文件 `requirements.txt` 和版本记录 `SUBMISSION.json`，然后打成 ZIP 并逐文件回读核对。构建时可能下载构建依赖。导出范围是 Git 中已提交的文件，本地凭据、虚拟环境和未跟踪的运行产物留在本地。

解压到新目录后，根目录直接包含 `main.py`、`requirements.txt`、`wheels/`、`platform/`、`arcbench_r6/`、`template/` 和 `skills/`。将这个 ZIP 按原来的赛事提交流程上传。赛事平台从解压目录安装并启动。

```bash
python -m pip install -r requirements.txt
python main.py "$TASK_DIR" --output-dir "$APP_WORKSPACE" --type web
```

平台提供任务目录、应用工作区，以及 `MODEL`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。安装过程可能下载 Python 运行依赖。下面说明的 Linux/WSL2、Node、应用依赖和浏览器准备要求仍然适用。打包脚本在 OC 的 Git 工作区运行，导出的 `main.py` 在解压后的参赛小项目中直接运行。

## 安装与启动

在 Linux 或 WSL2 环境准备 Python 3.10 以上、Node 22.12 以上、Git，以及赛题要求的应用依赖和 Chromium。比赛工作区需要已有的前后端源码、数据库与对应需求 YAML。

从 OC 仓库根目录安装当前 SDK 和随包的平台事件库。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install ./examples/arcbench-evolution-r6/platform/arcbench-agent-runtime
```

通过环境变量指定比赛模型、OpenAI 兼容端点和已有凭据。`MODEL` 或 `OPENCOLLAB_MODEL` 必须明确提供。`OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 也支持相应的 `OPENCOLLAB_` 名称。

```bash
python examples/arcbench-evolution-r6/main.py /absolute/path/to/requirements \
  --output-dir /absolute/path/to/application-copy --type web
```

目录参数依次查找 `requirements.yaml`、`requirements.yml` 和 `task.yaml`，也可以直接传文件。平台可以通过 `ARCBENCH_TASK_DIR`、`ARCBENCH_OUTPUT_DIR`、`ARCBENCH_TASK_TYPE` 提供参数。工作区中的前后端 `package.json` 均缺失时，入口补入通用模板的缺失文件。完整赛题的继承应用与数据由调用方提供。

## 运行设置

| 设置 | 默认值 |
| --- | --- |
| 累计 token 额度 | 1600 万 |
| 主阶段软额度和硬额度 | 1200 万与 1400 万 |
| 最终修复预留 | 200 万 |
| 主阶段累计步骤 | 200 |
| 总调度时间与编码阶段配置 | 100 分钟与 80 分钟 |
| 单次输出与上下文窗口 | 32768 与 1000000 |
| 最终修复 | 最多三轮，每轮最多 60 步、600 秒、200 万 token |

实际分组时间保留原 r6 的检查与清理预留。连续两轮缺少有效进展时结束修复。软额度扩展沿用当前实例的会话，并受工作流实际授权约束。比赛入口选择显式额度模式，避免其它任务的无限环境设置改变比赛配置。

`ARC_MAIN_BUDGET`、`ARC_MAIN_HARD_BUDGET`、`ARC_REPAIR_RESERVE`、`ARC_WALL_LIMIT_MIN`、`ARC_AGENT_LIMIT_MIN` 和 `OPENCOLLAB_MAX_STEPS` 可调整原有配置。`ARC_HISTORY_TRIGGER_TOKENS` 默认关闭。原生工作流采用比赛默认的分组路线，`ARC_GROUPED_MAIN=0` 会返回明确的配置错误。

## 结果与接续

预检会在实际模型工作区准备依赖和生成源码。正式检查在独立副本中执行未缓存的安装、构建与验证，保留原候选的源码和数据库。多个 reaction 场景各用独立的迁移后数据库副本，并复用一次安装和前端构建，计数与重启持久化断言保持。

当前结果保存在应用工作区的 `.arc/checks/outcome.json`。`delivery_ok` 表示本地完整检查是否通过，`official_score` 由赛事平台提供。SDK 外层执行完成与应用检查通过分别表达，CLI 在本次交付成功时返回零。

用 `--resume` 接续同一运行，入口读取原 run ID、需求文档、数据快照、阶段记录和已返回用量。已生成但尚未检查的组继续检查，已完成组保留结果。进程中断的阶段保留候选和交接信息，在剩余额度内接续。已经开始的修复轮次计入原三轮限制。

`--fresh` 明确声明新的输入应用，原报告归入 `.arc/history/`。同一个应用工作区由一个运行持有，取消时等待受管理的生成、检查与预检清理，随后形成当前运行的取消结果。

## 验证与材料

检查组件的独立入口、报告字段和浏览器夹具说明见 [CHECKS.md](CHECKS.md)。示例测试包含实际 SDK 会话、本地 HTTP 回放、浏览器操作、SQLite 重启以及取消后的真实进程清理。模型生成的测试响应由本地替身提供。

比赛原需求、继承应用与原始成绩回执配齐后，可以用同一入口执行正式复现。当前仓库提供完整工作流、比赛兼容入口、检查与交付组件，以及对应的运行测试。
