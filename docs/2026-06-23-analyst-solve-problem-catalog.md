# analyst-solve 问题清单（统一汇总）

> 生成 2026-06-22 / 2026-06-23。分支 `feat/analyst-solve-workflow`。
> 来源：通读项目记忆 `project_analyst_solve_workflow.md` + flask/django/pylint 深挖根因 + sympy/requests 轨迹挖掘，去重合并。
> 已提交基线：`4b7aaed`(空轮重试 + reasoning 入轨)、`e349bde`(verify-rigor：注入真 FAIL_TO_PASS + tester gate + diff-exclusion)。
> 未提交（工作树）：recon/planner 强制结构化提交修复（`workflow.py` + 测试，suite 783）。

## 状态图例
- ✅ **已修**：已落 commit。
- 🟡 **部分**：已缓解但未根治。
- 🔴 **未动**：尚未处理（或 triage）。

## 总览

| ID | 问题 | 层 | 严重 | 状态 |
|----|------|-----|------|------|
| P1 | 空轮(stop+无内容+无工具)被当成 DONE | runner/tooling | high | ✅ `4b7aaed` |
| P2 | tester 假 PASS（没跑真 FAIL_TO_PASS 测试） | workflow | critical | ✅ `e349bde` |
| P3 | 注入测试泄漏进 model_patch（未跟踪新文件） | harness/eval | critical | ✅ `e349bde` |
| P4 | recon/plan 散文 stop，从不调 structured_output | workflow | high | ✅ 工作树(未提交) |
| P5 | tester 死了就用 generic 结论顶替（非 fail-closed） | workflow | high | 🟡 部分(P6+P4 后缓解) |
| **P6** | **kimi 把工具调用标记当正文吐出（上游元凶）** | provider/解析 | high | ✅ 工作树(未提交) |
| **P7** | **1800s 硬截断 + forced-write 只认 token、对墙钟瞎** | harness/eval | critical | ✅ 工作树(未提交·含残留收口) |
| P8 | 强制提交把工具裁到只剩 structured_output → 后续 unknown_tool | workflow | high | 🟡 部分(P6 落地后大体自消) |
| P9 | run_tests 写死 pytest；非 pytest 测试床跑不了真测试 | runner/tooling | high | ✅ 工作树(未提交) |
| P10 | 诊断对了却写不出正确补丁（模型能力） | model-capability | high | 🟡 部分 |
| P11 | 没有 str_replace；整文件写易碎 → 退回 bash sed | runner/tooling | medium | 🔴 未动 |
| P12 | loop 检测误杀"改完再验"的合法重跑 | runner/tooling | medium | 🟡 部分 |
| P13 | 联网任务离线不可评分（非缺陷，需 triage） | environment | medium | 🔴 triage |
| P14 | 轨迹 JSONL 跨 run 累加，需按 run 切分 | harness/eval | low | 🟡 部分 |

---

## 详细条目

### P1 — 空轮被当成 DONE　✅ 已修 `4b7aaed`
- **实例**：flask-4045（每 run ~12–20 次空轮，已重试）、django-11564、sympy-11400（tester died ×4）
- **症状**：LLM 返回 `finish_reason=stop` 且 **0 内容 + 0 工具调用**；会话循环当成干净 DONE。自由文本 coder → 空摘要;schema tester → 从不调 structured_output → 捕获 None → "tester died"。长会话里由 thinking 诱发（flask A/B：thinking OFF 时空轮 12→0）。
- **根因**：`session_run.handle_pending_response` 无"既无内容又无工具"检测，落到正常终止路径。
- **修复**：`4b7aaed` —— 检测空轮并每轮重试一次，经新 FSM 边 `HANDLING_RESPONSE→AUTOSAVING` + nudge（角色交替安全占位）；`_empty_stop_retried` 每轮重置;记 `empty_stop_retry` 轨迹步。flask-b2 实测跑满 149 步、无静默 DONE。

