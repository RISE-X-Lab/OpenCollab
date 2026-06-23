# analyst-solve 能力攻坚:探索全记录与问题清单（2026-06-23）

> 目标：提升 `analyst-solve` SWE-bench 工作流在硬题上的解决率，定位并修掉一个个具体的失败机制。
> 本文记录本轮（接续 batch-1 鲁棒性、batch-2 empty-stop 之后）的探索链路、每个问题**具体到 file:line 的根因**、修复、以及实测结果。
>
> 涉及代码：包根 `opencollab/opencollab/`，工作流 `workflows/analyst_solve.py`，SWE-bench 接线 `swebench/gen_prediction_workflow.py`，评测 harness `opencollab/opencollab/harness/`。
> 分支：`feat/analyst-solve-workflow`。

---

## 0. TL;DR — 问题清单与状态

| # | 问题（具体机制） | 根因位置 | 修复 | 状态 |
|---|---|---|---|---|
| P1 | flask 是**覆盖率丢失**：FAIL_TO_PASS 测试体从未到达 agent，tester 靠判断/`python -c` 自证 | `run_eval_task` 只传 `description`；`EvalTask` 无 test_patch 字段 | inject-f2p：注入真测试 | ✅ 已修并验证 |
| P2 | `run_tests` 信号被 warnings 污染、无法证明某条**具名**测试变绿 | `adapters/tools/run_tests.py` 只列失败 + 聚合 `passed=N`，warnings 混入 counts | runtests-clean-signal | ✅ 已修 |
| P3 | tester 的 PASS 不基于真实测试 | `analyst_solve.py` TESTER_PROMPT/verdict 仅自报 | tester-real-pass + `_f2p_gate` | ✅ 已修（按你的决策保留"信任自报"） |
| **P4** | **致命：注入的测试泄漏进提交补丁 → 评分器双重 apply 冲突** | diff-exclusion `git checkout` 对未跟踪新文件失败 | 逐路径 `git checkout` + `git clean -fq` | ✅ 对抗 review 抓出并修复，真实例验证 |
| P5 | 注入失败时 `fail_to_pass` 仍下发 → gate 要求不存在的测试 → 全跑挂 | `evaluator.py` 无条件 `args.update(extras)` | 注入失败则不下发 `fail_to_pass` | ✅ 已修 |
| **P6** | **系统性：analyst→recon→plan 招牌流程几乎从不启动,全走单隐式 phase** | `_run_structured_agent` 漏传 `tool_choice` → schema 调用跑在 `"auto"` → 模型自由文本收尾不调 `structured_output` | 矫正轮强制收口 + 带上探查上下文 | ✅ 已实现（**未提交**），live 验证被中断 |
| P7 | structured 修复初版**丢了第一轮探查上下文**（建全新空 session） | 首版 `_forced_structured_commit` 用 `build_workflow_session` 起空会话 | `_carry_exploration` 复制首轮消息 | ✅ review 抓出并修复 |
| P8 | 1800s 超时把 pylint / django 在收敛前砍断 | harness `--timeout 1800` + 无 per-LLM-call 超时 | 未动 | ⬜ 待处理（次级瓶颈） |
| P9 | "tester died → substituting generic findings" 老毛病仍在 | 结构化 tester 子代理偶发无 verdict | 未动（本轮有界、未致假阳性） | ⬜ 待处理 |

**两个里程碑结果：**
- **verify-rigor 批**：已提交 `e349bde`（778 测试绿）；**flask-4045 首次 RESOLVED**（FAIL_TO_PASS 2/2、PASS_TO_PASS 50/50、补丁只碰 `src/flask/blueprints.py`）。
- **structured 路径修复**：已实现 + 对抗 review，**783 测试绿**，**未提交**；live 重跑验证（run_id `analyst-sf`）被手动中断，仅得一个早期正信号（pylint 这次 plan 未回退）。

---

## 1. 背景与方法

