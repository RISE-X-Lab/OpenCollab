---
{
  "id": "skills-on-demand",
  "title": "使用与编写 Skill",
  "category": "extensions",
  "summary": "在工作区 skills/ 下放 SKILL.md，给角色加 use_skill 工具，模型按需把技能正文拉进会话。",
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
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 使用与编写 Skill

OC 的 Skill 和 Claude Code 的 Skill 思路相同：一个带 frontmatter 的 `SKILL.md`，平时只有名称和描述出现在系统提示里，模型需要时调用 `use_skill` 把正文作为工具结果拉进会话。OC 的具体做法是：从任务工作区的 `skills/` 目录加载，运行启动时扫描一次，角色必须在 `tools` 里声明 `use_skill` 才能用。Skill 只提供说明，不授予任何权限，执行仍靠角色已有的工具。

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
   | `description` | 出现在技能目录摘要里，写清具体的触发场景 |

3. 给角色加上 `use_skill`，同时给它实际需要的读取、编辑或命令工具：

   ```yaml
   entry: reviewer
   roles:
     reviewer:
       prompt: 审阅给定变更，任务符合 review-change 时加载对应技能。
       tools: [file_read, grep, git_diff, use_skill]
   ```

4. 运行。启动时，系统提示会列出成功加载的技能名称和描述。模型选中技能后，提交下面的参数拿到正文：

   ```json
   {"name":"review-change"}
   ```

   名称写错时，返回结果会列出当前可用的名称，方便检查拼写和加载情况。

## 辅助文件和脚本

`use_skill` 只返回 `SKILL.md` 正文。

- 辅助文件当普通工作区文件读，例如让角色用 `file_read` 打开 `skills/review-change/templates/report.md`。
- 脚本由角色已有的命令工具执行。

## 和工具、Workflow 的关系

- 工具权限只来自角色配置。一个只有 `file_read` 和 `use_skill` 的角色，加载了写代码的步骤，也还是不能编辑。要编辑，得在角色配置里加写入工具。
- Skill 可以告诉 Agent 怎样设计 Workflow，程序员也可以直接看开发手册写 Python 工作流。执行顺序、并发和失败处理要靠 Workflow 的代码和接口确定，Skill 只是提供指导。

## 加载限制

| 项目 | 上限 |
| --- | --- |
| 正文 | 8,000 字符 |
| 单个文件 | 64 KiB |
| `description` | 超过 500 字符会被截断后放进目录 |
| 扫描范围 | 根目录最多 256 个技能包目录、4,096 个条目 |

## 常见问题

- 如果技能没出现在列表里：检查加载诊断。frontmatter 写错、名称重复、文件是链接、大小超限，都会导致加载失败。
- 如果改了技能没生效：技能在启动时加载，新增或修改后要重新启动运行。
- 如果正文进入会话后被压缩了：技能正文是普通的工具结果，和其他工具结果一样受上下文整形规则影响。
- 如果想跨项目共享技能：由部署流程把同一份技能放进各项目工作区的 `skills/` 目录。

`use_skill` 的工具 schema 也列出当前技能名称和说明。单 Agent 或 Workflow 可以从工具定义选择技能，再按名称读取正文。

## 源码与进一步阅读

[skills/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/skills/README.md) · [opencollab/adapters/skills/file_skill_store.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/skills/file_skill_store.py) · [opencollab/adapters/tools/use_skill.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/use_skill.py)