### P2 — tester 假 PASS / verify-rigor 缺口　✅ 已修 `e349bde`
- **实例**：flask-4045（决定性——修后才赢）、sympy-11400（退化成 grep-only 结论）、django-11564
- **症状**：tester 用自己的 `python -c` 复现就报 PASS,或把 22 条 run_tests 失败当"Werkzeug 环境噪声"忽略 → 真正失败的测试从未被抓/修;warnings 污染信号。
- **根因**：phase-passed/run-done 只看 tester 自报;TESTER_PROMPT 允许自证;run_tests 把 warnings 混进判定;且 FAIL_TO_PASS 测试在 base_commit 常不存在(由评分 test_patch 加入)→ 无法运行。
- **修复**：`e349bde`（verify-rigor）：① `harness/test_injection.py` 跑前注入 test_patch 使 F2P 测试可运行;② VERDICT_SCHEMA 加 `tests_run`/`failed_count`,TESTER_PROMPT 强制跑具名测试、禁 `python -c`,`_f2p_gate` 在 failed_count>0 或缺节点时推翻 PASS;③ run_tests `-rA` + `-p no:cacheprovider`,warnings 单列。**残留(按用户决定)**:gate 是 LLM 自报,不上机器闸——会幻觉的 tester 填 failed_count=0 仍可能过。

### P3 — 注入测试泄漏进补丁（新文件 bug）　✅ 已修 `e349bde`
- **实例**：所有带"新增测试文件"的 test_patch 实例（在 flask-resolve 前抓到）
- **症状**：注入的 SWE-bench 测试文件漏进 model_patch → 评分器双重 apply 冲突 → 未解决。被"测试里把 git checkout mock 成永远成功"掩盖。
- **根因**：diff-exclusion 用 `git checkout -- <paths>`,对**未跟踪新文件失败**,且一个新文件令整条多路径 checkout 全部回滚失败;真实 driver `gp.extract_patch` 用 `git add -A && git diff --cached` 把泄漏的测试捕获进补丁。
- **修复**：`e349bde` —— 逐路径 `git checkout -- p` + `git clean -fq -- p`(删未跟踪);新文件/混合文件回归测试(对旧实现 fail);注入失败时 `fail_to_pass` 不下发。flask-vr 端到端验证:提交补丁只动 blueprints.py、无 tests/。

### P4 — recon/plan 散文 stop（free-text-stop-B）　🟡 未提交
- **实例**：flask-4045（靠 fallback 仍赢）、pylint-6506（scope 泄漏 step13 + plan）、sympy-11400、django-11564
- **症状**：`tool_choice=auto` + thinking 下,kimi 探完后**用散文回答**(stop、有内容、0 工具)→ 从不调 structured_output → 捕获 None → "recon skipped / no usable plan → 单隐式阶段"。招牌的 analyst 分解→scout→分阶段对非平凡任务基本不触发。
- **根因**：`_run_structured_agent` 调 `build_workflow_session` **没传 tool_choice**(auto),不同于 `_run_agent`;纠正重试是同会话纯文本、只会重复。逐调用随机(scope、plan 各自独立中招)。
- **修复(未提交,suite 783)**：`_forced_structured_commit` 建只含 `[capture_tool]` + `tool_choice="required"` 的纠正会话,经 `_carry_exploration` 带入首轮探索。**线上验证:优雅非保证**——**DashScope 400 拒 `required`**(django 7×、pylint 6×)→ 退 auto;靠"只给一个工具"间接逼,提高概率但挡不死铁心散文(pylint scope、django tester 仍漏)。
- **下一步**：纠正轮加显式"必须调 structured_output、别散文";探 DashScope 是否吃 named-function dict;docstring 改"优雅非保证";**先修 P6 再提交**(见 P8 依赖)。

