---
{
  "id": "upgrade-existing-integration",
  "title": "升级 OpenCollab 后怎样迁移和验证",
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
    "install-source-checkout",
    "base-single2",
    "configuration-precedence",
    "configure-team",
    "custom-tools",
    "evaluation-boundary"
  ],
  "verified_ref": "0c47f74a"
}
---

# 升级 OpenCollab 后怎样迁移和验证

升级 OC 分三步：记录当前安装、按迁移记录核对变化、用原有任务实际验证。最容易踩坑的两处变化是：0.8.0 起默认 Agent 改为 Base（当前映射到 Single2）；0.7.0 起旧的 `run_tests` 工具改为用项目原生 Bash 命令。选定发布 tag、附带的 wheel 或源码提交后，阅读那个版本的包指南和迁移说明。

## 第一步：记录正在运行的安装

在启动 OpenCollab 的同一个 Python 环境里执行：

```bash
# 打印安装版本和实际导入位置
python -c 'from importlib.metadata import version; import opencollab; print(version("opencollab")); print(opencollab.__file__)'
# 源码安装：在对应仓库记录 Git 提交
git rev-parse HEAD
opencollab --help
```

如果 CLI 和服务用的是不同的虚拟环境，要分别检查。还要确认服务进程实际导入的包位置和版本。源码安装见 [安装 OpenCollab](../setup/install-source-checkout.md)。

## 第二步：沿迁移记录找到变化

1. 打开[迁移索引](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/README.md)，它按 0.5、0.6、0.7、0.8 串起主要变化。
2. 先处理跨版本的接口变化。
3. 再看 [CHANGELOG](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/CHANGELOG.md) 里目标补丁版本的修复。
4. 当前签名和默认值，以目标版本的包指南和 CLI 帮助为准。

### 默认 Agent 的变化（0.8.0）

| 你的写法 | 实际使用 |
| --- | --- |
| 省略 profile，或 `base`、`default`、`single` | Base，当前映射到 Single2，跟随这个映射变化 |
| 显式 `single2` 或 `agent2()` | 固定选择这个具体实现 |

比较历史实验时，同时保留源码版本和实际的 `metrics["agent_profile"]`。

### 工具列表的变化

- 测试操作现在用项目原生 Bash 命令。旧配置里的 `run_tests` 需要按 0.7.0 迁移说明更新。
- 重新核对 Bash 的宿主授权。SDK 默认要求进程隔离。只有明确允许在宿主执行时，才使用对应授权或 `headless=False` 工具设置。

## 第三步：核对配置和扩展

```bash
# 检查角色、工具和拓扑
opencollab team show --team-config team.yaml
# 列出工作区里的 Workflow
opencollab workflow list --workspace .
```

检查清单：

- 用 `team show` 找未知字段和旧工具名。
- Team 文件必须显式选择。
- 角色额度需要完整覆盖，并与固定名册调用配合。
- 公开导入优先用 `opencollab`、`opencollab.tools`、`opencollab.environments` 和 `opencollab.workflows`。旧集成如果引用内部路径，对照目标版本找到公开入口。
- 自定义工具应保留参数 schema 和实际执行结果。
- 环境提供器应继续负责申请、交付和清理。

先在小任务里做一次文件读取、修改或命令操作，再看结果和资源状态，确认扩展在新版本里正常工作。

## 第四步：用原任务验证

1. 选一个以前稳定能跑通的任务。
2. 用原来的 CLI 命令或 SDK 调用再跑一次。
3. 明确设置 token、步数和时间限制。
4. 核对最终交付文件和实际测试结果。
5. 如果服务有取消路径，触发一次取消，检查保存材料、用量和所拥有资源的清理结果。

## 进阶：历史实验和 OCE

- 历史实验继续保留原版本信息。旧结果的模型、任务、提示词和限制也要一起保存。
- OCE 集成按它记录的框架依赖版本来理解。评测分工见 [用 OCE 评测 OC 的 Agent、Team 和 Workflow](../observability/evaluation-boundary.md)。

## 参考

- 默认 Agent 的具体变化：[0.8.0 迁移](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/migrations/0.8.0.md)
- 当前公开接口：[包指南](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md)
