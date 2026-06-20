---
title: 会话纪要 — 从 CL4R1T4S 提示词挖掘到 Context 基础设施升级
date: 2026-06-15
branch: feat/mini-workflow
scope: 一次会话内,从"评估一个外部仓库"一路推进到"上下文系统承重升级 + Loader 设计"
---

# 一句话总结

从用户丢来一个提示词泄露仓库(CL4R1T4S)出发,先把其中**对编码 agent 有用的提示词模式**系统挖掘并吸收进 OC,再顺势把暴露出来的**上下文(Context)系统短板**做了一次基础设施升级:让分层上下文**真正承重**,并为下一步的 Loader 出了完整设计。本分支净增 3 个提交,测试从 568 → 576 全绿,ruff 干净。

---

# 一、起点与目标

用户给了仓库 `github.com/elder-plinius/CL4R1T4S`(Pliny 收集的**生产级 AI 产品系统提示词泄露/逆向集合**,按厂商分目录),问:**这对设计 OC 的提示词有没有帮助?如何吸收?** 要求用英文 subagent 探索、保持主 context 清爽。

由此引出一条主线:**提示词吸收**;并意外牵出第二条主线:**上下文系统升级**。

---

# 二、做了什么(时间线)

## 阶段 1 — 评估 CL4R1T4S(subagent 探索)
- 确认其性质:**泄露/逆向、未经厂商确认**的系统提示词;许可 AGPL,来源存疑 → 结论:**学技法、用 OC 自己的话重写,绝不逐字搬运**。
- 判定:**部分有用**——OC 现有提示词已成熟,但这些 C 端/IDE agent 提示词能补几个 OC 临时处理或缺失的点。
- 单独评估了用户点名的 `ANTHROPIC/CLAUDE-FABLE-5.md`:认定它是**消费端聊天**提示词,对 OC(编码 agent)价值弱,真正有用的是 Cursor/Devin/Claude Code 那几份。

## 阶段 2 — 系统挖掘(Workflow,23+1 agent)
- 用 GitHub API 枚举出 61 个提示词文件,锁定 **24 个编码 agent 相关**(Claude Code、Cursor×3、Devin×3、Cline、Windsurf×2、Codex×2、Replit×3、Droid、Bolt、Lovable、v0、Manus×2、Grok-Code、Dia、Same)。
- 工作流:每文件一个 agent 提取可迁移模式 → 综合 agent 去重并映射到 OC 真实文件。
- 产出 **282 条模式**,综合报告落到 `docs/2026-06-15-cl4r1t4s-prompt-mining.md`。

## 阶段 3 — 吸收落地(Workflow,4 agent)+ 提交
把 OC-适用的模式真正改进代码(均"先读后改、适配真实内容"):
- **工具描述层**(`adapters/tools/fs.py`/`bash.py`/`run_tests.py`):读后再改门槛、grep/file_read/run_tests 路由提示、bash 无头(no-TTY/绝对路径)纪律——经 function schema 直达每个 agent。
- **Workflow 提示层**(`self_collab.py`/`split_solve.py`):`SHARED_RULES` 增 3 条(验证 import、≤8 行汇报、别 grep 不存在的测试)且两文件字节级一致;**三态裁决 PASS/FAIL/BLOCKED**(环境阻塞短路轮次循环,不再空烧);apply_patch 回退细化;新增对应单测。
- **README** 增 "Context layering" 段。
- 提交:`6837a76`(split_solve 工作流落地)、`a68f480`(CL4R1T4S 吸收)。

## 阶段 4 — Context 系统升级(发现问题 → 设计 → 实现 → 提交)
写 README 时暴露出真正的架构张力:**分层上下文只管"开局种子",执行一开始就退化成扁平消息列表,压缩器对层一无所知**——`AutoCompactShaper` 甚至可能把 agent 自己的 TASK 折进摘要。即"层没有承重"。