### P5 — tester 死了顶替 generic 结论　🟡 部分
- **实例**：sympy-11400（4 个退化结论）、flask-vr（1 次"tester died — substituting generic findings",靠 f2p gate 才安全）、django-11564（round-1 tester died）
- **症状**：tester verdict 未捕获时,workflow 记 "tester died — substituting generic findings" 并以 grep-only 退化结论(tests_run=[]、failed_count=0)继续。
- **根因**：与 P4 同族(tester verdict 调用处散文 stop)+ 一个"编造 generic verdict"的兜底,而非把"无证据"当 FAIL。tester-death 本身 fail-CLOSED,但 generic 顶替仍发生、掩盖真实状态。
- **修复**：由 f2p gate(e349bde)间接缓解(仅当注入了 f2p 且 gate 触发时,编造 PASS 才不安全)+ 空轮重试(4b7aaed)+ P4 强制。**开放核心**:把 P4 强制扩到 implement 阶段的 tester verdict;有 f2p ids 却无证据时硬判 FAIL,取消 generic 顶替;schema agent 限/关 thinking 以缩短死亡生成。

### P6 — kimi 工具调用标记当正文吐出　🔴 未动　**【上游元凶】**
- **实例**：sympy-11400（11 次 PROSE-STOP：steps 10,22,28,48,62,75,93,100,125,148,155）、requests-2148（4×：11,13,74,134）、pylint-6506（scope step13 → recon skipped）
- **症状**：模型把 kimi 控制 token 标记 `<|tool_calls_section_begin|><|tool_call_begin|>functions.grep:6<|tool_call_argument_begin|>{...}<|tool_call_end|>` 当成**纯正文 content** 吐出,`finish_reason=stop`、0 解析出的 tool_calls。harness 看作散文 stop,目标工具从未执行。**这是把 recon 变 "recon skipped"、并级联出 unknown_tool(P8)的总上游触发器。**
- **根因**：`openai_provider` 适配器对 kimi(DashScope 兼容模式)**没有把这种特殊 token 工具调用块解析进 tool_calls**——`adapters/llm/` 里没有任何针对这些分隔符的解析器(grep 确认无)。
- **修复**：在 `openai_provider._parse_response`:当 content 含 kimi 特殊 token 工具调用分隔符且 tool_calls 为空时,从 `<|tool_call_begin|>functions.NAME:ID<|tool_call_argument_begin|>{...}<|tool_call_end|>` 抽出函数名 + JSON 参数,合成 tool_calls 数组(并从 content 清除标记)。单层 provider 修复,可救所有下游 schema agent、阻断 P8 级联。**开放项里最高杠杆(上游)。**

### P7 — 1800s 硬截断 + forced-write 对墙钟瞎　🔴 未动　**【heavy 任务头号阻塞】**
- **实例**：django-11564（决定性——被中途杀死）、pylint-6506（implement 中途撞 1800s,冻住错补丁）
- **症状**：django:recon 健康,coder 跑了真 F2P 测试(见 FAIL)、定位到**确切修法**(在 `django/conf/__init__.py` 给 MEDIA_URL/STATIC_URL 加 `get_script_prefix()`),step 114 reasoning 原话 "OK, let me just use file_write"——但 1800s 墙先到,杀在任何写入前。forced-write 没触发(它看 `remaining()>RESERVE_TOKENS`,只用了 643k/5M)。被杀时:django=无补丁,pylint=错补丁冻盘。
- **根因**：①`_budget_ok`/forced-write 触发只认 token,无 deadline 感知;②`asyncio.wait_for` 打断不了单条在飞的 595s thinking 生成,取消只在 await 间落地=实际硬截断;③thinking-ON 的 tester 死亡生成巨慢(单条 595s、8.5 万字 reasoning;finish=stop-无工具 延迟占 django 墙钟 61%)。
- **修复**：forced-write **墙钟感知**——把 start-time/deadline(T−120s)传入 WorkflowContext,临界时触发 forced-write coder 落地当前最优补丁,独立于 token 预算。schema agent 限/关 thinking + 加 per-LLM-call 超时,使单条 595s 生成吃不掉 61% 墙钟。与 P4/P6 并列为 heavy 逻辑任务主阻塞。