`analyst-solve`（`workflows/analyst_solve.py`）是 analyst 驱动的混合工作流，五阶段：
`scope`(分解 2–4 个 recon 维度) → `recon`(并行只读 scout) → `plan`(综合出 `{root_cause, approach, phases}`) → `implement`(每 phase coder/tester 循环，`MAX_ROUNDS_PER_PHASE=4`，失败继续) → `verify`(整目标校验 + 1 轮修复)。
预算地板：`RESERVE_TOKENS=350_000`，低于则进 forced-write 强制落补丁。

本轮方法：**清亮主上下文 + 多代理委派**——所有噪声型的代码勘察、实现、对抗验证、SWE-bench 实跑都放进后台 workflow / Agent，主线只接收蒸馏结论。对每个改动都做**多视角对抗 review**（正确性 / 架构边界 / 过拟合 / diff 污染等）再采信。

测试基线：`cd opencollab && .venv/bin/python -m pytest -q`（起点 751 → 现 783）。

---

## 2. verify-rigor 批：让 tester 基于"真实测试"通过

### 2.1 诊断（为什么 flask 第二个 case 之前拿不下）
flask-4045 之前是**部分修复**：coder 只挡了 Blueprint **名字**里的点（`blueprints.py __init__`），漏了 `add_url_rule` 的 **endpoint 带点**这一例（FAIL_TO_PASS 1/2）。根因不在能力，而在**验证闭环断裂**：

- `run_eval_task` 只把 `description` 喂进工作流。FAIL_TO_PASS 只是**文字节点名**，真正的测试体（SWE-bench 实例的 `test_patch`）**被整个丢弃**——`EvalTask` 连存它的字段都没有。
- 于是 tester 只能靠判断 + 临时 `python -c` 片段"自证",而不是跑那条真正失败的测试。
- 叠加 `adapters/tools/run_tests.py` 的信号问题:只列**失败**节点 + 聚合 `passed=N`(无法证明某条**具名**测试变绿),并把 `warnings=N` 混进同一个 counts —— 模型把 deprecation warning 误读成失败/噪声(flask/werkzeug 里极常见)。

### 2.2 三处改动（推荐顺序）
1. **`runtests-clean-signal`**（`adapters/tools/run_tests.py`，仅 adapter，不动 port）
   - 命令加 `-rA`（逐测试摘要含 PASSED）+ `-p no:cacheprovider`（确定性）。
   - 新增 `_passed_tests()` 列**具名 PASSED**节点；只在**聚焦某 target**时列、上限 25 条防爆 context；全套跑不列。
   - `_parse_counts` 把 warnings 从判定 counts 摘出,单列 `"Warnings: N (not failures)"`;判定只看 exit code + failed/error。
2. **`inject-f2p`**（决策 **D1 = 注入整份 test_patch + diff-exclusion**）
   - `harness/evaluator.py`：`EvalTask` 加**通用透传** `extras: dict | None`（不是 SWE-bench 专用字段）。
   - `swebench/gen_prediction_workflow.py`：抽出实例的 `test_patch` + 解析的 FAIL_TO_PASS ids → `extras`。
   - 新 `harness/test_injection.py`：`apply_test_patch(env, patch)` 跑前 `git apply`（回退 `patch -p1`），返回触及文件；失败返回 `[]` 不抛（坏补丁绝不中断整跑）。
   - `analyst_solve.py`：把 node-ids 织进 SCOPE/PLAN/CODER/TESTER 提示——**只给行为描述、绝不给断言字面值**（反过拟合）；非 SWE-bench 跑为空、提示不变。
3. **`tester-real-pass`**（决策 **D2 = 只硬卡 FAIL_TO_PASS**）
   - VERDICT_SCHEMA 加机器可校验字段 `tests_run` / `failed_count`。
   - TESTER_PROMPT 强制用 `run_tests` 跑具名节点、**禁止 `python -c` 自证**。
   - `_f2p_gate`：tester 报 PASS 但 `failed_count>0` 或缺具名节点 → **覆盖为未通过**（镜像已有的 diff-guard 覆盖）。
   - **条件触发**：`fail_to_pass` 为空（注入失败/不可用）→ 旁路，保留旧行为。PASS_TO_PASS 仍作 verify 阶段软信号。