与用户确认方向后(选了"让层承重"),实现并提交 `d079864`:
- 优先级 `LAYER_PRIORITY` + `ContextSource.priority` 覆盖 + `effective_priority`;
- 种子消息盖 `_ctx={layer,priority}` 标签(随 dict 传递,provider 忽略);
- `PIN_FLOOR=70` 钉住 identity/team/task,**autocompact 绕开钉住段**(修掉潜在 bug);
- 新增 `LowPriorityContextShedShaper`:压力下先卸最低优先级(memory→project),不碰钉住/无标签消息;
- 更新 README、加 7 个单测。

## 阶段 5 — Loader 设计(subagent,仅报告)
为让 PROJECT/MEMORY 不再是空壳,设计了 `ContextLoaderPort` 及其接入(端口签名、解析时机、与 pin/shed 的衔接、首个 `ProjectConventionsLoader`、记忆召回、bootstrap 接线、resume 风险、卸载排序、测试与分阶段计划)。落到 `docs/2026-06-15-context-loader-design.md`。**未实现**。

---

# 三、解决了什么问题

1. **"外部泄露提示词能不能用、怎么用"有了明确答案**:可借技法不可搬内容;并把可借的部分真正落进代码,而非停留在讨论。
2. **补齐 OC 提示词的几个真实缺口**:工具使用指引现在直达每个 agent;环境阻塞不再伪装成代码失败空烧预算(BLOCKED);汇报更精炼(≤8 行)。
3. **修掉一个潜在正确性 bug**:开启摘要后,agent 的 TASK 可能被压缩进摘要——现已被 pin 机制杜绝。
4. **让分层上下文从"装饰"变"承重"**:优先级/钉住/卸载真正参与上下文预算治理,且对当前运行零行为改变(纯增量、卸载器在内容为空时休眠)。
5. **为下一步铺好路**:Loader 有了可直接照做的分阶段设计。

---

# 四、产出清单

**代码提交(feat/mini-workflow,均未推送)**
- `6837a76` feat: split_solve 独立子任务工作流 + 测试
- `a68f480` feat: 吸收 CL4R1T4S 编码提示词模式进工具/角色提示
- `d079864` feat: 让上下文层承重(优先级、钉住、低优先级卸载)

**文档**
- `docs/2026-06-15-cl4r1t4s-prompt-mining.md` — 282 条模式的挖掘报告
- `docs/2026-06-15-context-loader-design.md` — Loader 完整设计
- `docs/2026-06-15-session-report.md` — 本纪要

**质量**:测试 568 → 576 全绿;ruff 干净;clean-architecture 边界完好(domain 纯净、端口在 application、接线在 bootstrap)。

---

# 五、遗留 / 下一步

1. **Loader 实现**(最自然的承接):按 P0(抽 `stamp()` 辅助)→ P1(`ProjectConventionsLoader` 读 CLAUDE.md、接 spawn+lead 两路,PROJECT 落地、卸载器转正)推进。
2. **SWE-bench A/B**:本批提示词 + 上下文改动对 61.7% 基线验证 resolved-rate 不回退、并看 ≤8 行汇报的 token 收益(需重拉镜像,用户拍板)。
3. **几个待定**:port 同步/异步、OpenAI 前是否剥 `_ctx`、卸载两阶段排序——见 loader 设计文档"Open decisions"。
4. 整批仍**未提交推送 / 未合并**到 main。

---

# 汇报口径(30 秒版)

> 今天从评估一个外部提示词仓库,做成了两件事:一是把其中对编码 agent 有用的提示词模式系统挖掘(282 条)并真正吸收进 OC——工具指引、BLOCKED 裁决、精炼汇报都落地了;二是顺手把上下文系统做了一次基础设施升级,让分层上下文真正承重(优先级/钉住/卸载),还修掉一个会把任务摘要掉的潜在 bug。三个提交,测试 568→576 全绿。Loader 的完整设计也出好了,下一步直接照做。