### P8 — 强制提交把工具裁到只剩 structured_output → unknown_tool　🟡 部分
- **实例**：sympy-11400（11 次 unknown_tool：steps 12,50,52,77,79,102,104,127,128,157,159）、requests-2148（verify step135–138 file_read 被拒 unknown_tool,此前已成功 10+ 次）
- **症状**：标记泄漏/散文 PROSE-STOP 被当成 miss 后,纠正轮把工具集裁到只剩 capture tool + `tool_choice=required`。模型接下来的真实 grep/file_read/bash 返回 `error=unknown_tool`,之后只能调 structured_output、过早 FAIL/BLOCKED。verdict 文本("only structured_output tool remained available")是对 harness 状态的**如实描述**,非幻觉。
- **根因**：P4 纠正设计有意把工具集裁到 `[capture_tool]`;当它被标记泄漏(P6)**误触发**(而非真正探索结束)时,把 agent 的真实工具中途砍掉。与 P6 互相放大。
- **修复**：**先根治 P6**(解析标记,纠正轮就不会被误触发);另:仅在真正 end-of-budget/end-of-rounds 触发单工具纠正,或在强制 structured_output 时**保留只读工具集**(grep/file_read/run_tests)而非裁成 capture-only。P6 落地后大体自消。

### P9 — run_tests 写死 pytest；非 pytest 测试床跑不了　🔴 未动
- **实例**：sympy-11400（无 pytest,run_tests 在 steps 64,65,111,112 报死）、pylint-6506（Summary "could not parse" → 无清爽通过/失败）
- **症状**：每次 run_tests 跑 `python -m pytest ...`,sympy 测试床报 'No module named pytest' / exit 1(sympy 自带 bin/test)。F2P 测试存在且已注入却无法执行,tester 无 ground-truth → grep-only。另:pylint 的 Summary 显示 "(could not parse)",coder 从没拿到清爽 GREEN/RED。
- **根因**：`DEFAULT_RUNNER='python -m pytest'` 无项目原生 runner 探测;summary 行非 pytest 形态时 `_format_report` 落 "(could not parse)"。模型可覆盖 runner 但很少用,且 pytest 节点 id 不映射到 bin/test。
- **修复**：探测项目原生 runner(sympy bin/test、tox、manage.py test)并把 F2P 节点 id 翻成原生调用;或见 'No module named pytest' 自动回退。即使无 pytest summary 行也给**可解析的 GREEN/RED + 缺失子串 hint**;同一断言连挂 N 次则升级/换写法。

### P10 — 诊断对了却写不出正确补丁（能力）　🟡 部分
- **实例**：pylint-6506（决定性·真能力不足）、sympy-11400（空补丁,patch_produced=false）、psf（早期 smoke,已被行为改动修)
- **症状**：模型正确诊断根因与确切修法却从不写。pylint:跑了真 F2P 测试、看着 `assert 'usage: pylint' in ''` 挂 56 次/~125 步、round-3 自诊 "only sys.exit(32)",却 ship 了 `except _UnrecognizedOptionError: sys.exit(32)`(文件对、语义错——满足 SystemExit 但 stderr 空;测试要 argparse usage 打到 stderr)。sympy:结构化 approach 与 gold `_print_sinc`/Piecewise 一致但树为空。
- **根因**：两支:(a) 较难语义的真能力天花板(pylint stderr 行为)——加墙时间无用,无收敛;(b) 早期"coder 不写"是行为缺口(tool_choice 长期 auto、从不强制),写工具其实给了。
- **修复**：由 P1(WorkingTreeProbe diff-gate + 空树强制写)+ CODER_PROMPT"确认目标后首动作=file_write"缓解。真能力部分非 workflow 可修;部分缓解=把缺失子串当 actionable hint(P9)+ 同一失败重复时强制换写法。

### P11 — 没有 str_replace;整文件写易碎 → bash sed　🔴 未动
- **实例**：requests-2148（file_write unknown_tool step29-30 → bash python -c replace step34）、flask-4045（整文件 file_write → invalid_json_args → bash sed）
- **症状**：整文件内容的 file_write 偶发 `invalid_json_args`,coder 退回易错的 bash sed / `python -c content.replace()` heredoc;偶尔还调不存在的工具名得 unknown_tool。即使最终 diff 对,也是反复出现的能力缺口。
- **根因**：工具集有 FileWriteTool + ApplyPatchTool 但**无定向 str_replace/edit 原语**;大块整文件 JSON 参数脆弱,逼模型每次都 bash 改文件。
- **修复**：给 coder 加一流的定向 str_replace/edit 工具(old_string/new_string 精确替换),并把 CODER_PROMPT 引向定向编辑而非整文件重写;加同字节 str_replace no-op 守卫(fs.py)。

