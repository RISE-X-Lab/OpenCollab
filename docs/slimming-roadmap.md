# OpenCollab Slimming Roadmap — 多会话执行手册

> **给谁看.** 这份文件是"每阶段一个新 Claude 会话"工作流的**单一真相源**。每个新会话只需读本文件(深层证据看 `docs/pattern-catalog.md`),照对应 Lane 干活即可。
>
> **一句话思路.** 代码瘦身 + pattern 显性化 + 报告写作是**沿概念脊椎的一条流水线**:每个子系统走 `slim → 顺手 docstring → 那一章就能写`。
>
> **基线** `cc04a36`(分支 `chore/release-pre-push`,或从当时的 `main`)。脊椎顺序:FSM → scheduler → context → provider/tools → 持久化 → 边界。

---

## 铁律(每个会话都必须遵守)

1. **一个会话只做一个 Lane**,一分支一 PR,不越界。分支从最新 `main`(或上一 lane 的 tip)切。
2. **改行为前:先对齐 → 再布网 → 只锁核心**(仅改行为的步骤适用):
   - **先对齐**:动码前先向人报告**现状逻辑 + 期望逻辑**,讨论达成一致再开工(别闷头改)。
   - **再布网**:写 golden-master / characterization 测试锁住**要保住的行为**(先跑确认绿),再动物理代码。
   - **只锁核心**:网只覆盖**核心不变量**(FSM 拓扑、判分读的 `AgentRunResult.outcome`、持久化往返、预算闸门…);**外围 / 展示层测试(TUI、toolbar、渲染串之类)该删就删,完全不必锁**——锁核心是为了别人 fork 后能自由改外围而不被我们的具体行为绑死,不是让 fork 复刻我们的每个像素。
3. **docstring 只加 docstring / rename / 抽已重复项**,**绝不加新抽象层**(小而美,prefer deletion over abstraction)。
4. **不许碰**(都是 live / 核心):`config.py` 的 file-first key 优先级、`fact_sheet.py`、`ports.py` 成员、`spawn_with_review`。
5. **完成判据**:`cd opencollab && .venv/bin/python -m pytest -q` 全绿 + `.venv/bin/ruff check opencollab/` 绿 + `tests/test_*_boundaries.py` 绿。(首次需 `cd opencollab && uv sync --extra dev`。)
6. **收尾**:conventional commit(`refactor:` / `docs:` / `test:`),**不 push、不合并**,等人确认。完成后在项目记忆 `project_slimming_plan.md` 里标记本 lane 完成(给下一个会话看)。

---

## 五个阶段

| 阶段 | 内容 | 会话数 |
|--|--|--|
| 0 地基 | 审计 + 覆盖矩阵 + blueprint + `pattern-catalog.md` | ✅ 已完成 |
| 1 清场 | 零风险独立死码删除 | 1 |
| 2 逐段流水线 ★ | 6 个 lane(S1–S4c),各 slim + docstring | 6 |
| 3 写报告 prose | 9 章按 outline 铺 | 1–9 |
| 4 收尾/公开 | commit · md→LaTeX · 合 main | 1 |

**路线决策(报告先写还是后写)**:
- **甲(推荐,质量优先)**:逐段 slim→docstring→写那一章,报告描述干净码、零返工,但报告等代码。
- **乙(速度优先)**:现在就按 `pattern-catalog.md` 写报告(引当前 code + 标 "planned sharpening"),slim 后轻改一遍。

---

## Lane 规格

### Phase 1 · 清场（零风险死码）
**目标**:纯删,无前置,~-40 行。每删一项先 `grep -rn` 证明 0 引用(排除定义处 + 测试)。
- 删 `adapters/storage.py` 的 `JsonlStore._parse`(被 `_parse_document` 取代)
- 删 `application/async_timeout.py` 的 `terminate_tasks` + `AsyncRuntimeUnhealthyError`,及 `sdk/lifecycle.py` 对应 `__all__` 项
- 删 `domain/scheduler.py` 的 `SchedulerState.all_done`、`application/_scheduler_constants.py` 的 `MAX_FORCED_CLEANUP_TIMEOUT`
- 删 `application/autosave.py` 的 `pending_write_futures` 空 compat 属性(若本 lane 一起做)
- 删幽灵 `opencollab/opencollab/harness/`(只剩 pyc)+ `opencollab/container.id`、`container.name` 运行时残留,并把后两者加进 `.gitignore`
- 归档 `docs/2026-06-15-context-loader-design.md` → `docs/archive/`(front-matter 已注明 not implemented;`grep ContextLoaderPort` = 0)
**解锁**:无(纯清场)。

