---
{
  "id": "shell-permissions-execution",
  "title": "管理终端权限并确认命令结果",
  "category": "extensions",
  "summary": "bash 工具在 Agent 绑定的环境里执行；能否在宿主运行由 --yolo、--allow-local-child-shell、allow_unisolated_shell 和 headless 决定，结果要看退出状态和输出。",
  "aliases": [
    "风险命令",
    "自动批准",
    "命令超时",
    "输出截断",
    "--yolo",
    "allow_unisolated_shell",
    "require_process_isolation",
    "SandboxInterceptor",
    "timeout",
    "max_output_chars",
    "--allow-local-child-shell",
    "headless"
  ],
  "keywords": [
    "终端命令",
    "命令确认",
    "bash"
  ],
  "questions": [
    "让必要命令真正运行，理解确认提示、超时和截断，并据实际输出判断结果？",
    "bash 命令为什么被拒绝或要求确认？",
    "bash 工具默认超时多少？",
    "为什么上一条命令的 cd 没生效？"
  ],
  "related": [
    "isolation-overview",
    "file-editing-safe-workspace",
    "workspace-recovery",
    "tool-selection",
    "native-test-evidence"
  ],
  "verified_ref": "0c47f74a"
}
---

# 管理终端权限并确认命令结果

OC 的 `bash` 工具在 Agent 绑定的执行环境里运行命令：本地环境和本地工作树用宿主进程权限，Docker 环境在容器内执行。一条命令最终能不能跑、跑在哪里，由三层共同决定：执行环境、角色的工具授权、风险命令确认策略。OC 默认偏保守，例如 `builtin_tools(..., headless=True)` 会要求 shell 运行在有进程隔离的环境里，宿主 shell 需要显式开关。

## 最小调用

```json
{"command":"python -m pytest tests/test_parser.py -q","timeout":180}
```

| 参数 | 说明 |
| --- | --- |
| `command` | 要执行的命令 |
| `timeout` | 秒数，默认 120 |

`max_output_chars` 不是调用参数。它在构造工具时通过 `limits` 设置，见 [选择 Agent 能使用的工具](tool-selection.md)。

执行规则：

- 命令在非交互环境运行。带分页器或确认提示的命令，要加上对应的非交互选项，否则无法完成。
- 每次调用都从绑定的工作区开始。上一次的 `cd` 不会保留。需要进子目录时，在同一条命令里完成切换和执行，或者用绝对路径。

## 三个开关

| 设置 | 管理的行为 |
| --- | --- |
| CLI `--yolo` | 自动批准被判定为需要确认的风险命令 |
| CLI `--allow-local-child-shell` | 允许动态子成员在没有系统进程沙箱时执行宿主 shell |
| SDK 或工作流的 `allow_unisolated_shell` | 显式允许这次运行使用宿主 shell |

这些开关只放开权限，不会自动加工具。团队配置和工具集合里仍然要包含 `bash`。

`builtin_tools(..., headless=True)` 默认要求 shell 使用进程隔离环境。`headless=False` 允许对应的宿主执行方式。

还有一点容易忽略：Workflow 的 Python 编排代码本身跑在调用方进程里。业务代码里直接调用宿主文件或子进程操作时，不经过 `bash` 工具，要单独考虑影响范围。

## 怎样确认命令真的成功

1. 读工具返回的退出状态、标准输出、错误输出和超时标志。
2. 测试类命令，要确认预期的测试目标确实执行了。`--help` 的输出只说明用法，测试收集结果只说明有哪些待执行目标，都不代表通过。
3. 需要机器可读的测试证据时，使用 `evidence_tools` 和支持的 runner，见 [用实际测试验证 Agent 的交付](../observability/native-test-evidence.md)。

## 进阶：几种超时的区别

长命令要区分三种时限：命令自身的 `timeout`、Agent 的运行期限、模型请求超时。调大模型超时，无法延长一个已经被 `bash` 工具终止的进程。

## 中断与清理

- 取消运行时，框架会清理它自己创建的命令和环境。
- 调用方传入的环境，由调用方继续管理。
- 返回结果里的清理状态和恢复路径，可以帮你判断何时能安全复用目录或删除临时资源，见 [找回隔离任务的改动并清理资源](../isolation/workspace-recovery.md)。

## 常见问题：命令失败

先看原始错误，再按类型处理：

| 现象 | 通常的方向 |
| --- | --- |
| 找不到依赖 | 检查执行环境或镜像里是否安装 |
| 工作目录不对 | 记得每次调用都从工作区开始，`cd` 不保留 |
| 环境已被撤销 | 环境已释放，查看清理状态 |
| 权限被拒绝 | 检查上面的三个开关和 `headless` |
| 测试断言失败 | 这是真实的测试结果，按项目问题处理 |

保留原命令、退出状态和相关环境设置，比重新启动整个任务更容易定位原因。

VM 策略下，命令由配置的 SSH 账号在来宾执行，网络和系统权限由来宾环境决定。成员自编 DW 的 Python 子进程也位于成员的执行环境中，准备方式见 [VM 指南](../isolation/vm-environment-extension.md)。

## 源码与进一步阅读

[opencollab/adapters/tools/bash.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/bash.py#L43) · [opencollab/adapters/safety.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/safety.py#L21) · [opencollab/bootstrap/tool_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/tool_registry.py#L117) · [opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L361)

[隔离怎么选](../isolation/isolation-overview.md) · [安全地查找读取和修改文件](file-editing-safe-workspace.md) · [找回隔离任务的改动并清理资源](../isolation/workspace-recovery.md) · [选择 Agent 能使用的工具](tool-selection.md) · [用实际测试验证 Agent 的交付](../observability/native-test-evidence.md)