### P12 — loop 检测误杀"改完再验"的重跑　🟡 部分
- **实例**：requests-2148（loop_blocked steps 114,122,124 → 1 轮后 BLOCKED）
- **症状**：loop 检测把合法的 F2P 测试重跑(run_tests ×2,count 3,4)和 models.py 重读当成 thrash,在模型试图分辨"是代码错还是网络挂"时把阶段推到 BLOCKED。
- **根因**：检测器把重复相同工具调用计为 thrash,不区分"改后验证重跑"与"真循环停滞"。(历史:此前只扫短窗口,全窗口扫已入 `0f18d86` 合 main。)
- **修复**：豁免"前面有写/编辑介入"的 run_tests 重跑(状态变了≠thrash);对验证型探测提高阈值,使"确认修好"不被罚。

### P13 — 联网任务离线不可评分（非缺陷,triage）　🔴
- **实例**：requests-2148（F2P 实际修复测试 1/1 过;2 个联网 PASS_TO_PASS 失败）
- **症状**：requests-2148:workflow 产出了**规范的正确补丁**、真修复测试通过,但 2 个 PASS_TO_PASS 集成测试打 httpbin.org → ConnectionResetError(104)/ProtocolError → 离线永远评不绿。gold 补丁同样挂。属任务不可评分,非能力/workflow 缺陷。
- **根因**：评测沙箱无网;部分 SWE-bench Lite 测试是实网集成测试(Py3.9 还撞 'getresponse() buffering')。
- **修复**：非代码——**triage**:把 env-blocked 实例移出能力指标(workflow 已正确报 BLOCKED + findings);A/B 选纯逻辑任务;给已知联网不可解实例打标,不计入。20-unresolved 中部分很可能是 env-blocked 而非能力。

### P14 — 轨迹 JSONL 跨 run 累加　🟡 部分
- **实例**：所有被多次重跑的实例（flask/pylint/sympy/django/requests）
- **症状**：每实例轨迹文件(如 pallets__flask-4045.jsonl)**追加写**、step 每 run 归 1,朴素分析会混多 run 得错结论;须隔离最近一次 run。
- **根因**：tracer 以 instance_id 为键 append,无 run_id 文件分隔。
- **修复**：已由阅读器 `logs/eval_workflow/view_run.py <traj> -1` 取最近 run 缓解。彻底:按 run 写文件(run_id 后缀)或写 run 边界标记记录,使下游自动归到最近 run。

---

## 行动计划（按杠杆排序）

> 提交顺序的关键依赖：**P6 必须在 P4 之前**（P4 的强制提交一旦被 P6 的标记泄漏误触发，会经 P8 把 coder 工具砍掉造成回归）。

1. **P6 kimi 标记解析器**（provider，影响大/成本低）—— 上游元凶,救 P4/P5/P8。
2. **P7 墙钟感知 forced-write + per-call 超时 + schema agent 限 thinking**（harness/workflow,critical/中）—— heavy 任务头号阻塞,救 django。
3. **P4 提交并加固 recon 强制提交**（已在工作树,补强制指令 + docstring 改口 + 探 named-dict + 提交）。
4. **P9 项目原生 run_tests + 清爽 GREEN/RED + 缺失子串 hint**（救 sympy/pylint 信号）。
5. **P5**（tester verdict 强制 + 无证据硬 FAIL）、**P8**（不裁工具/仅末轮触发）—— 多在 P6+P4 后顺带消解。
6. **P11**（str_replace 工具）、**P12**（loop 豁免改后重跑）。
7. **P10 真能力部分**（非 workflow 可修,靠 P9 hint + 换写法缓解）。
8. **P14**（按 run 写轨迹）、**P13**（env-blocked triage,非代码）。