### Lane S1 · 生命周期 / FSM（心脏，最大解锁）
深层证据:`pattern-catalog.md` §3.1、§3.2、§7。**严格按此顺序**:
1. **布网**:补 `test_session_characterization` + 终态契约测试 + `from_dict` 恢复兼容(历史快照串 `budget_exceeded`/`cancelled`/… 映射到 `STOPPED`)+ `TurnEnforcementState` 往返 JSON **逐键相等**测试。先跑,确认锁住当前行为。
2. **删两启发式**:`application/extension_valve.py`(整文件)+ 其 3 个 state 字段 + runner 里的 offer/resolve 分支;`session_run.py` 的 `_update_turn_cost_ewma` / `_predictive_overshoot` + 相关字段/常量。**随源码整删 `tests/test_predictive_extension.py`**(559 行)。改行为=token 节奏,判分不变。
3. **抽 steering**:把 `session_run.py` 的每轮 steering 块搬到 `application/steering.py`(行为保持,run-loop 变成 precheck→call→handle→execute→autosave 从上读到下)。
4. **抽值对象**:`domain/session.py` 的 ~12 个 enforcement 字段 → 嵌套 `TurnEnforcementState`;`checkpoint=copy` 一行(原 8 行手抄);删死字段 `forced_unsatisfied`(写不读)。JSON 键不变。
5. **收 FSM**:4 优雅终态(`CANCELLED`/`BUDGET_EXCEEDED`/`STEP_LIMIT_EXCEEDED`/`CONTEXT_OVERFLOW`)→ `STOPPED(reason)`;删纯过渡 `SCHEDULED`。**判分读 `AgentRunResult.outcome` 非 phase,预算闸门须照常触发**。加 `from_dict` 兼容垫(第 1 步已备测试)。**顺手修终态误标 seam**:loop-block 被标 `STEP_LIMIT_EXCEEDED`、wind-down 成功被标 `BUDGET_EXCEEDED`(`session_run.py:755,801` 附近)——收成 `STOPPED(reason)` 后 reason 变显式,bug 自然消。
6. **瘦耦合测试**:`test_session_characterization` 37→~18(删钉内部接线的 5 个 + ~9 个 run-loop 重复,单份留 `test_session_run_loop.py`)。
7. **docstring**:enforcement 字段加 `=== per-turn ===` / `=== session-lifetime ===` banner;`_apply_enforcement_gate` 加"四触发→一 actuator"funnel 说明。FSM 表已干净,报告点名即可。
**解锁**:报告 §1(FSM)+ §5(eval-integrity 核)。

### Lane S2 · Scheduler / OS 模型
深层证据:`pattern-catalog.md` §3.3。
1. **单源 topology 谓词** `_topology_forbids(src,dst)`(`_scheduler_team.py:28` raise 与 `scheduler_messaging.py:46` 返错串共享判定,各留响应形状)。core-guarantee,反更稳。
2. **单源用户轮次+预算回滚事务** `_append_user_turn_txn`(`_scheduler_run.py` 与 `scheduler_messaging.py` 两份合一:checkpoint/try-add/except-rollback/finally-pop)。
3. **workflow.py 5 骨架 → Template Method**:`_run_agent`/`_run_enforced_agent`/`_run_structured_agent`/`_synthesize_dead_scout`/`_draft_findings_with_lease` → 一个 `_run_tracked_session(build_kwargs,*,run)`;eval 防御栈剥到 `application/workflow_agents.py`(mixin,`workflow.py` 回落 <800)。用 `test_workflow_context.py` 当契约网。
4. **统一术语**:`_reservation` / `_turn_budget` → 一个 "lease"。
5. **docstring**:两 scheduler 各一句"one of two Strategies driving `session.run_loop()`";`precheck()` 导读为 guard-chain;写 **OS 进程模型对照表**(Session=进程 / SCB=PCB / SessionTable=进程表 / spawn=fork / 预算=配额 / topology=IPC 权限 / PendingEventTable=就绪队列+唤醒)。
**别合并 mixin**(违反 many-small-files;`application.scheduler` 是唯一公共入口,0 外部 mixin import)。
**解锁**:报告 §2(OS 进程模型)+ §4(两 regime)。

