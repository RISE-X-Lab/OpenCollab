---
{
  "id": "worktree-workspaces",
  "title": "让多个任务在独立工作区改代码",
  "category": "isolation",
  "summary": "让几个任务同时改同一个项目而不互相踩到，并知道改动最后去了哪里。",
  "aliases": [
    "分目录",
    "独立分支",
    "Git worktree",
    "WorktreeEnvironment",
    "WorktreePool",
    "use_worktrees",
    "source workspace has uncommitted changes"
  ],
  "keywords": [
    "工作树",
    "并行改代码",
    "worktree",
    "独立工作区"
  ],
  "questions": [
    "并行修改同一个项目时保留清晰的文件起点并理解结果如何返回？",
    "两个 Agent 同时改代码会不会冲突？",
    "报错 source workspace has uncommitted changes 怎么办？",
    "工作树里的改动怎么回到我的项目？"
  ],
  "related": [
    "isolation-overview",
    "candidate-delivery",
    "workspace-recovery"
  ],
  "verified_ref": "0c47f74a"
}
---

# 让多个任务在独立工作区改代码

`isolation="worktree"` 让每个会话在自己的 Git worktree 里改代码，用于并行实现和候选比较。OC 的默认取舍：普通工作树从 `HEAD` 创建，要求源仓库干净；带 `adopt_agent` 的 Team 则从当前交付内容做快照；改动不会自动写回，要经过采纳。非 Git 目录会退化为目录副本。

本文讲：手动创建工作树、起点是哪个版本、改动怎么交回来。

## 最小示例：创建一个工作树

在一个干净的 Git 项目或普通目录里运行：

```python
import asyncio
from opencollab.environments import worktree_environment

async def main():
    environment = worktree_environment(".")   # 只创建对象，还没有建目录
    try:
        await environment.setup()             # 这一步才真正准备目录
        print(environment.workspace)          # 工具实际使用的路径
        result = await environment.exec_cmd("pwd")
        print(result.returncode, result.stdout)
    finally:
        await environment.cleanup()           # 用完要自己清理

asyncio.run(main())
```

要点：

1. `worktree_environment(".")` 只创建对象。
2. `setup()` 才准备目录。
3. `workspace` 是工具实际读写的路径。
4. 如果你从仓库的子目录启动，新工作树里也会停在同样的子目录。这样已有脚本里的相对路径还能用。
5. 这样单独创建的工作树由你自己管理。先导出需要的改动，再 `cleanup()`。

## 工作区从哪个版本开始

普通的工作树从 Git 提交创建，所以有两个要求：

- 源仓库要有能解析的 `HEAD`。
- 源仓库要是干净的，没有未提交的改动。

如果不干净，会报错 `source workspace has uncommitted changes`，并列出相关文件。处理步骤：

1. 运行 `git status --short` 看看是哪些文件。
2. 按项目习惯提交这些修改，或者用 stash 先存起来。
3. 再次运行。

当前运行自己生成的未跟踪产物目录不算在内。已跟踪文件的改动仍然会被检查。

带采纳能力的 Team 不一样。如果 Team 声明了 `adopt_agent`，子任务会从“候选快照”开始。候选快照包含交付目录里当前的内容，包括你的修改和之前已经采纳的结果。审阅任务还可以直接以被审阅成员的真实环境为起点。

简单记：

| 情况 | 起点 |
| --- | --- |
| 普通 `worktree` | Git 提交 |
| 带 `adopt_agent` 的 Team | 当前交付内容做成的候选快照 |

## 改动怎么交回来

- 子成员的完成结果里可以带工作树的 diff。
- 声明了采纳工具的入口成员，要显式采用成功的 Coder 的结果。
- 采用之后再派一个新的 Tester，它才会从已经更新的交付内容开始检查。

完整流程见 [把候选结果采纳到交付目录](candidate-delivery.md)。

## 进阶：普通目录、子模块和共享资源

如果源目录不是 Git 仓库，环境会复制一份独立副本，并用文件差异记录改动。

如果是 Git 项目，还要注意：

- 已初始化的子模块
- 文件的可执行位
- 实际写到磁盘上的内容

候选准备会处理能用的源子模块，并检查实际文件内容能否重建。

工作树只把文件编辑分开。进程和网络仍然和父环境一样，而且工作树位于父执行环境里：父任务在容器中，子工作树也在那个容器里执行。所以：

- 两个任务在同一端口启动服务，仍可能冲突。
- 共用同一个外部缓存，也可能互相影响。

需要连进程都分开时，按 [隔离怎么选](isolation-overview.md) 选容器策略。

## 常见问题

- 如果报 `source workspace has uncommitted changes`：见上面“工作区从哪个版本开始”的三个步骤。
- 如果报子模块准备错误：确认源端子模块已经初始化到需要的状态，再看报错里指出的子模块路径。
- 如果补丁捕获失败，或看到改动被保留的提示：先按 [找回隔离任务的改动并清理资源](workspace-recovery.md) 导出完整材料。

## 参考

创建过程见[工作树环境](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/_env_worktree.py)和[工作树池](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/worktree_pool.py)。交付入口见 [把候选结果采纳到交付目录](candidate-delivery.md)。
