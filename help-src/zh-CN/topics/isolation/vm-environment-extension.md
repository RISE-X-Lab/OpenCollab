---
{
  "id": "vm-environment-extension",
  "title": "在现有 Linux VM 中运行任务",
  "category": "isolation",
  "summary": "把已有的 Linux VM 交给 Agent 用：准备控制端和来宾、写 VM 配置、选 vm-group 或 vm-session；运行结束后导出改动、重置并归还机器。",
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
    "来宾重置",
    "VMCapacityError",
    "manage_workflow"
  ],
  "questions": [
    "怎样让 Team 或 Workflow 在虚拟机里运行？",
    "vm-group 和 vm-session 怎样分配机器？",
    "怎样让子 Workflow 继续使用父成员的 VM？",
    "VM 容量不足或重置失败后怎样恢复？"
  ],
  "related": [
    "isolation-overview",
    "execution-environments",
    "candidate-delivery",
    "team-run-workflow",
    "model-routing-request-capacity"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在现有 Linux VM 中运行任务

OC 可以把 Agent 的文件工具和命令放进你已经准备好的 Linux VM 里执行，模型请求和编排逻辑仍在本机。适合这种情况：任务会装依赖、起后台服务、改系统配置，你不想让它们碰到本机，也不想让它们互相干扰。

## 先理解四个名词和一次运行的过程

- **控制端**：运行 OC 的那台机器，agent loop 和模型请求都在这里。
- **来宾**：被借用的 Linux VM。OC 通过系统 OpenSSH 连上它，在里面读写文件、执行命令。
- **租约**：OC 在控制端的 SQLite 文件里写一条记录，表示“这台 VM 现在归某次运行用”，相当于一把机器锁。
- **重置命令**：管理员在来宾上准备的脚本，OC 归还 VM 之前运行它，把来宾恢复干净。

一次运行的过程：

1. OC 领取一台空闲 VM，写入租约。
2. OC 把项目内容复制到这台 VM 的工作区。
3. Agent 在 VM 里读写文件、执行命令。
4. 运行结束，OC 把改动导出到控制端。
5. OC 运行重置命令，删掉任务目录，再删除租约，VM 回到池里。

最小用法：写好 `vm-profile.json`，然后

```python
client = OpenCollab(".", config={"vm": profile})
await client.workflow(flow, inputs, isolation="vm-group")
```

下面按顺序讲怎么准备。

## 先选 vm-group 还是 vm-session

两种 VM 策略的区别在于几个会话共用一台机器，还是每个会话一台：

- 几个会话只要文件分开、可以共用依赖和服务：用 `vm-group`。同一台 VM 里的各个工作树共用内核、网络、系统依赖和公共临时目录。
- 会话之间的系统状态必须分开：用 `vm-session`。
- 网络能访问哪里，由来宾和它所在的基础设施决定，OC 不管。

`group` 的范围是“一次运行里的一个环境池”。成员启动自己写的子 Workflow 时，子运行有自己的环境池：子运行再选 `vm-group` 可能领到另一台 VM；想让子 Agent 留在成员当前的 VM 里，子运行要选 `worktree`；子调用选 `vm-session` 时，池里要有父 VM 之外的空闲机器。和其他策略的对比见 [隔离](isolation-overview.md#六种内置策略)。

## 第一步：准备控制端和来宾

### 环境要求

| 机器 | 需要 |
| --- | --- |
| 控制端 | 系统 OpenSSH |
| 来宾 | Python 3.10 或更新版本、Git、Bash、和控制端相同的 OpenCollab 安装包 |
| 来宾内核 | 支持 child subreaper 和 pidfd，SSH 账号能完整看到操作后代进程的 `/proc` |

内核那一行的用途：任务取消或 SSH 断线后，OC 要靠这些能力找到远端命令并结束它。

### 在控制端和来宾上安装同一个 wheel

控制端和每台来宾必须装同一份安装包。做法是从固定的源码提交构建一次 wheel，到处安装这一个文件。这个提交的包版本号仍是 `0.9.5`，真正区分来源的是完整提交号。

先在一个独立的源码目录里构建，并装到控制端：

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git OpenCollab-vm
cd OpenCollab-vm
git checkout 0c47f74a27441888286a22012b23b658185f8b8f
uv build --wheel
python3 -m venv .venv
.venv/bin/python -m pip install dist/opencollab-0.9.5-py3-none-any.whl
source .venv/bin/activate
```

把这份 wheel 复制到每台来宾的 `/home/worker/opencollab-0.9.5-py3-none-any.whl`，用来宾上的 `worker` 账号安装，pip 会按来宾系统装好运行依赖：

```bash
python3 -m venv /home/worker/.venvs/oc
/home/worker/.venvs/oc/bin/python -m pip install /home/worker/opencollab-0.9.5-py3-none-any.whl
```

记下来宾虚拟环境里 Python 的绝对路径，后面要用两次：填到配置的 `connection.python`，以及成员自编子 Workflow 的 `launch.python`。后面的控制端示例都在你要处理的项目目录里执行，用的是刚才激活的 Python 环境。

### 在来宾上建两个目录

用 SSH 账号 `worker` 在每台来宾上执行（路径按你的账号调整）：

```bash
install -d -m 700 /home/worker/oc-workspaces
install -d -m 700 /home/worker/oc-state
```

- `workspace_root`：放任务工作区，必须已经存在并且可写。
- `state_directory`：远端辅助程序在这里保存运行记录和操作记录，断线后靠它重连和恢复。要放在工作区之外，归 SSH 账号所有，权限 `0700`。

两个路径都要写成规范化的绝对路径，不能是 `/`。

### 检查 SSH

OC 只用非交互认证，并严格校验主机密钥：

1. 提前核对来宾的主机密钥，写进控制端信任的 `known_hosts` 文件。
2. `identity_file` 指向控制端上已有的私钥。私钥只由控制端的 OpenSSH 使用，不会复制到来宾。
3. OC 连接时用 `ssh -F /dev/null`，不读你的 SSH 配置文件。主机名、端口、账号、解释器都要在 VM 配置里写明。

按实际配置手动连一次，确认账号、Python 包和 Git 都能用：

```bash
ssh -T -F /dev/null \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/controller/.ssh/known_hosts \
  -i /home/controller/.ssh/vm-key -o IdentitiesOnly=yes \
  -p 22 worker@vm-a.example.net \
  '/home/worker/.venvs/oc/bin/python -c "import opencollab; print(opencollab.__version__)" && git --version'
```

### 别让模型凭据进 VM

OC 会按快照规则把项目文件复制到 VM，项目里的 `configs/.env` 也可能被一起复制过去。所以模型凭据不要放在项目里。推荐做法：把模型认证配置放在控制端的专属目录，用 `OPENCOLLAB_CONFIG_FILE` 指定它的绝对路径，后面的示例都这样做：

```bash
export OPENCOLLAB_CONFIG_FILE=/home/controller/.config/opencollab/model.env
```

## 第二步：写 VM 配置

把下面的 JSON 存成控制端项目里的 `vm-profile.json`，把主机、账号、密钥、目录和重置程序路径换成你的。示例有两台来宾，这样可以验证“父成员占一台，子调用再领另一台”。

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

| 字段 | 含义 | 默认值 |
| --- | --- | --- |
| `resource_id` | 一台实际机器的稳定 ID | 必填 |
| `lease_database` | 控制端本地 SQLite 文件的绝对路径，存租约 | 必填 |
| `wait_timeout` | 没有空闲 VM 时最多等多少秒 | `0` |
| `cleanup_timeout` | 运行结束后等清理多少秒 | `60` |
| `reset_command` | 归还 VM 之前运行的管理员程序 | VM 池要求每台都写 |

**`resource_id`**：一个 ID 对应一台真实机器。同一台机器即使用不同的主机名或 SSH 连接访问，所有配置里也要用同一个 ID；同一份资源列表里 ID 不能重复。

**`lease_database`**：同一台控制主机上，所有共用这个机器池的进程都要指向同一个数据库文件。OC 用 SQLite 事务和资源 ID 的唯一约束，保证一台 VM 同时只分给一方。多台控制主机要共同分配 VM 时，一个本地 SQLite 文件就不够了，要用 [自定义环境提供器](execution-environments.md#方式二注册一个新的隔离策略) 接入外部分配服务。

**`wait_timeout`**：`0`（默认）表示没有空闲机器就立刻抛出 `VMCapacityError`；正数表示最多等这么多秒；JSON 里写 `null` 表示一直等，直到有机器空出来或运行被取消。注意：如果父任务已经占了全部机器，子调用再怎么等也要靠别的任务归还，所以按实际最大并发量多准备几台。

**`cleanup_timeout`**：SDK 的 Team 和 Workflow、CLI、成员自编子 Workflow 都用这个值。SDK 运行参数或子运行 `launch` 里显式写的值优先；Workflow CLI 可以用 `--cleanup-timeout` 覆盖；都没写时用配置里的值，默认 `60` 秒。重置程序跑得慢，就按实测耗时调大。

**`reset_command`**：要指向管理员准备好、验证过的程序，示例里的路径只是占位。OC 自己只删除任务目录，这只能回收任务文件；依赖安装、后台服务、系统配置这些整机状态，要由这个脚本恢复。工作负载可能改到的来宾状态，脚本都要负责还原。脚本成功退出，OC 才认为恢复完成。

## 第三步：用 SDK 跑一次检查

先在控制端配好模型端点和凭据，见 [接入模型服务](../models/provider-endpoint-credentials.md)。然后创建 `workflows/vm_check.py`：

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

在项目根目录创建并运行 `run_vm.py`。它把这次运行的默认策略设成 `vm-group`，Agent 调用会进入 VM：

```python
import asyncio
import json
from pathlib import Path
from opencollab import OpenCollab
from workflows.vm_check import vm_check

async def main():
    profile = json.loads(Path("vm-profile.json").read_text(encoding="utf-8"))
    client = OpenCollab(".", config={"vm": profile})  # 注册 vm-group 和 vm-session
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

跑完检查三件事：模型报告的主机名和目录是 VM 的；`vm-run` 下有导出的 VM 工作区；项目要构建或测试的话，把真实命令写进任务，看它的退出码和输出。

`config["vm"]` 会同时注册 `vm-group` 和 `vm-session`。如果你已有资源服务，可以在 SDK 的 `environment_providers` 里注册同名工厂覆盖它们，公开工厂是 `vm_isolation_provider`，见 [使用现有 VM 提供器](execution-environments.md#使用现有-vm-提供器)。

## 用 CLI 或 Team 选择 VM

带装饰器的 Workflow 可以直接按名字运行。CLI 从环境变量 `OPENCOLLAB_VM` 读 JSON，模型配置还是用前面指定的外部 env 文件：

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" \
  opencollab workflow run vm-check --workspace . \
  --isolation vm-group --cleanup-timeout 60
```

团队文件可以设整次运行的默认策略，也可以给单个角色设。把下面内容存成 `team.yaml`：

```yaml
entry: inspector
isolation: vm-group
roles:
  inspector:
    prompt: Read the project and report the hostname, working directory, and test result.
    tools: [file_read, bash]
```

用 SDK 跑：拿上面配好 VM 的 `client`，调用 `await client.team("检查项目", config="team.yaml", artifacts="team-vm-run")`。用交互式 Team CLI 跑：

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" \
  opencollab --workspace . --team-config team.yaml
```

优先级从高到低：角色上的策略、SDK 运行参数、团队文件的默认值，见 [在哪里设置，谁优先](isolation-overview.md#在哪里设置谁优先)。

## 让成员的子 Workflow 留在自己的 VM

成员 Agent 可以用 `manage_workflow` 写出并启动自己的子 Workflow：先调用 `{"action": "describe"}` 看能用哪些策略和工具，写出源文件，再用下面的参数启动。子 Agent 会在成员 VM 里的独立工作树中执行，`python` 要和来宾上的实际安装位置一致：

```json
{
  "action": "launch",
  "source": "generated.py",
  "isolation": "worktree",
  "python": "/home/worker/.venvs/oc/bin/python"
}
```

之后用 `status`、`wait`、`result` 或 `stop` 管理子运行。子 Workflow 里也可以给单次 `ctx.agent` 写 `isolation="vm-session"`，这次调用会领取父 VM 之外的另一台机器。模型路由、凭据和请求容量都留在控制端，由桥接层管理；工作区文件按快照规则复制到 VM。完整过程见 [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md#在-vm-成员里用独立工作树)。

仓库有一个 [双 VM 离线示例](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/vm-authored-workflow/demo.py)。它默认用 `python3` 启动成员写的源码；如果你按本页的方式在来宾上建了虚拟环境，就在示例的 `manage_workflow` 启动参数里加上 `"python": "/home/worker/.venvs/oc/bin/python"`，再用同一份 VM 配置运行。

## 导出改动和归还机器

**改动在哪里**：VM 环境释放前，OC 把候选内容导出到控制端运行产物的 `vm/<租约 token>/<角色目录>` 下。显式设置了隔离的 Team，入口补丁还可以从 `result.metrics.get("entry_patch")` 拿到。先看完整文件或补丁，再按任务要求采纳，见 [隔离改动怎样回到项目](candidate-delivery.md)。

**OC 怎样归还机器**：结束自己发起的操作 → 清理工作树 → 仍然持有租约，运行管理员的重置程序 → 重置成功、任务目录删掉之后，才释放租约。

**归还失败怎么办**：

- 导出、连接、清理或重置失败时，OC 会保留租约和恢复资料。记下错误里的资源 ID、远端路径、操作 ID 和控制端产物位置。
- 池对象还活着：对同一个对象再调用一次清理即可重试。
- 控制进程已经退出：SQLite 里的租约仍然保留，重新创建池时 OC 会认出已有租约。管理员确认三件事（远端操作已经结束、改动已经导出、来宾已经成功重置）后，可以删掉这台资源对应的那一条租约记录，步骤见 [VM 断线与租约恢复](candidate-delivery.md#vm-断线与租约恢复)。
- 报 `VMCapacityError`：没有空闲 VM。等别的任务归还，调大 `wait_timeout`，或增加资源。

## 延伸阅读

- 源码依据：[VM 使用指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/virtual-machines.zh-CN.md) · [VMProfile 配置](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/environment_profiles.py) · [VM 资源字段](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/vm_config.py) · [VM 分配与释放](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/vm_isolation.py) · [成员自编 VM 示例](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/vm-authored-workflow/README.zh-CN.md)
- 相关指南：[隔离](isolation-overview.md) · [使用自己的执行环境](execution-environments.md) · [隔离改动怎样回到项目](candidate-delivery.md) · [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md)