### Lane S3 · Context / Shaping
深层证据:`pattern-catalog.md` §3.4。
1. **先加装配 golden 锚**:冻结 identity+team+task+skill+project+memory → 精确有序消息列表,走**生产路径** `system_prompt()` + `startup_user_messages()`(不是被删的 `messages()`)。
2. **删 lazy-loader 死脚手架**(byte-identical):`LoadTiming` 枚举 + `timing`/`visible`/`loader_key` 字段 + `messages()`/`deferred_sources()` + `context_builder.py:167-209` 的 3 个空 `ON_DEMAND` 源 + `ContextLayer.MEMORY`/`TOOL_META` 及其 `LAYER_PRIORITY` 行;`_startup()` 简化为 position+content 过滤。
3. **删 2 恒等 shaper**:`LowPriorityContextShedShaper`(可证恒空)+ `ContextCollapseShaper`(纯占位)及其 pipeline 槽和 exports;pipeline 7→5,每 rung 真触发。
4. **docstring**:`LAYER_PRIORITY` 加一句"今天只有 `PIN_FLOOR=70` 承重,sub-floor 排序等 deferred 源上线才激活"。**不建 ContextAssembler**(catalog 已否:给 1 常量 + 1 调用包 2 个类是负收益)。
**解锁**:报告 §3(Context engineering)。