## 已修问题对应 commit
- P1 → `4b7aaed`
- P2、P3 → `e349bde`
- P4、P6、P7、P9 → 未提交(工作树,见下"修复批次")

## 修复批次 2026-06-23(fix workflow P6749，全在工作树·未提交)

经 Spec→Implement→Verify fix workflow(13 agent)+ 1 轮 P7 残留收口落地;**suite 810 passed / 0 fail,ruff 干净,边界测试 5 passed**。对抗复审:P6/P4/P9 = solid,P7 初版 needs_work(残留时序缺口)已收口。

- **P6 ✅(经线上 forensics 二次修正后才真生效)** `adapters/llm/openai_provider.py` `_parse_response`:新增 `_extract_markup_tool_calls` 抽 `functions.NAME:ID`+JSON 合成 tool_calls。⚠️**初版是生产死代码**:只扫 `message.content`,但 thinking-ON 下 kimi 把 markup 放在 **`reasoning_content`**、content 为空 → 解析器没看到,reasoning 兜底又把 markup 拷进 content 伪装成"content 泄漏"(4/4 实例 100% 泄漏、靠 4b7aaed 空轮重试兜住,sympy 一处未兜住→空补丁)。**已修:同时扫 `reasoning_content`(elif 分支)并从 reasoning 清除 markup,suite 813、+3 reasoning 用例。** 上游元凶现在才真正根治、P8 才大体自消。
- **P7 ✅** 三部分 + 收口:① `WorkflowContext` 加 `seconds_left()/time_low()`(`time.monotonic()`),`evaluator` 把 `deadline=now+task.timeout` 接入,`_budget_ok` AND `not _time_low` → **墙钟感知 forced-write**;② `session_run._invoke_llm` 加 `per_call_timeout`(经 ports/container 注入,默认 600s 粗 backstop,不动全局);③ schema agent `thinking=False`(干掉 595s 那条)。**收口**:forced-write coder 改 `thinking=False` + 按 `seconds_left()` 夹紧(`asyncio.wait_for`,超时在 workflow 内取消、磁盘补丁已落),关掉"thinking-ON 强制写被外层墙截断"的残留缺口。
- **P4 ✅**(加固原工作树)`_STRUCTURED_RETRY` 起手 "You MUST call structured_output. Do NOT answer in prose";支持 named-function dict tool_choice(DashScope 可用则用,优雅回退);docstring 改"优雅非保证";纠正轮 key 在 `_schema_satisfied`(非 prose/stop)→ **P6 标记泄漏不会误触发它**(互放关系正确)。
- **P9 ✅** `adapters/tools/run_tests.py`:探测项目原生 runner(sympy `bin/test`/tox/`manage.py test`)+ 节点 id 翻译,见 'No module named pytest' 自动回退;**始终给可解析 `Verdict: GREEN|RED` + 缺失子串 hint + 连挂升级**(纯 tool 内解决,未碰 workflow.py)。

**提交顺序(待用户授权)**:P6 先于 P4(互放约束)。建议分提:① `openai_provider.py`+`test_llm_providers.py`(P6);② `run_tests.py`+`test_run_tests_tool.py`(P9);③ 其余 `workflow.py`/`session_run.py`/`ports.py`/`container.py`/`workflow_runtime.py`/`evaluator.py`/`workflows/analyst_solve.py`+相关测试(P7+P4)。

**已知延后(followup,非本批)**:P8 在纠正轮保留只读工具集(grep/file_read/run_tests)而非 capture-only;`anthropic_provider` 的 named-function dict tool_choice 会被丢(仅 native-Anthropic 路径,DashScope 无影响);P9 的 `Verdict:` 行未被 workflow.py 程序化消费(目前模型读,纯增量,留作未来硬 gate 钩子);per-call 600s 对 ~595s 用例本身不咬(靠 thinking=False 解决,刻意不全局降 `llm_timeout` 以免影响 team/CLI)。**未做线上 A/B 验证**(下一步:对 django/sympy/pylint smoke 复跑)。
