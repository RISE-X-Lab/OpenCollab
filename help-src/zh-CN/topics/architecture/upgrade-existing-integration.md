---
{
  "id": "upgrade-existing-integration",
  "title": "升级 OC 后怎样迁移和验证",
  "category": "architecture",
  "summary": "先记下当前装的是哪个版本，再按迁移记录核对默认行为和接口变化，最后用原来的任务实际跑一遍。",
  "aliases": [
    "更新后报错",
    "旧代码",
    "旧配置",
    "旧教程",
    "release 和 main",
    "升级 SDK",
    "run_tests",
    "profile",
    "导入路径变了",
    "升级检查清单"
  ],
  "keywords": [
    "版本升级",
    "迁移指南",
    "CHANGELOG",
    "SemVer",
    "Base",
    "Single2",
    "public API",
    "team.yaml"
  ],
  "questions": [
    "从已有版本迁移到选定版本，核对默认行为和公开接口，并验证原有任务继续按预期运行？",
    "升级后默认 Agent 变了吗？",
    "旧配置里的 run_tests 怎么改？",
    "怎么确认服务实际用的是哪个版本？"
  ],
  "related": [
    "first-run-workspace",
    "python-sdk-entrypoints",
    "configure-team",
    "custom-tools",
    "extension-and-evaluation-boundaries"
  ],
  "verified_ref": "v0.9.6"
}
---

# 升级 OC 后怎样迁移和验证

升级分四步：记下现在装的是什么、按迁移记录找变化、核对配置和扩展、用原来的任务实际跑一遍。最容易踩的两处变化是：**0.8.0 起默认 Agent 改为 Base**（当前指向 Single2）；**0.7.0 起旧的 `run_tests` 工具改为用项目原生的 Bash 命令**。

选定发布 tag、附带的 wheel 或源码提交之后，阅读那个版本的包指南和迁移说明。

## 第一步：记下正在运行的安装

在启动 OC 的同一个 Python 环境里执行：

```bash
# 打印安装版本和实际导入位置
python -c 'from importlib.metadata import version; import opencollab; print(version("opencollab")); print(opencollab.__file__)'
# 源码安装：在对应仓库记录 Git 提交
git rev-parse HEAD
opencollab --help
```

CLI 和服务用的是不同的虚拟环境时，要分别检查，并确认服务进程实际导入的包位置和版本。从源码安装时，包版本号只在发布时更新，两次发布之间的提交都显示上一个版本号。所以源码安装要同时记下提交号（`git rev-parse HEAD`），见 [安装](../setup/first-run-workspace.md#第一步安装)。

## 第二步：沿迁移记录找到变化

1. 打开 [迁移索引](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/README.md)，它按 0.5、0.6、0.7、0.8 串起主要变化。
2. 先处理跨版本的接口变化。
3. 再看 [CHANGELOG](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/CHANGELOG.md) 里目标补丁版本的修复。
4. 当前的签名和默认值，以目标版本的包指南和 CLI 帮助为准。

### 默认 Agent 的变化（0.8.0）

| 你的写法 | 实际使用 |
| --- | --- |
| 省略 profile，或写 `base`、`default`、`single` | Base，当前指向 Single2，跟随这个映射变化 |
| 显式 `single2` 或 `agent2()` | 固定选择这个具体实现 |

比较历史实验时，同时保留源码版本和实际的 `metrics["agent_profile"]`。见 [Base、Single2 和 agent2() 的关系](../setup/python-sdk-entrypoints.md#basesingle2-和-agent2-的关系)。

### 工具列表的变化

- 测试操作现在用项目原生的 Bash 命令，旧配置里的 `run_tests` 要按 0.7.0 的迁移说明更新。
- 重新核对 Bash 的本机授权：SDK 默认要求进程隔离，只有明确允许在本机执行时，才用对应的授权或 `headless=False` 工具设置，见 [一条命令要过三道关](../extensions/tool-selection.md#一条命令要过三道关)。

## 第三步：核对配置和扩展

```bash
# 检查角色、工具和拓扑
opencollab team show --team-config team.yaml
# 列出工作区里的 Workflow
opencollab workflow list --workspace .
```

检查清单：

- 用 `team show` 找未知字段和旧工具名；团队文件必须显式选择。
- 角色额度需要完整覆盖所有角色，并和固定名册调用配合。
- 公开导入优先用 `opencollab`、`opencollab.tools`、`opencollab.environments` 和 `opencollab.workflows`。旧集成引用了内部路径时，对照目标版本找到公开入口。
- 自定义工具应保留参数 schema 和实际执行结果；环境提供器应继续负责申请、交付和清理。

先在小任务里做一次文件读取、修改或命令操作，再看结果和资源状态，确认扩展在新版本里正常工作。

## 第四步：用原任务验证

1. 选一个以前稳定能跑通的任务。
2. 用原来的 CLI 命令或 SDK 调用再跑一次，明确设置 token、步数和时间限制。
3. 核对最终交付文件和实际测试结果。
4. 服务有取消路径的话，触发一次取消，检查保存的材料、用量和所拥有资源的清理结果。

## 历史实验和 OCE

历史实验继续保留原来的版本信息，旧结果的模型、任务、提示词和限制也要一起保存。OCE 集成按它记录的框架依赖版本来理解，见 [OC 和 OCE 怎样分工](extension-and-evaluation-boundaries.md#oc-和-oce-怎样分工)。

## 延伸阅读

- 源码依据：[0.8.0 迁移](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/migrations/0.8.0.md) · [0.7.0 迁移](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/migrations/0.7.0.md) · [包指南](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/README.md) · [CHANGELOG](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/CHANGELOG.md)
- 相关指南：[安装并完成第一次运行](../setup/first-run-workspace.md) · [从 Python 调用 OC](../setup/python-sdk-entrypoints.md) · [Team 入门](../team/configure-team.md) · [自定义工具](../extensions/custom-tools.md)