### Lane S4a · Provider / Tools / Safety
深层证据:`pattern-catalog.md` §3.5、§3.6。
1. **抽 `_checked_path` helper**:`adapters/safety.py` 的 `check_path` 被 fs 工具 copy-paste;抽成一个共享 helper,**顺带补 `git_diff` 漏掉的路径牢笼**(seam #2)。
2. **`TaskTerminationResult`→bool**(3 个 prod caller 只读 `.terminal`)+ **7 处 timeout 校验归一**(升一个 stdlib-only 正-有限-timeout helper 到 `domain/`,各处 delegate,各留 None/inf/label 特判)。
3. **config 死分支删**:`config_resolve.py` 的 `_safe_int`/`_safe_bool` 二次强转(pydantic 已验)——**保 file-first key 优先级不动**。
4. **docstring**:`_is_green` 标为"positive-proof specification" + runner 分支 = proof-adapter Strategy;`kimi markup recovery` / `thinking passthrough` / `Usage` VO 各一句;抽 empty-turn rescue guard 为一个命名 rung。
**解锁**:报告 §6(provider 边界)+ §7 部分。

### Lane S4b · 持久化 / 观测
深层证据:`pattern-catalog.md` §3.7。
- **slim**:删 `autosave.py` 两个残留 compat 成员(`pending_write_futures` / `serialization_key`,若 Phase 1 没删)。
- **docstring**:`autosave` 的 freeze-then-flush 导读;`Tracer` 的 keyword-only `log_step` 说明;hooks 标"phase-1 observe-only(`HookOutcome.allow` deny seam 未建,报告不得声称 hooks 现在能拦工具)"。
**解锁**:报告 §7(观测/持久化)。

### Lane S4c · 边界 / SDK
深层证据:`pattern-catalog.md` §3.8。
- **slim**:**删 `sdk/` 的 7 个 0-ref shim 模块**(`agents`/`config`/`environments`/`lifecycle`/`persistence`/`repository`/`tracing`,-140)——先 `grep -rn` 确认全仓库 + **外部 eval 包没 import 子路径**(gate!);在 `test_sdk_boundaries` 加"退休 shim 须消失"断言。
- **docstring**:`sdk/__init__.py` 一句"`SDK_API_VERSION` 是 `test_sdk_api.py` 锁的兼容契约"。`ports`/`container`/fitness 测试已干净,报告点名即可。
**解锁**:报告 §8(边界为何可 fork)。

### 未来 lane 候选（本次 S1 讨论产出，超出行为保持瘦身范畴，另起 lane）

> 两条都是**结构性/行为相邻**改动,不满足「行为保持删除」的 S1-S4 铁律,故不塞进现有 lane;记于此不丢想法。

- **F1 · 可配置 brake registry**:把 `precheck()` 里硬编码的 guard-chain(cancel / loop-block / wind-down gate / budget / step)改造成一张**可注册、可逐个 toggle 的刹车表**,让「未来控制哪些刹车加/不加」成为声明式配置。**前置已由 S1 铺好**:S1 删掉 predictive+valve 两个 ad-hoc 启发式(wind-down 触发器瘦成「看门狗 OR 低产」两条)+ 把 14 个 enforcement 字段收进一个内聚 `TurnEnforcementState` box,未来做 registry 时面对的是干净的 2-触发漏斗 + 一个数据盒子,不背包袱。**代价**:新增一层抽象(registry/config),需权衡是否值得(small-and-beautiful:仅当"改动小收益大"才做)。
- **F2 · AUTOSAVING 折为 infra**:autosave 的**机制**已是 infra(EventBus 订阅 `step_end` 做 freeze-then-flush),但 `AUTOSAVING` 仍是一个 FSM 状态(单出边→PRECHECK)。可考虑把它折进转移、省 1 个状态(10→9)。**但**它精确标记「一个**有产出的** step 收尾」(spawn 挂起路径 `EXECUTING_TOOLS→AWAITING_EVENTS` 刻意跳过它,不 emit step_end),且是两条入边(正常工具 / 空转重试)的 DRY 汇合点 + restore 的干净落点(快照 phase 是恢复路标)。**倾向保留**:OC 立论是"checkpoint everything / 让不变量可见",一个**显式**的持久化 checkpoint 比散在转移里的隐式副作用更贴合哲学。收益(-1 状态)小、动到 restore 语义风险实。仅在"能显著简化且证得行为保持"时再动。

### Phase 3 · 写报告 prose
读 `docs/pattern-catalog.md`(§5=9 章 outline,§2=headline,§3=每章证据 file:line,§6=诚实○)+ 记忆 `project_adp_tech_report`。**framing 务必沿用**:两种控制模式(team / dynamic-workflow,single agent=team-of-one),**不说"三种"**;论点=operationalization + eval-integrity。用**新覆盖 12●/6◐/3○**(取代旧 9/8/4)。在分支 `docs/agentic-design-patterns-report`(`6b894c1`)续写。**跳过 Eval 成绩章**(等新数据)。英文 / arXiv 风格。可一章一会话,或一次写完。

---

## 每个会话的 kickoff prompt（复制即用）

> 模板固定,只换 Lane 名。spec 在本文件里,所以 prompt 只是"指针 + 护栏"。

**Phase 1（清场）:**
```
在 OpenCollab 项目继续既定重构。先读 docs/slimming-roadmap.md 的「铁律」+「Phase 1 · 清场」两段,严格照办。本会话只做 Phase 1 清场,一分支。每删一项先 grep 证明 0 引用。完成判据=cd opencollab && .venv/bin/python -m pytest -q 全绿 + .venv/bin/ruff check opencollab/ 绿。conventional commit 不 push,并在项目记忆 project_slimming_plan.md 标记 Phase 1 完成。开始。
```

**Lane S1 / S2 / S3 / S4a / S4b / S4c（把 Sx 换成对应名）:**
```
在 OpenCollab 项目继续既定重构。先读 docs/slimming-roadmap.md 的「铁律」+「Lane Sx」两段,再读 docs/pattern-catalog.md 里它引用的 § 作为深层证据。本会话只做 Lane Sx 这一个 lane,一分支,严格按 lane 内列出的步骤顺序(改行为的步骤先布 golden-master 网)。完成判据=cd opencollab && .venv/bin/python -m pytest -q 全绿 + .venv/bin/ruff check opencollab/ 绿 + tests/test_*_boundaries.py 绿。conventional commit 不 push,并在项目记忆 project_slimming_plan.md 标记 Lane Sx 完成。开始。
```

**Phase 3（写报告）:**
```
在 OpenCollab 项目写技术报告。读 docs/slimming-roadmap.md 的「Phase 3」段 + docs/pattern-catalog.md(§5 outline / §2 headline / §3 证据 / §6 诚实○)+ 记忆 project_adp_tech_report。在分支 docs/agentic-design-patterns-report 续写第 <N> 章,按 outline 铺 prose、每个论点引 catalog 的 file:line、覆盖用 12●/6◐/3○、framing 用"两种控制模式"、跳过 Eval 成绩章、英文 arXiv 风格。docs: commit 不 push。开始。
```

---

## 建议顺序

Phase 1(清场,半天)→ Lane S1(FSM,最大解锁,先攻心脏)→ S2 → S3 → S4a → S4b → S4c → Phase 3(路线甲则每 lane 完趁热写对应章;路线乙则最后集中写)→ Phase 4(合 main + md→LaTeX + Eval 数据到位补)。各 lane 尽量从最新 `main` 切分支,后面的 lane 就能看到前面的清理。