### 2.3 P4 —— 对抗 review 抓到的致命污染 bug（最重要的一条）
**4 个视角独立、用真 git 复现:** diff-exclusion 当时是 `git checkout -- <paths>`。但——

- `git checkout --` **只能恢复 HEAD 里已跟踪的文件**。SWE-bench 的 test_patch **极常新增**测试文件(`new file mode`),`git apply` 后是**未跟踪**的;`git checkout -- <新文件>` 报 `pathspec did not match`(rc=1),文件**留在工作树**。
- 而且**一个新文件会让整条多路径 checkout 全部失败**(git 拒绝整个 pathspec 列表),连带已跟踪测试文件的注入改动也没被回滚。
- 真正提交的补丁不是 `result.patch`——production 走的是 `gp.extract_patch` = **`git add -A && git diff --cached`**(`gen_prediction.py`),会把残留的注入测试**暂存并写进 model_patch**。
- 后果:评分器在 model_patch 之上再 apply 自己的 test_patch → **双重 apply 冲突 → 实例判未解决**。正是 D1 要防的污染。
- 当时单测把 `git checkout` mock 成"永远成功"且只测改已存在文件,所以 **778 全绿却藏着这个 bug**。

**修复**：逐路径 `git checkout -- p` + `git clean -fq -- p`（删未跟踪新文件）；补新文件 / 混合文件回归测试（对旧实现会 fail）。
**并修 P5**：`fail_to_pass` 现在只在 `injected_paths` 非空（注入成功）时才下发,否则 gate 退回信任路径,不会要求不存在的测试。

### 2.4 决策（你拍板）
- 注入**整份 test_patch**（忠实评分器）而非外科式单函数。
- **只硬卡 FAIL_TO_PASS**（不卡 ~50 条 PASS_TO_PASS）。
- **不上机器闸,信任 tester 自报**——`_f2p_gate` 读 tester 自报的 `tests_run/failed_count`,不回解析 `run_tests` 真实输出（残留:幻觉 tester 理论上可填 `failed_count=0` 过闸;"machine-checkable proof"措辞略强,按你的选择保留）。

### 2.5 结果
- 提交 **`e349bde`**(11 文件 +1288/−32),套件 **778** 绿,ruff + 边界 5/5。
- **flask-4045 RESOLVED**(run_id `analyst-vr`,thinking ON,~9 min):
  - FAIL_TO_PASS **2/2**(含目标 `test_route_decorator_custom_endpoint_with_dots`),PASS_TO_PASS **50/50**,零回归。
  - 补丁 1394 字符,**只碰 `src/flask/blueprints.py`**;注入的测试**完全不在补丁里** → P4 修复**真实例端到端验证通过**。
  - 是**根因修复**(校验名/端点/视图函数名里的点),非教到测试。
  - inject-f2p 触发:`run_tests` 用精确 F2P 节点名跑了 7 次(base commit 不存在这些测试,只能因注入才跑得起来);`_f2p_gate` **0 次 override**(真跑绿的)。

---

## 3. 更广 smoke：verify-rigor 安全性 + 暴露真瓶颈

5-task 逻辑集(run_id `analyst-vr-smoke`,@ e349bde):

| 实例 | resolved | F2P | P2P | 结束 | 分类 |
|---|---|---|---|---|---|
| pallets__flask-4045 | **Y** | 2/2 | 50/50 | 干净,phase0 一轮 | resolved |
| pylint-dev__pylint-6506 | N | 0/2 | 6/6 | 1800s 超时 | 能力差 + 超时 |
| sympy__sympy-11400 | N | 0/2 | 29/29 | 4 轮 tester died | 能力差 |
| django__django-11564 | N | — | — | 1800s 超时→空补丁 | infra/超时 |
| psf__requests-2148 | N | — | — | 无网络/httpbin 503 | 环境锁死 |

**结论：1/5（vs 之前 0/5），零回归。** verify-rigor 既没广帮也没伤：
- `_f2p_gate` 在 5 个 session 里**一次都没 override**,`MAX_ROUNDS_PER_PHASE=4` 限死无死循环路径。
- **diff-污染抽查 5/5 全过**(flask→blueprints.py、pylint→lint/run.py、sympy→printing/ccode.py、另两个空)。
- flask **复现**(2/2,这次更干净:1 轮、无 tester died)→ 不是侥幸。
- 伤害来自**超时**,不是 gate。

