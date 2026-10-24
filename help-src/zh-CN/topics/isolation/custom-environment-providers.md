---
{
  "id": "custom-environment-providers",
  "title": "接入自己的执行环境",
  "category": "isolation",
  "summary": "注册一个自己的隔离策略名，让 Agent 跑在你提供的环境里，并负责好分配和清理。",
  "aliases": [
    "custom",
    "EnvironmentPool",
    "Environment",
    "acquire",
    "release_env",
    "release",
    "adopt"
  ],
  "keywords": [
    "自定义环境",
    "环境提供器",
    "environment_providers",
    "自定义隔离"
  ],
  "questions": [
    "使用已有环境提供器扩展隔离策略并让资源分配、交付和清理保持一致？",
    "怎么让 isolation 用我自己的环境？",
    "提示策略需要 environment provider 是什么意思？",
    "自己写环境池要实现哪些方法？"
  ],
  "related": [
    "execution-environments",
    "workspace-recovery",
    "vm-environment-extension",
    "isolation-configuration"
  ],
  "verified_ref": "0c47f74a"
}
---

# 接入自己的执行环境

内置策略不够用时（例如自有容器配置或远程执行服务），可以通过 `OpenCollab(..., environment_providers={"名字": 工厂})` 注册新的策略名，之后 `isolation="名字"` 就会使用它。OC 仍负责申请环境、交付改动和清理的调度，你的提供器负责实际资源。

OC 名词：

- 环境提供器（environment provider）：一个工厂函数，每次运行时创建一个环境池。
- 环境池（environment pool）：负责给每个会话发放环境、回收环境。
- 环境（Environment）：OC 工具实际读写文件、执行命令的对象。

## 最小示例：把 Docker 提供器包一层

通过 `environment_providers` 注册：键名就是新的策略名，值是工厂。下面把现成的 Docker 提供器注册成 `custom`，镜像、网络和隔离能力都来自这份 Docker 配置。

```python
from opencollab import OpenCollab
from opencollab.environments import DockerOptions, docker_isolation_provider

container_factory = docker_isolation_provider(
    DockerOptions(image="python:3.11", network="none"), scope="session"
)

# 工厂接收运行信息，返回一个环境池
def project_environment_factory(
    *, workspace, base_environment, owned_save_dir,
    enable_adoption, owned_output_paths,
):
    return container_factory(
        workspace=workspace,
        base_environment=base_environment,
        owned_save_dir=owned_save_dir,
        enable_adoption=enable_adoption,
        owned_output_paths=owned_output_paths,
    )

client = OpenCollab(".", environment_providers={
    "custom": project_environment_factory,
})
```

使用：

- Workflow：`await client.workflow(flow, inputs, isolation="custom")`。`flow` 是你自己的异步工作流，`inputs` 是它的输入字典。
- Team：在团队文件或角色里写 `isolation: custom`。

策略名要合法，并且和注册的键一致。

## 使用现有 VM 提供器

已有 Linux VM 可以使用内置 `vm_isolation_provider`。下面的工厂与 Docker 工厂接受同一组运行参数，可按需要注册策略名。连接、目录和管理员重置程序应先按 [VM 指南](vm-environment-extension.md) 准备。

```python
from opencollab import OpenCollab
from opencollab.environments import VMConnection, VMResource, vm_isolation_provider

resources = [VMResource(
    resource_id="worker-01",
    connection=VMConnection(
        host="vm-a.example.net", user="worker",
        python="/home/worker/.venvs/oc/bin/python",
        identity_file="/home/controller/.ssh/vm-key",
        known_hosts_file="/home/controller/.ssh/known_hosts",
    ),
    workspace_root="/home/worker/oc-workspaces",
    state_directory="/home/worker/oc-state",
    reset_command="/usr/local/sbin/reset-opencollab-guest",
)]
client = OpenCollab(".", environment_providers={
    "vm-group": vm_isolation_provider(
        resources,
        lease_database="/home/controller/oc-state/vm-leases.sqlite3",
        scope="group",
        wait_timeout=0,
    ),
})
```

之后调用 `await client.workflow(flow, inputs, isolation="vm-group", cleanup_timeout=60)`。显式提供器模式由运行参数设置清理等待时间。需要通过 SDK、CLI 和 Team 共用可序列化配置时，使用 `config["vm"]` 或 `OPENCOLLAB_VM`，其中 `cleanup_timeout` 默认是 `60` 秒。

同一控制主机的进程通过同一个本地 SQLite 文件协调 VM 租约。多个控制主机共用机器池时，可以把外部分配服务接成自己的 EnvironmentPool，由服务维护跨机器分配与归还。稳定资源 ID 应与实际机器对应。

## 工厂收到的参数

| 参数 | 含义 |
| --- | --- |
| `workspace` | 调用方项目的位置 |
| `base_environment` | 真实的来源环境 |
| `owned_save_dir` | 当前运行的产物目录 |
| `owned_output_paths` | 帮环境区分“运行自己写出的记录”和“任务源内容” |
| `enable_adoption` | 运行是否需要采纳能力，会影响候选准备和交付方式 |

## 自己写环境池要实现什么

环境池要实现公开接口 `opencollab.environments.EnvironmentPool`：

| 方法 | 作用 |
| --- | --- |
| `acquire(role, source=...)` | 接收角色和实际父环境或审阅源，返回已经能执行工具的 Environment |
| `adopt(patch)` | 把改动应用到交付工作区 |
| `release_env(environment)` | 归还一次分配 |
| `release()` | 结束池拥有的全部资源 |

Environment 需要提供：准备、文件读写、命令执行、撤销、停止、清理。详细签名见[公开端口](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/ports.py)。

几条要注意的规则：

- 审阅源可能来自另一种策略，也可能在容器里。要通过环境操作读取它的内容。如果直接从宿主机同名目录复制，审阅任务会读到错误版本。
- 传进来的源环境是借来的。释放时只释放池自己分配的资源。
- 想清楚环境对象和底层资源的关系。例如一个环境对象代表一份会话租约，底层容器由池统一管理。多个环境引用同一个资源时，归还其中一个，资源还要留到其他使用者都结束。审阅和清理都要按这个关系来。

## 接入新后端时怎样测试

1. 在一个环境里写入一个文件，再读回来。
2. 执行一条短命令，看退出码。
3. 申请两份环境，分别写入同名文件，确认内容互不影响。
4. 实际试一下文件写入、命令取消和导出失败后的恢复，确认资源能被正确清理。

## 进阶：取消和失败

- 取消可能发生在申请资源的途中。提供器要跟踪还没结束的申请，等它有了明确结果再完成撤销。
- 补丁导出失败时，保留可恢复的文件和资源信息，并让同一个池能再次清理。

## 写给使用者的说明

为你的提供器写清楚三件事：

1. 文件共享到什么程度。
2. 进程和网络怎么隔离。
3. 权限有哪些限制。

## 常见问题

- 如果提示该策略需要 environment provider：说明这个策略名还没注册。检查 `environment_providers` 的键和 `isolation` 写的名字是否一致。
内置 VM 提供器已经实现机器租约、项目复制、内容导出与归还重置，操作步骤见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。接入其他远端服务时，按服务的资源所有权完成对应生命周期。

## 参考

环境池的组合方式见[策略池实现](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/session_environments.py)。已有环境由谁负责清理，见 [让任务在指定目录或现有容器里执行](execution-environments.md)。
