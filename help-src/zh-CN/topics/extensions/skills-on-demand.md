---
{
  "id": "skills-on-demand",
  "title": "Skill：按需加载的说明书",
  "category": "extensions",
  "summary": "在工作区 skills/ 下放 SKILL.md，给角色加 use_skill 工具，模型按需把说明拉进会话；Skill 只提供指导，不授予权限。",
  "aliases": [
    "SKILL.md",
    "use_skill",
    "技能包",
    "技能不加载",
    "操作手册",
    "skills 目录"
  ],
  "keywords": [
    "Skill",
    "技能"
  ],
  "questions": [
    "给角色增加按需加载的操作说明，并知道技能内容何时进入会话？",
    "SKILL.md 放在哪里才会被加载？",
    "技能没出现在列表里怎么办？",
    "技能正文有长度限制吗？"
  ],
  "related": [
    "tool-selection",
    "custom-tools",
    "configure-team"
  ],
  "verified_ref": "v0.9.6"
}
---

# Skill：按需加载的说明书

Skill 是一份写给 Agent 看的操作说明。OC 的 Skill 和 Claude Code 的 Skill 思路相同：一个带 frontmatter 的 `SKILL.md`，平时只有名称和描述出现在系统提示里，模型需要时调用 `use_skill`，把正文作为工具结果拉进会话。这样说明书很多也不会占满上下文。

先记住两条规则：Skill 从任务工作区的 `skills/` 目录加载，运行启动时扫描一次；角色必须在 `tools` 里声明 `use_skill` 才能用。更重要的是，**Skill 只提供说明，不授予任何权限**，执行仍靠角色已有的工具。

## 最小用法

1. 在任务工作区建目录，每个直接子目录放一个 `SKILL.md`，也可以放辅助脚本或模板：

   ```text
   skills/
     review-change/
       SKILL.md
   ```

2. 写 `SKILL.md`：

   ```markdown
   ---
   name: review-change
   description: 在用户要求审查代码变更时，核对正常使用影响与实际验证证据。
   ---

   先明确变更要解决的用户问题，再阅读对应实现与调用者。
   用正常使用过程说明触发条件，并验证修复前后的行为。
   最后汇报影响、验证结果以及仍需用户决定的事项。
   ```

   | 字段 | 说明 |
   | --- | --- |
   | `name` | 调用键，建议和目录同名 |
   | `description` | 出现在技能目录摘要里，写清具体的触发场景，模型靠它判断什么时候加载 |

3. 给角色加上 `use_skill`，同时给它实际需要的读取、编辑或命令工具：

   ```yaml
   entry: reviewer
   roles:
     reviewer:
       prompt: 审阅给定变更，任务符合 review-change 时加载对应技能。
       tools: [file_read, grep, git_diff, use_skill]
   ```

4. 运行。启动时系统提示会列出成功加载的技能名称和描述，模型选中技能后提交 `{"name":"review-change"}` 拿到正文。名称写错时，返回结果会列出当前可用的名称。

`use_skill` 的工具 schema 里也写着可用的技能名称和说明，所以单 Agent 和 Workflow 里的 Agent 看工具定义就能选技能。

## 辅助文件和脚本

`use_skill` 只返回 `SKILL.md` 的正文。辅助文件当普通工作区文件读，例如让角色用 `file_read` 打开 `skills/review-change/templates/report.md`；脚本由角色已有的命令工具执行。

## Skill、工具和 Workflow 的关系

- **工具权限只来自角色配置。** 一个只有 `file_read` 和 `use_skill` 的角色，加载了写代码的步骤，也还是不能编辑。要编辑，就在角色配置里加写入工具。
- **Skill 是指导，不是流程。** Skill 可以告诉 Agent 怎样设计 Workflow，但执行顺序、并发和失败处理要靠 Workflow 的代码和接口确定，见 [编写 Workflow](../workflow/workflow-programming-model.md)。
- 一个具体的业务操作适合写成 [自定义工具](custom-tools.md)，较长的使用说明适合放进 Skill。

## 加载限制

| 项目 | 上限 |
| --- | --- |
| 正文 | 8,000 字符 |
| 单个文件 | 64 KiB |
| `description` | 超过 500 字符会被截断后放进目录 |
| 扫描范围 | 根目录最多 256 个技能包目录、4,096 个条目 |

## 常见问题

- **技能没出现在列表里**：检查加载诊断。frontmatter 写错、名称重复、文件是链接、大小超限，都会导致加载失败。
- **改了技能没生效**：技能在启动时加载，新增或修改后要重新启动运行。
- **正文进入会话后被压缩了**：技能正文是普通的工具结果，和其他工具结果一样受上下文整理规则影响，见 [上下文窗口与压缩](../operations/context-compaction.md)。
- **想跨项目共享技能**：由部署流程把同一份技能放进各项目工作区的 `skills/` 目录。

## 延伸阅读

- 源码依据：[skills/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/skills/README.md) · [opencollab/adapters/skills/file_skill_store.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/skills/file_skill_store.py) · [opencollab/adapters/tools/use_skill.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/use_skill.py)
- 相关指南：[内置工具](tool-selection.md) · [自定义工具](custom-tools.md) · [Team 入门](../team/configure-team.md)
