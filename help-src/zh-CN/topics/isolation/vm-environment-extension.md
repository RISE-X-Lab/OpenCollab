---
{
  "id": "vm-environment-extension",
  "title": "在现有 Linux VM 中运行任务",
  "category": "isolation",
  "summary": "准备 Linux 来宾与 SSH 连接，通过 VM 配置选择组隔离或会话隔离，并导出改动、恢复租约。",
  "aliases": [
    "虚拟机沙箱",
    "独立系统",
    "vm-group",
    "vm-session",
    "VMProfile",
    "VMConnection",
    "VMResource",
    "OPENCOLLAB_VM",
    "lease_database",
    "reset_command"
  ],
  "keywords": [
    "虚拟机",
    "VM 隔离",
    "SSH",
    "机器池",
    "容量不足",
    "来宾重置"
  ],
  "questions": [
    "怎样让 Team 或 Workflow 在虚拟机里运行？",
    "vm-group 和 vm-session 怎样分配机器？",
    "怎样让子 Workflow 继续使用父成员的 VM？",
    "VM 容量不足或重置失败后怎样恢复？"
  ],
  "related": [
    "isolation-overview",
    "isolation-configuration",
    "execution-environments",
    "workspace-recovery",
    "member-authored-workflow",
    "model-routing-request-capacity"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在现有 Linux VM 中运行任务

`vm-group` 和 `vm-session` 通过系统 OpenSSH 使用已经准备好的 Linux VM。控制端运行 Python 编排与模型请求，文件工具和命令进入来宾。项目内容会复制进分配的来宾工作区，结束时导出改动，再执行管理员提供的重置命令并归还租约。

## 先选择共享范围

六种内置策略分别适合不同的文件和系统共享需求。

| 策略 | 文件与机器的关系 |
| --- | --- |
| `shared` | 复用父环境和父目录 |
| `worktree` | 在父环境中创建独立工作树，保留父机器 |
| `docker-group` | 当前运行的策略池共用一个容器，各会话使用独立工作树 |
| `docker-session` | 各会话使用独立容器与工作目录 |
| `vm-group` | 当前运行的策略池共用一台 VM，各会话使用独立工作树 |
| `vm-session` | 各会话领取一台 VM，并在其中准备工作区 |

`group` 指每次运行内的环境池范围。成员启动的子 Workflow 会创建自己的环境池，继续选择 `vm-group` 时可能领取另一台来宾。要让子 Agent 留在父成员的 VM 中，子 Workflow 的 `launch` 应选择 `worktree`。单次子调用选择 `vm-session` 时，需要父 VM 之外的空闲机器。

同一台 VM 中的工作树共享内核、网络、系统依赖和公共临时目录。需要彼此独立的系统状态时，按会话分配 VM。网络访问范围由来宾及其基础设施配置。完整选择依据见 [隔离怎么选](isolation-overview.md)。

## 准备控制端与来宾

控制端需要系统 OpenSSH。来宾需要 Python 3.10 或更新版本、Git、Bash，以及与控制端相同的 OpenCollab 安装包。来宾 Linux 需要支持 child subreaper、pidfd，并让 SSH 账号完整查看操作后代进程的 `/proc`。这些能力用于在取消和断线恢复时定位并结束远端命令。

从固定源码提交构建一次 wheel，控制端和每台来宾安装这同一个文件。该提交的包版本仍为 `0.9.5`，安装来源由下面的完整提交确定。先在独立源码目录执行构建和控制端安装。

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git OpenCollab-vm
cd OpenCollab-vm
git checkout 0c47f74a27441888286a22012b23b658185f8b8f
uv build --wheel
python3 -m venv .venv
.venv/bin/python -m pip install dist/opencollab-0.9.5-py3-none-any.whl
source .venv/bin/activate
```

将生成的同一份 wheel 复制到每台来宾的 `/home/worker/opencollab-0.9.5-py3-none-any.whl`，再由来宾的 `worker` 账号执行安装。pip 会为来宾系统安装适用的运行依赖。

```bash
python3 -m venv /home/worker/.venvs/oc
/home/worker/.venvs/oc/bin/python -m pip install /home/worker/opencollab-0.9.5-py3-none-any.whl
```

后续控制端示例在要处理的项目目录中执行，并使用刚准备的 Python 环境。将来宾虚拟环境的 Python 绝对路径填写到 `connection.python`。成员自编 Workflow 的 `launch.python` 也应指向这个解释器。

先在每台来宾上准备工作区根目录和状态目录。以下命令由示例中的 SSH 账号 `worker` 在来宾中执行，实际路径按账号调整。

```bash
install -d -m 700 /home/worker/oc-workspaces
install -d -m 700 /home/worker/oc-state
```

`workspace_root` 必须已经存在并可写。`state_directory` 放在工作区之外，由 SSH 账号拥有，权限为 `0700`。远端辅助程序在其中保存运行和操作记录，用于重连与恢复。两者都是规范化的非根绝对路径。

SSH 使用非交互认证与严格主机密钥验证。提前核对来宾的主机密钥，并写入控制端已信任的 `known_hosts` 文件。`identity_file` 引用控制端已有私钥。连接使用 `ssh -F /dev/null`，主机名、端口、账号和解释器通过 VM 配置明确提供。可先按实际配置执行一次连接与命令，确认账号、Python 包和 Git 可用。

```bash
ssh -T -F /dev/null \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/controller/.ssh/known_hosts \
  -i /home/controller/.ssh/vm-key -o IdentitiesOnly=yes \
  -p 22 worker@vm-a.example.net \
  '/home/worker/.venvs/oc/bin/python -c "import opencollab; print(opencollab.__version__)" && git --version'
```

模型凭据通过控制端的进程环境或工作区之外的 env 文件加载。项目文件会按快照规则复制到 VM，项目内的 `configs/.env` 也可能随之进入来宾。将模型认证配置保存在控制端专属目录，并通过 `OPENCOLLAB_CONFIG_FILE` 指定其绝对路径，下面示例使用该方式。

```bash
export OPENCOLLAB_CONFIG_FILE=/home/controller/.config/opencollab/model.env
```

## 保存完整 VM 配置

将下列 JSON 保存为控制端项目中的 `vm-profile.json`。替换主机、账号、密钥、目录和重置程序路径。示例包含两台来宾，可以验证父成员占用一台 VM 时，子调用再领取另一台的情况。

```json
{
  "resources": [
    {
      "resource_id": "worker-01",
      "connection": {
        "host": "vm-a.example.net",
        "user": "worker",
        "port": 22,
        "identity_file": "/home/controller/.ssh/vm-key",
        "known_hosts_file": "/home/controller/.ssh/known_hosts",
        "python": "/home/worker/.venvs/oc/bin/python",
        "connect_timeout": 10
      },
      "workspace_root": "/home/worker/oc-workspaces",
      "state_directory": "/home/worker/oc-state",
      "reset_command": "/usr/local/sbin/reset-opencollab-guest"
    },
    {
      "resource_id": "worker-02",
      "connection": {
        "host": "vm-b.example.net",
        "user": "worker",
        "port": 22,
        "identity_file": "/home/controller/.ssh/vm-key",
        "known_hosts_file": "/home/controller/.ssh/known_hosts",
        "python": "/home/worker/.venvs/oc/bin/python",
        "connect_timeout": 10
      },
      "workspace_root": "/home/worker/oc-workspaces",
      "state_directory": "/home/worker/oc-state",
      "reset_command": "/usr/local/sbin/reset-opencollab-guest"
    }
  ],
  "lease_database": "/home/controller/oc-state/vm-leases.sqlite3",
  "wait_timeout": 0,
  "cleanup_timeout": 60
}
```

`resource_id` 稳定标识实际机器。同一台机器经由不同主机名或 SSH 连接访问时，所有配置继续使用同一个 ID。同一份资源列表中的 ID 必须唯一。

`lease_database` 是控制端的绝对本地 SQLite 路径。同一控制主机上共享机器池的各进程使用同一数据库文件，SQLite 事务和资源 ID 的唯一约束协调独占分配。多台控制主机需要共同分配 VM 时，通过 [自定义环境提供器](custom-environment-providers.md) 接入外部分配服务。

`wait_timeout` 默认为 `0`，容量不足会立即抛出 `VMCapacityError`。正数表示最多等待的秒数，JSON 中的 `null` 表示持续等待到容量可用或运行取消。父任务持有全部机器时，子调用继续等待也需要其他任务归还容量，可按实际最大并发量增加资源。

`cleanup_timeout` 默认为 `60` 秒，供 SDK Team、Workflow、CLI 和成员自编子 Workflow 使用。SDK 运行参数或子运行 `launch` 显式指定的值优先，Workflow CLI 也可用 `--cleanup-timeout` 覆盖。重置程序较慢时，按实测耗时增加这个等待时间。

每台资源的 `reset_command` 都要引用管理员已经准备并验证过的程序。上面的 `/usr/local/sbin/reset-opencollab-guest` 是需替换的管理员脚本路径。脚本承担工作负载允许产生的全部来宾状态恢复，包括依赖安装、后台服务和系统配置，并以成功退出表示恢复完成。工作区删除负责回收任务文件，整机状态由该重置程序恢复。

## 用 SDK 运行一次检查

先配置控制端的模型端点与凭据，方法见 [逻辑模型路由与共享请求容量](../models/model-routing-request-capacity.md)。创建 `workflows/vm_check.py`，保存下面的工作流。

```python
from opencollab import workflow
from opencollab.tools import builtin_tools

@workflow(name="vm-check")
async def vm_check(ctx, args):
    output = await ctx.agent(
        args.get("goal", "读取 README.md，执行 hostname 和 pwd，将观察结果写入 vm-result.txt。"),
        tools=builtin_tools("file_read", "file_write", "bash"),
    )
    return {"inspection": output}
```

在项目根目录保存并运行 `run_vm.py`。默认策略让这次 Agent 调用进入 `vm-group`。

```python
import asyncio
import json
from pathlib import Path
from opencollab import OpenCollab
from workflows.vm_check import vm_check

async def main():
    profile = json.loads(Path("vm-profile.json").read_text(encoding="utf-8"))
    client = OpenCollab(".", config={"vm": profile})
    result = await client.workflow(
        vm_check,
        {},
        isolation="vm-group",
        artifacts="vm-run",
    )
    print(result.raise_for_status().output)
    print(result.artifacts)

asyncio.run(main())
```

检查模型返回的主机名和目录，再查看 `vm-run` 下导出的 VM 工作区。项目本身需要构建或测试时，把真实命令写进任务，检查其退出码和输出。`config["vm"]` 会注册 `vm-group` 和 `vm-session` 两种策略。SDK 的 `environment_providers` 可以覆盖其中的同名工厂，适合已有资源服务的集成，公开工厂为 `vm_isolation_provider`。

## 用 CLI 或 Team 选择 VM

上述带装饰器的工作流可以直接按名称运行。CLI 从 `OPENCOLLAB_VM` 读取 JSON，模型配置沿用前面指定的外部 env 文件。

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" \
  opencollab workflow run vm-check --workspace . \
  --isolation vm-group --cleanup-timeout 60
```

Team 文件可以设置运行默认值，也可以为角色指定策略。将下面内容保存为 `team.yaml`。

```yaml
entry: inspector
isolation: vm-group
roles:
  inspector:
    prompt: Read the project and report the hostname, working directory, and test result.
    tools: [file_read, bash]
```

SDK 使用已配置 VM profile 的 `client` 调用 `await client.team("检查项目", config="team.yaml", artifacts="team-vm-run")`。交互式 Team CLI 使用同一份配置。

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" \
  opencollab --workspace . --team-config team.yaml
```

Team 的角色策略优先于 SDK 运行默认值，SDK 值优先于 Team 文件默认值。详细覆盖规则见 [默认隔离与单次覆盖怎么配](isolation-configuration.md)。

## 让成员自编子 Workflow 留在自己的 VM

给成员配置 `manage_workflow` 后，先调用 `{"action": "describe"}` 查看当前可用策略与工具权限，再写出子 Workflow 源文件。成员用下面的工具参数启动子运行，让子 Agent 在成员 VM 的独立工作树中执行。`python` 应与实际来宾安装位置一致。

```json
{
  "action": "launch",
  "source": "generated.py",
  "isolation": "worktree",
  "python": "/home/worker/.venvs/oc/bin/python"
}
```

子 Workflow 也可对单次 `ctx.agent` 设置 `isolation="vm-session"`，领取父 VM 之外的机器。创建者用 `status`、`wait`、`result` 或 `stop` 管理子运行，Team 成员通信继续遵循配置拓扑。完整编写过程见 [让成员编写并管理自己的子 Workflow](../workflow/member-authored-workflow.md)。

桥接层的模型路由、凭据与请求容量保存在控制端，工作区文件按快照规则复制，VM 配置决定工具运行位置。相同控制主机的模型请求可共用本地请求池，多台控制机器的全局额度交给外部网关或调度服务，配置见 [逻辑模型路由与共享请求容量](../models/model-routing-request-capacity.md)。

## 导出改动与恢复资源

环境释放前会把候选内容导出到控制端运行产物的 `vm/<租约 token>/<角色目录>` 下。显式隔离的 Team 入口补丁还可从 `result.metrics.get("entry_patch")` 取得。查看完整文件或补丁后，再按任务要求采纳到交付目录，具体见 [把候选结果采纳到交付目录](candidate-delivery.md)。

池会结束拥有的操作，清理工作树，再持有机器租约执行管理员重置程序。重置成功、任务目录回收完成后，租约才会释放。导出、连接、清理或重置失败会保留租约和恢复资料。保存错误中的资源 ID、远端路径、操作 ID 与控制端产物位置，再按 [找回隔离任务的改动并清理资源](workspace-recovery.md) 继续处理。

控制进程退出后，SQLite 中的租约继续保留。管理员确认对应远端操作已结束、改动已经导出且来宾成功重置后，可以移除该资源对应的确切租约记录。仍存活的池对象支持清理重试，重新创建池会继续识别已有租约。

仓库的 [双 VM 离线示例](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/vm-authored-workflow/demo.py) 使用 `python3` 启动成员源码。采用本页的来宾虚拟环境布局时，在示例的 `manage_workflow` 启动参数中加入 `"python": "/home/worker/.venvs/oc/bin/python"`，再使用同一份 VM profile 运行示例。

## 源码依据

[VM 使用指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/virtual-machines.zh-CN.md)、[VMProfile 配置](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/environment_profiles.py)、[VM 资源字段](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/vm_config.py)、[VM 分配与释放](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/vm_isolation.py)、[成员自编 VM 示例](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/vm-authored-workflow/README.zh-CN.md)。
