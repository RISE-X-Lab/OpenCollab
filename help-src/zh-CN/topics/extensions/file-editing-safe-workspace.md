---
{
  "id": "file-editing-safe-workspace",
  "title": "安全地查找读取和修改文件",
  "category": "extensions",
  "summary": "OC 内置文件工具（file_read、grep、file_write、apply_patch、git_diff）的参数和编辑模式，以及路径越界、符号链接、覆盖保护等报错的含义。",
  "aliases": [
    "越界路径",
    "符号链接",
    "file_read",
    "file_write",
    "grep",
    "apply_patch",
    "str_replace",
    "line_replace",
    "unified_diff",
    "overwrite",
    "relocate_expected",
    "normalize_hunks",
    "git_diff"
  ],
  "keywords": [
    "文件工具",
    "读写文件",
    "路径限制"
  ],
  "questions": [
    "在 Agent 实际工作区内定位文件、准确编辑内容并理解路径或覆盖错误？",
    "file_write 和 apply_patch 各有哪些模式？",
    "str_replace 为什么报找不到或不唯一？",
    "Agent 说改好了，为什么源目录没变？",
    "为什么路径被拒绝了？"
  ],
  "related": [
    "execution-environments",
    "worktree-workspaces",
    "shell-permissions-execution"
  ],
  "verified_ref": "0c47f74a"
}
---

# 安全地查找读取和修改文件

OC 内置的文件工具是 `file_read`、`grep`、`file_write`、`apply_patch` 和 `git_diff`。它们有一个和直觉不同的地方：所有路径都相对于 Agent 自己的执行工作区解释，而不是你启动进程的目录。Agent 在工作树或容器里时，改的是那里的副本。读写还会检查路径越界、符号链接和文件类型。本文给出各工具的参数和编辑模式，以及常见报错怎么处理。

## 速查

| 想做什么 | 工具与模式 | 关键参数 |
| --- | --- | --- |
| 分页读文件 | `file_read` | `path`、`offset`、`limit` |
| 搜索 | `grep` | `pattern`（正则）、`path`、`glob`、`max_results`（默认 50） |
| 新建或整体替换 | `file_write` `create` | `content`，大幅缩短已有长文件需 `overwrite=true` |
| 替换一段唯一原文 | `file_write` `str_replace` | `old_str`、`new_str` |
| 按行替换或插入 | `apply_patch` `line_replace` | `start_line`、`end_line`、`expected_str`、`new_str` |
| 一次改多处 | `apply_patch` `unified_diff` | `patch` |
| 查看改动 | `git_diff` | `path`、`staged`、`stat_only`、`include_status` |

具体参数以所选工具的 schema 为准。

## 读取与定位

```json
{"path": "src/parser.py", "offset": 1, "limit": 120}
```

- 大文件分段返回。看结果里实际显示的范围和下一段的位置，再继续读。
- 可以先用 `grep` 找到函数，再读附近的内容。
- 单个工具的输出上限和历史压缩可能让模型看不到全部内容。文件本身仍完整地在执行环境里，用更窄的范围重读即可。

## 编辑文件

### 新建或整体写入：`create`

```json
{"path":"settings.py","mode":"create","content":"RETRIES = 2\n"}
```

完整内容放进 `content`。这个模式也能整体替换已有文件。如果新内容让一个已有的长文件大幅变短，必须显式传 `overwrite=true`，这是防止误覆盖的保护。

### 局部替换：`str_replace`

```json
{"path":"settings.py","mode":"str_replace","old_str":"RETRIES = 2","new_str":"RETRIES = 3"}
```

`old_str` 必须在文件里恰好出现一次。不唯一时，多带几行邻近代码。

### 按行替换：`line_replace`

```json
{"path":"settings.py","mode":"line_replace","start_line":1,"end_line":1,"expected_str":"RETRIES = 3","new_str":"RETRIES = 4","relocate_expected":true}
```

- 行号从 1 开始，`end_line` 包含在替换范围内。
- `expected_str` 用来核对当前范围的原文，适合读过文件之后再编辑。
- 行号过期、但原文在文件里唯一时，可以显式开启 `relocate_expected`，让工具先找到原文再改。
- `end_line` 等于 `start_line - 1` 时，表示在该行之前插入。

### 一次改多处：`unified_diff`

```json
{"path":"settings.py","mode":"unified_diff","patch":"@@ -1 +1 @@\n-RETRIES = 4\n+RETRIES = 5\n"}
```

- `patch` 接受标准的 `@@ -a,b +c,d @@` hunk，可以省略文件头。
- 工具按上下文和旧内容匹配，容许行号小幅漂移。
- 显式 `normalize_hunks=true` 会重算 hunk 行数，并允许上下文唯一的裸 `@@` 标头。
- 只要有一个 hunk 或范围匹配失败，整个编辑都不生效，文件保持原样。

### 改完之后

用 `git_diff` 或原生 Git 命令确认改动范围，再跑相关测试。写入成功只说明文件写好了，功能对不对要靠项目的实际验证。

`git_diff` 参数：

| 参数 | 作用 |
| --- | --- |
| `path` | 只看某个文件 |
| `staged=true` | 看暂存区内容 |
| `stat_only=true` | 只看文件级统计 |
| `include_status=false` | 省略状态部分 |

默认显示相对 HEAD 的未提交改动，并附上未被忽略的未跟踪文件。结果可能被截断，之后可以按文件继续看。

## 路径为什么被拒绝

内置的本地文件适配器会检查路径、链接和是否为普通文件。容器里的路径由对应的环境检查。

如果遇到链接或越界路径被拒绝：

1. 先确认目标文件是否真是任务需要的。
2. 是的话，通过环境配置安排正确的工作目录或挂载资源，而不是绕过检查。

注意：这些路径限制只约束原生文件工具。宿主 Bash 仍然拥有宿主进程的全部权限，两者要分开看。执行不可信的项目代码时，优先配置进程和网络隔离，见 [管理终端权限并确认命令结果](shell-permissions-execution.md)。

## 多个 Agent 同时修改

- 共享目录里的成员看到的是同一份文件。文件写锁能保护单次原生编辑，但多个任务的修改仍可能互相影响。
- 想让各自独立实现、再挑结果，用工作树或候选环境，再通过明确的采用动作交付。

如果出现"Agent 已经改好，但源目录没变"：先看当前环境和候选的交付状态，再怀疑编辑工具。隔离任务在自己的目录里修改后，要通过采用或恢复补丁把结果带回目标目录。

`FileWriteTool(allow_create=False)` 的模型可见 schema 会列出 `str_replace`。自编 Workflow 继承父成员的创建限制后，子会话的工具说明随实际权限更新。

## 源码与进一步阅读

[opencollab/adapters/tools/fs.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/fs.py#L42) · [opencollab/adapters/tools/apply_patch.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/apply_patch.py#L28) · [opencollab/adapters/safe_anchored_files.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/safe_anchored_files.py#L27) · [opencollab/adapters/_env_docker.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/_env_docker.py#L748)

[让任务在指定目录或现有容器里执行](../isolation/execution-environments.md) · [让多个任务在独立工作区改代码](../isolation/worktree-workspaces.md) · [管理终端权限并确认命令结果](shell-permissions-execution.md)