**但浮出真瓶颈(P6):** flask/pylint/sympy/django **全部**只跑了"隐式单 phase",日志反复 `recon skipped — analyst produced no dimensions` / `planner produced no usable plan — falling back to a single implicit phase`。**招牌的 analyst 分解→并行 scout→分阶段计划根本没启动**;flask 是靠回退路径赢的,不是健康流水线。

---

## 4. P6 根因诊断：structured 路径漏传 tool_choice（高置信）

只读 4-代理诊断工作流(代码 + 轨迹双证):

**根因:** schema 绑定的 workflow agent **从不强制模型走 `structured_output` 工具**。
- `application/workflow.py` 的 `_run_structured_agent`(~230-282)调 `build_workflow_session` 时**漏传 `tool_choice`**(对照:非结构化的 `_run_agent:214` 是传的)→ schema 调用跑在 `tool_choice="auto"`。
- `auto` + thinking ON 下,kimi-k2.6 常常先 grep/file_read 探一探,然后**直接用自由文本收尾**(`finish_reason="stop"`、有 content、**零 tool_call**),**从不调 `structured_output`**。
- `session_run.handle_pending_response` 把"stop + 有内容"当正常 **DONE**(注意:这**不是** empty-stop —— `empty_stop_retry` 仅在"既无 content 又无 tool_call"时触发,见 session_run.py:~302-303;这里有内容,所以 batch-2 的网兜不接)。
- 仅有的矫正重试(`_STRUCTURED_RETRY`,workflow.py:~277)是**纯文本**的,结果一样 → `capture_tool.captured` 一直 None → `ctx.agent(schema=)` 返 None。
- `analyst_solve.py` 的 scope(~605,`DIMENSIONS_SCHEMA`)收到 None → "recon skipped"(~618);plan 收到 None → "no usable plan"(~638)。

**关键:per-call 随机,非 per-instance。** 轨迹证据:flask/django 的 scope 自由文本(失败)、pylint/sympy 的 scope 调了 structured_output(成功);**但 pylint 的 PLAN 阶段又自由文本了**(尽管 scope 成功)。⇒ 修复**必须同时覆盖 scope 和 plan 两个 `ctx.agent(schema=)` 点**(它们都走 `_run_structured_agent`,单点即可)。

**被推翻的错误假说:** 另一诊断 agent 提出"structured_output 被空 `{}` 调用",综合时用日志**推翻**——失败跑里该工具调用次数 **=0**,不是空调用。

**附带发现:** `workflow.py:179-180` docstring **错误**声称 tool_choice "在 structured 路径被忽略(已 pin 住 structured_output)"——实际什么都没 pin。

---

## 5. P6 修复 + P7（review 抓出的上下文丢失）

**修复（`application/workflow.py` `_run_structured_agent`，集中改动）：**
1. **首轮不变**：`[capture_tool, *tools]` + 默认 `auto`,保住自由探查能力。
2. 若未捕获，新的矫正轮 `_forced_structured_commit`：会话**只留 `[capture_tool]` + `tool_choice="required"`**——单工具下 `required` 只能落到 `structured_output`,**强制收口**。
3. `required` 在 DashScope **已知可用**(batch-1 forced-write 就用它),且 session_run 有 **400→auto 兜底**(~508-526),优雅降级。
4. 轻量加固 `_schema_satisfied()`:缺捕获 / 非 dict / 缺 schema required 顶层键 → 视为未命中(触发强制轮)。
5. 修正第 4 节那条错误 docstring。

**P7 —— 对抗 review(4 视角)抓到 1 个 HIGH:** 矫正轮初版建的是**全新空 session**(`build_workflow_session` 起的是无种子消息的空会话),把**第一轮探查上下文全丢了** → schema 基于"啥都没探查到"硬凑 → 低质/幻觉。虽消除了 None(recon 会跑),却牺牲了这个修复本要救的**结果质量**。
**修复:** `_carry_exploration` 把首轮消息复制进矫正会话;加**负控测试**(去掉该调用测试就 fail,防止空测试)。

