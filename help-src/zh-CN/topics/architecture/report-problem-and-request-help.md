---
{
  "id": "report-problem-and-request-help",
  "title": "遇到问题怎么反馈、求助或提建议",
  "category": "architecture",
  "summary": "选对 GitHub 反馈入口，附上最小复现和脱敏后的记录，让维护者能快速重现你遇到的问题。",
  "aliases": [
    "怎么提 issue",
    "找谁帮忙",
    "报 bug",
    "功能建议",
    "最小复现",
    "提供哪些日志",
    "脱敏",
    "安全漏洞",
    "Security Advisories",
    "怎么反馈问题",
    "提问模板"
  ],
  "keywords": [
    "问题反馈",
    "Bug report",
    "Question or support",
    "Feature request",
    "GitHub Issue",
    "version",
    "repro",
    "logs"
  ],
  "questions": [
    "选择正确反馈入口，用最小复现和必要记录让维护者理解遇到的问题？",
    "遇到 bug 该在哪里提？",
    "提 issue 要附哪些信息？",
    "发现安全漏洞怎么私下报告？",
    "分享日志前要删掉什么？"
  ],
  "related": [
    "configuration-precedence",
    "run-results",
    "artifacts-and-history",
    "upgrade-existing-integration"
  ],
  "verified_ref": "v0.9.6"
}
---

# 遇到问题怎么反馈、求助或提建议

OC 的反馈走 GitHub Issues，有三个模板：Bug report、Question or support、Feature request；安全漏洞走私密的 Security Advisories。写好一份反馈的要领是：先写目标、预期和实际看到的现象，再给最小复现，附上 `run_id` 等运行记录，提交前脱敏。

## 第一步：选对入口

| 你的情况 | 选哪个 | 入口 |
| --- | --- | --- |
| 看到了错误行为或报错 | Bug report | [Issues 模板选择页](https://github.com/RISE-X-Lab/OpenCollab/issues/new/choose) |
| 不知道怎么配置、怎么用 | Question or support | 同上 |
| 希望增加新能力 | Feature request | 同上 |
| 疑似安全漏洞 | 私密安全报告 | [Security Advisories](https://github.com/RISE-X-Lab/OpenCollab/security/advisories/new) |

- Bug report：写清可以观察到的错误行为。
- 使用问题：写清你的目标，以及卡在哪一步。
- 功能建议：写清你现在工作中遇到的具体困难，以及这个能力会怎样改善它。
- 安全问题：**不要发公开 issue**。框架会执行模型生成的代码，也涉及凭据和容器资源，所以要走私密入口。报告里说明触发条件、影响范围和复现材料。仓库安全策略给出的目标是 72 小时内确认收到。

提问之前，可以先按 [启动出错：按阶段排查](../models/configuration-precedence.md#启动出错按阶段排查) 自查一遍，很多配置问题能当场定位。

## 第二步：按这个顺序写问题

1. 想完成什么任务（Goal）。
2. 预期结果（Expected behavior）。
3. 实际看到了什么（Observed behavior）。要具体：哪个文件还是旧内容，工具返回了什么，命令退出码是多少。
4. 最小复现：能重复出问题的最短命令或 SDK 调用（Reproduction）。
5. 环境信息：组织方式、调用接口、模型与 provider、包版本或 Git 提交（Environment）。同一个包版本号可能对应不同提交，所以源码安装要附提交号。
6. 证据：脱敏后的工具结果、diff、traceback（Evidence）。

“最小复现”指去掉无关内容之后，仍能触发问题的最小例子：命令保留关键选项；SDK 示例保留触发错误的最小函数和参数；团队文件保留相关的角色、工具与拓扑；项目用一个小项目加一次工具操作就够，方便维护者直接执行。

下面是一份补丁交付问题的英文反馈示例，提交时换成你的实际版本、任务和观察结果：

```text
Goal
Apply a completed coder's changes to the delivery workspace.

Expected behavior
The adopted implementation appears in the delivery directory.

Observed behavior
The coder completed, but the source file retained its previous contents.

Reproduction
Run the attached minimal team configuration with the included SDK script.
Inspect the coder completion result and the final source file.

Environment
Team through Python SDK, with the exact package version and commit attached.

Evidence
Include the sanitized tool result, final diff, and relevant traceback.
```

## 第三步：附上有用的记录

| 记录 | 有什么用 |
| --- | --- |
| 运行结果里的 `status`、`reason`、`agent_failures`、`metrics["run_id"]` | 关联到具体的一次运行 |
| 产物目录、清单和轨迹 | 展示调用顺序 |
| 保存的会话 | 看当时模型收到的任务 |
| 生效的配置字段和相关 CLI 参数 | 排查配置问题 |

这些记录怎么读，见 [判断任务是否真的完成](../observability/run-results.md) 和 [运行记录](../observability/artifacts-and-history.md)。

## 第四步：提交前脱敏

删掉 API key、授权头、带凭据的 URL、私有仓库内容和敏感日志。完整轨迹可能包含任务文件和模型回答，分享前先打开检查一遍。保留错误类型、调用顺序、必要参数和相关日志片段，让维护者仍能还原失败过程。

## 收到修复之后

用原来的复现步骤再执行一次，检查原先出错的文件或操作，然后在同一个 issue 下补充：用了哪个修复提交、实际执行的命令和最终结果。这样维护者能判断问题是否真正解决。

## 延伸阅读

- 源码依据：[Bug report 模板](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/.github/ISSUE_TEMPLATE/bug_report.yml) · [使用问题模板](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/.github/ISSUE_TEMPLATE/question.yml) · [功能建议模板](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/.github/ISSUE_TEMPLATE/feature_request.yml) · [安全策略](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/SECURITY.md)
- 相关指南：[配置从哪来、哪个生效](../models/configuration-precedence.md) · [判断任务是否真的完成](../observability/run-results.md) · [运行记录](../observability/artifacts-and-history.md) · [升级 OC 后怎样迁移和验证](upgrade-existing-integration.md)