**结果:** 套件 **783** 绿,ruff + 边界 5/5,**未提交**。剩低优 nit(提交前清):docstring 仍写"same session"/"guarantee"措辞偏强(`required` 只保证**发生调用**,不保证参数合法);`_schema_satisfied` 的 required-key 分支对真实捕获是死防御。

**live 验证(run_id `analyst-sf`)被手动中断**,仅得早期正信号:**pylint 这次在 plan→implement 过渡、未触发 plan-fallback**(此前 pylint 的 plan 会回退)——暗示 structured 修复生效,但**未跑完,不算定论**。

---

## 6. 次级瓶颈（已识别，未动）

- **P8 超时**:`--timeout 1800` 把 pylint、django 在收敛前砍断(django 因此空补丁)。候选:抬 per-task timeout + 加 per-LLM-call 超时护栏。
- **P9 "tester died → substituting generic findings"**:结构化 tester 子代理偶发无 verdict;本轮有界、且因 `_f2p_gate` 0-override 未致假阳性,但仍是鲁棒性隐患。
- **轨迹文件按实例追加跨多次 run**(`logs/eval_workflow/trajectories/<instance>.jsonl`),分析需按时间窗/ run_id 切分——建议改 per-run 文件。
- **psf__requests-2148 离线不可评分**(测试依赖网络,gold 补丁也救不了)→ 20-unresolved 里可能还有若干是环境锁死而非能力问题,选 smoke 应挑纯逻辑题。

---

## 7. 当前代码与产物状态

**提交：**
- `4b7aaed` —— batch-2(empty-stop 重试 + reasoning 入轨迹),已提交未 push。
- `e349bde` —— verify-rigor 批(P1–P5),已提交未 push,778 绿。

**未提交（工作树）：** P6/P7 structured 路径修复(`application/workflow.py` + `tests/test_workflow_structured_output.py`),783 绿。

**分支** `feat/analyst-solve-workflow` **整体未 push**。

**实跑产物（`/home/xuzhenhua/swebench-eval/`）：**
- `analyst-vr`：flask 单例 → RESOLVED。
- `analyst-vr-smoke`：5-task → 1/5。
- `analyst-sf`：structured 修复重跑 → **被中断**(可恢复)。
- 脚本 `run_analyst_flask_vr.sh` / `run_analyst_vr_smoke.sh` / `run_analyst_sf_smoke.sh`。

**待办（建议优先级）：**
1. 跑完 `analyst-sf`(或重启)→ 确认 P6 修复让 recon/plan 真起来、且 resolve 不回退(理想是涨)。
2. structured 修复提交前清掉 §5 的 docstring nit。
3. 决定 P8 超时处理(直接影响 pylint/django)。
4. 决定何时 push 分支。

---

## 8. 关键文件:行 速查

| 关注点 | 位置 |
|---|---|
| 工作流五阶段 / scope / recon-skip / plan-fallback | `workflows/analyst_solve.py` ~605-610 / :618 / :638 |
| `_f2p_gate` / VERDICT_SCHEMA / TESTER_PROMPT | `workflows/analyst_solve.py`（tester-real-pass 段） |
| schema agent 分发 / 漏传 tool_choice / 错误 docstring | `opencollab/opencollab/application/workflow.py` :158-195 / :258-264 / :179-180 |
| 矫正轮强制收口 / 上下文带回 | `application/workflow.py` `_forced_structured_commit` / `_carry_exploration` |
| empty-stop 判定 / stop+content→DONE / required 400→auto 兜底 | `application/session_run.py` :302-303 / :328-330 / :508-526 |
| schema 调用走 auto | `adapters/llm/openai_provider.py` :37 |
| 测试注入 + diff-exclusion(P4) | `harness/test_injection.py` / `harness/evaluator.py` diff-exclusion 段 |
| 真正提交补丁的抽取(git add -A && git diff --cached) | `swebench/gen_prediction.py`（`extract_patch`） |
| run_tests 信号(P2) | `adapters/tools/run_tests.py` `_passed_tests` / `_parse_counts` / `_build_command` |
