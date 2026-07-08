# OpenCollab vs Claude Code:多智能体构建的优势分析

> 2026-06-12。回答的问题:**OpenCollab 相较于 Claude Code 的 workflow 构建多智能体的优势是什么?**
> 结论经过多轮"有证据吗"式的自我拷问,所有被推翻的主张都保留在文中,防止以后再犯。

## TL;DR

1. **不要在"谁有 workflow"这个轴上比**——OC 的 workflow 引擎本来就是 CC Workflow 的同构移植(`agent/parallel/pipeline/phase/budget` 几乎一一对应),单拼引擎赢不了。
2. OC 真正可防守的优势是两条:**可消融性**(port 边界把换实现做成低成本操作——注意这是架构属性,port 级 A/B 实验尚未发生过,见九)和**工程化的调度层**(策略即代码、拓扑可声明、可 headless)。
3. **性能维度不竞争**:88.6%(Opus 4.8)vs 76.8%(Kimi K2.5)主要是模型差距;OC 的卖点是能把"模型贡献"和"harness 贡献"**分解开**的仪器,不是排行榜分数。
4. CC 的实验性 **Agent Teams 已支持 agent 间直接通信**——"调度层独有"的说法已失效,必须改为形态差异(见下)。引用它反而是好事:Anthropic 也走到了这个方向,等于给论文方向背书。

## 一、被证据修正过的主张(答辩前必读)

这些是讨论过程中先后被推翻的说法,**不要再对外使用**:

| 曾经的主张 | 为什么不成立 | 修正后的说法 |
|---|---|---|
| "CC 是黑盒、不可观测" | 全量 JSONL transcript 落盘(`~/.claude/projects/`)、官方 OTEL 导出、hooks 可拦截每次工具调用 | CC 数据层很透明,封闭的是**机制层**(源码、prompt 组装、压缩策略、调度逻辑) |
| "CC 不可复现、无法批量评测" | `claude -p --output-format json` headless 模式 + Agent SDK,可进 CI;版本可钉死 | 两边都只能做到**统计可复现**(LLM 采样无 seed);真正差异是可消融性 |
| "CC 永远接不了 kimi" | Moonshot 官方提供 Anthropic 兼容端点,`ANTHROPIC_BASE_URL` 即可接入,官方有教程 | CC 能接但"穿别人的西装"——harness 为 Claude 调优且不可改;OC 换模型时每个接缝可重调 |
| "CC 没有真正的多智能体,agent 不能互相通信" | 实验性 Agent Teams:lead+teammates、共享任务列表(文件锁)、mailbox、**teammate 间直接互发消息** | 只对 subagent 成立。对 Teams 的正确批评是**形态**:实验性、终端绑定、策略不可编程、状态临时 |
| "16 个 port,每个都是独立实验变量" | 九.审计:真接缝仅 4-5 个,其余为单实现 + 测试 fake;全仓**零次** port 级换实现 A/B | "port 边界把消融做成低成本可达的状态"(架构属性,已具备);"已消融"是实验事实,尚未发生——承重墙已浇筑,还没挂过重物 |

## 二、CC 侧事实清单(带证据)

### 三种编排形态,互为孤岛

1. **Subagent(Agent tool)**:一次性函数调用,spawn → 返回最终文本 → 销毁。不能互相通信,不能嵌套 spawn,每个约 20k token 开销。
2. **Workflow tool**:模型现写 JS 脚本编排 subagent。引擎成熟(journal 级 resume、worktree 隔离、结构化输出重试),但脚本是**会话内临时产物**,跑在受限沙箱里(禁 `Date.now`、嵌套一层、并发封顶)。
3. **Agent Teams(实验性)**:lead + teammates,共享任务列表,mailbox 点对点通信,hooks 质量门。限制:默认关闭;无 session 恢复;一次一个团队;不能嵌套;lead 固定;**深度绑定交互式终端**(in-process / tmux / iTerm2 分屏),文档中没有任何 headless/SDK 入口;调度策略完全由 lead 的 LLM 即兴决定,只能用自然语言"劝";团队状态是 `~/.claude/teams/` 临时文件,session 结束即删。

### 开放的部分

- Transcript:每 session 全量 JSONL(消息、工具调用、参数、结果),subagent 独立成文件。
- 遥测:官方 OpenTelemetry 导出(token、成本、工具调用、延迟)。
- Headless:`claude -p` + `--output-format json`,Agent SDK(Python/TS);2026-06-15 起 headless 用量走独立配额池。
- 版本钉死:`npm install @anthropic-ai/claude-code@<版本>` + 关自动更新 ≈ 钉住 harness。

### 封闭的部分(真正的批评点)

- 源码不开放(发行打包混淆 JS):可观测其输出,**无法阅读其机制,更无法替换任何组件**。
- 版本间无机制级 changelog:只知道行为变了,不知道哪个部件变了。
- 没有接缝(seam):不能只换 compaction 策略 / 调度逻辑而其余不变;暴露的是配置旋钮,不是组件接口。

## 三、OC 侧设计速览

- **严格 clean architecture**:`adapters → application → domain` 单向依赖,边界由测试强制;`application/ports.py` 定义 16 个 port,bootstrap 是唯一知道具体类型的组合根。
- **调度层**(`application/scheduler.py` + lifecycle/messaging/dedup 三个 mixin):SessionTable/SCB 进程表抽象、非阻塞 spawn、结果经消息注入投递给父 agent、Topology 拓扑校验、single-flight spawn 去重、worktree 池端口、autosave/manifest 持久化。**团队结构在 YAML 声明,调度行为是可读可改可测的代码。**
- **Workflow 层**(`application/workflow.py`,约 3.3k 行 application 层的一部分):确定性 mini 引擎,`agent/parallel/pipeline/phase/log/budget` 原语,纯 application 层(ports 注入 session factory / tools / tracing)。工作流是**仓库里受版本控制、有单测的 Python 制品**(`workflows/split_solve.py` + `tests/test_split_solve_workflow.py`)。已知短板:session `isolation` 尚为 no-op,子任务只能串行。
- **模型层**:原生多 provider adapter(`anthropic_provider.py` / `openai_provider.py`),实际生产配置跑 kimi-k2.6(DashScope)。
- **评测层**(`harness/`):headless 评测器是一等公民;workflow 函数可被 `run_eval_task` 原样调用;team 模式与 workflow 模式共享同一基座,支持受控 A/B(SWE-bench 子集 baseline:team+kimi-k2.6 = 61.7% resolved;workflow 模式 A/B 驱动已验证,全量待跑)。

## 四、维度对比

| 维度 | Claude Code | OpenCollab |
|---|---|---|
| 定位 | 商业编程产品,优化单用户体验 | 研究/工程平台,优化可控实验 |
| 多智能体 | subagent 一次性、不互通;Agent Teams 可互发消息但实验性、终端绑定、策略不可编程、状态临时 | 工程化调度器:声明式拓扑、消息注入、spawn 去重、持久 session FSM,全部可 headless |
| Workflow | 引擎更成熟(resume、worktree 隔离),但脚本是会话临时物 | 版本控制 + 单测的 Python 制品,与 team 模式同基座;isolation 尚为 no-op |
| 编排谱系 | 三个孤立形态(subagent / Workflow / Teams),互不互通,底座封闭 | 确定性 workflow ↔ 自治 team 是同一基座上的连续谱,可受控 A/B |
| 模型 | Anthropic 协议;第三方靠兼容端点,harness 为 Claude 调优且不可改 | 原生多 provider,换模型时每个接缝可重调 |
| 可观测 | JSONL transcript + 官方 OTEL,很好 | TracePort + harness;水平相当,**不构成差异点** |
| 可消融 | 不可:闭源整体,只有配置旋钮 | port 边界使换实现低成本(真接缝 4-5 个,余为测试接缝);port 级 A/B 尚未发生(见九) |
| 评测 | headless / SDK 可批量跑(但 Agent Teams 不在其中);harness 本身不可作为实验对象 | harness 一等公民,两种模式同基座可对比 |
| 性能 | Opus 4.8 SWE-bench Verified 88.6%;模型占大头 | OC+kimi-k2.6 自有子集 61.7%;Kimi K2.5 官方 76.8%。不主张性能,主张**可分解**性能 |

## 五、可防守的最终主张

> CC 最近的 Agent Teams 恰好验证了 peer 通信多智能体这个方向——连 Anthropic 都在做。但它做成了一个绑定终端、调度策略锁在 LLM 和闭源 harness 里的实验性产品功能;我们做成了一个调度策略即代码、拓扑可声明、可 headless 评测的开放平台。CC 的三种编排形态是三个互不互通的孤岛;OC 的确定性 workflow 和自治 team 跑在同一套可消融的基座上,这让"控制流放代码里还是放 LLM 里"第一次成为可被 SWE-bench 度量的实验变量。性能维度我们不竞争——那主要是模型差距,而我们的基座恰好是能把"模型贡献"和"harness 贡献"分解开的那个仪器。

关键措辞纪律:

- 用"**可消融性/可干预性**",不用"黑盒/不可复现"(守不住)。
- 调度层说"**先发的工程化实现**",不说"独有"(Agent Teams 已存在)。
- "调度层有潜力"≠"调度层已验证"——A/B 跑完前,能拍桌子的证据只有 harness 本身和 61.7% baseline。

## 六、待跑的实验

1. **workflow vs team 同基座 A/B**(进行中):split-solve workflow 模式 vs 自治 team 模式,SWE-bench 同子集。回答"确定性控制流值多少分"。
2. **CC+kimi vs OC+kimi**(新提出):CC 钉死版本、headless `-p` 单 agent 模式,经 `ANTHROPIC_BASE_URL` 接 kimi-k2.6,与 OC 同模型同子集对比。模型恒定,纯 harness 内循环质量对比。注意 Agent Teams 无 headless 入口,所以只能对比 CC 单 agent 形态——这反而干净。
3. **harness-模型适配消融**(远期):OC 内对同一模型开/关各 shaping 组件,量化"为模型调 harness"的价值,反衬 CC 固定 harness 接第三方模型的损耗。

## 七、显式会话状态机(Session FSM)的价值评估

> 独立研究 agent(Opus 4.8)读码 + 调研 CC 公开资料后的结论。回答师兄的问题:"你设计的状态机有价值吗?(CC 的状态是藏在循环里的)"

**裁决:有真实价值,但价值几乎全部集中在多 agent 调度;崩溃恢复上未兑现;单 agent 场景接近冗余。**

### OC 侧事实

- 13 个 phase(`domain/session.py:15`),`PHASE_TRANSITIONS` 是唯一迁移真相源;`transition_to` 运行期拒非法边且**拒绝时 phase 不变**;`fail()`/`cancel()` 是仅有的两个具名旁路。
- 全仓生产代码无绕过 FSM 直接改 phase 的地方——迁移校验是真闸门,不是摆设。
- **调度器核心决策直接读 peer 的 phase**:`_wake` 唤醒条件(`scheduler_lifecycle.py:245`)、`_quiescent` 全队静止判定(`scheduler.py:283`)、teammate 消息投递闸门(对方 `AWAITING_EVENTS` 时压在 inbox,`scheduler_messaging.py:113`)、finalize 判定。没有被具体化的 phase,这些 join/去重/唤醒逻辑写不出来。
- `test_session_phase_fsm.py` 用迁移表参数化**穷举**全部合法边 + 非法边 + 终态性质——隐式循环写不出这种测试。
- **反面硬证据:持久化不含 phase**。`adapters/storage.py` / `application/session.py` 的 `save()` 只写消息 + meta;恢复 = 消息重放,phase 重置 `IDLE`。`set_phase` docstring 声称的 snapshot/restore 用途全仓无实现。**"FSM 支撑崩溃恢复"这个主张被自己的代码证伪,不要对外说。**

### CC 侧事实

- 单 agent 循环确实无显式状态机:状态 = 消息历史 + while 循环 + `stop_reason`;唯一 reified 的是终止时的 `ResultMessage.subtype`(逆向分析原话:"no explicit state machine... full state is reconstructible from history")。
- **但 CC 在产品演进中不断把隐式状态显式化(最强旁证)**:Agent Teams 被迫外置一套带 status/ownership/dependency 的共享 task list + mailbox;Agent SDK hooks 把 SubagentStart/Stop、TaskCreated/Completed、TeammateIdle、PreCompact/PostCompact 等生命周期点 reify 成具名事件。"协作复杂度上来,状态就得显式化"是被两边独立验证的规律。

### 维度速览

| 维度 | 强度 | 一句话 |
|---|---|---|
| 多 agent 调度 | **强** | 唤醒/join/静止判定/消息闸门直接读 phase,刚需 |
| 可测试性 | **强** | 迁移表可穷举单测 |
| 可观测 | 中→强 | phase 是 trace/roster 的天然骨架,零额外成本;CC 要靠加 hook 事件逼近 |
| 正确性不变量 | 中 | 运行期强制,真能挡 handler 写错 phase,但线性循环里此类 bug 本就少 |
| 崩溃恢复 | **弱(未兑现)** | 持久化不存 phase,恢复靠消息重放——与 CC 无差异 |
| 单 agent 增值 | ≈0 | 消息历史已是完备状态,FSM 是冗余投影 |

### 对师兄可说的话

> 状态机在单 agent 场景基本不增值——消息历史本身就是完备状态,CC 不建模也跑得很好,我自己的持久化层都没存 phase。但它在多 agent 被动调度里是刚需:调度器要靠 peer 的生命周期状态做唤醒、join、静止判定,这些决策直接读 phase。最有力的旁证是 CC 自己:单 agent 循环坚持隐式,一做 Agent Teams 就被迫把同类状态显式化成共享 task list。所以它的回报来自调度而不是恢复,和 OC 作为多 agent 研究平台的定位对齐——这是我设计在前、被 Anthropic 的演进路线印证的部分。

### 弱点 / 改进点

1. **phase + pending_events 不持久化**:agent 在 `AWAITING_EVENTS` 时崩溃,挂起语义丢失。补上可把"可恢复性"从弱变强,反而成为对 CC 的差异化点;否则应删掉 docstring 的虚假声明。
2. `SCHEDULED`/`IDLE`/`AUTOSAVING` 偏薄(近乎单出边 pass-through),只服务可观测性。
3. **数字预警**:本文引用的 "Opus 4.8 SWE-bench Verified 88.6%" 在本轮检索中未复现(查到 Opus 4.5 = 80.9%);两者未必矛盾(不同代模型),答辩前核对出处。

## 八、OS 多进程模型类比的价值评估

> 七的姊妹篇,同样由独立研究 agent(Opus 4.8)读码 + 外部调研。回答:"模仿 OS 的多进程机制(SessionTable/SCB/被动调度器/消息注入/worktree 池)是真价值还是好看的类比?"

**裁决:OS 进程模型主要是命名词汇表,不是技术内核。真正干活的是任何并发 agent 系统都需要的两件事——统一生命周期视图 + 资源记账硬限额。词汇表的真实贡献是一份经过五十年验证的问题清单(僵尸/孤儿/wait/去重/限额/隔离),OC 答出了其中一半。**

### 关键事实

- **与学界主流是两个不同的类比**:AIOS/MemGPT/Karpathy 把 OS 映到**单 agent 的内存层**(虚拟内存/上下文分页);OC 把 OS 映到**多 agent 的进程层**(进程表/PCB)。学界主流不直接背书 OC 的选择;AIOS 的真抢占调度针对的是"多 agent 抢一个本地 LLM 后端",与 OC 的瓶颈(API token 成本)不同。
- **没有任何主流框架用 OS 进程模型**:LangGraph = 有状态图 + checkpoint;AutoGen = 对话/事件循环;CrewAI = 角色编排;CC = 三套分立机制(subagent / background / Teams task list),无统一进程表。这是 OC 独特的选择,不是行业常态——既是差异点也是需要自证的负担。
- **问题清单是真问题**:生产事故(Kilocode #8637 无界递归 spawn)和近期论文(ROMA / AgentSpawn)仍在被"重复 spawn、孤儿 agent、无界递归"反复咬;OC 的 single-flight 去重 + per-parent 锁 + step/budget cap 提前把这一类问题解掉了。

### 类比成立/破裂速览

| OS 概念 | OC 兑现 | 强度 |
|---|---|---|
| 进程表 / PCB(aid/ppid/状态/资源计数) | 完全(`domain/scheduler.py:63-104`) | **强**——单一真相源 |
| 非阻塞 fork、互斥原语、静止判定 | 完全(single-flight 去重、per-parent 锁、`_quiescent`) | **强**——成熟问题的成熟解 |
| rlimit(step/budget 会话生命期硬墙) | 完全(`session_run.py:151-181`) | **强**——headless 防烧钱刚需 |
| IPC(out-of-history inbox + 投递纪律) | 完全 | **强** |
| wait()/返回值通道 | 部分(仅 deferred spawn;fire-and-forget 无回收) | 中 |
| 信号(cancel_event ≈ SIGTERM) | 部分(仅 step 边界,协作式;LLM 调用不可中断) | 中 |
| cgroup 层级配额 | 部分(`split_budget` 是 spawn 时静态切块,非层级账户;且与 workflow 侧 `WorkflowBudget` 共享池是两套并行模型) | 中 |
| 地址空间隔离(worktree) | 分裂:团队侧真隔离 / **workflow 侧 no-op** | 中 |
| 快照恢复(PCB 持久化) | **不兑现**(save() 不存 phase/预算/pending) | 弱 |
| 抢占 / 时间片 / 优先级 | **不兑现**("被动 tracker"严格说不是 OS 意义的 scheduler,名字略名不副实) | — |
| **孤儿回收 / 僵尸收割 / 父死级联** | **不兑现**:`children_of` 建了但 application 层从未调用;父失败时在飞子继续跑到全局 cleanup;终态 SCB 永不收割 | — |
| Topology 声明式拓扑 | 完全,但这是**对 OS 模型的偏离**(更像 SELinux 式 MAC 策略,不是动态进程树)——OS 类比之外的自有设计 | 强 |

### 对师兄可说的话

> OS 进程模型在 OC 里主要是命名词汇表,不是技术内核——真正干活的是统一生命周期视图和资源记账硬限额,这些换成 LangGraph 的 checkpoint 图也能拿到。但词汇表本身不是零价值:它给了我一份五十年验证过的问题清单——僵尸、孤儿、wait、去重、限额、隔离。我答出了一半,而且答得比隐式框架稳:single-flight 去重和 per-parent 锁是同步领域的成熟解,而生产里和近期论文都还在被"无界递归 spawn、孤儿 agent"反复咬。破裂点我也认:没有抢占(我的调度器是被动 tracker)、没有孤儿/僵尸回收、崩溃恢复不存 phase、workflow 侧隔离是 no-op。一句话:价值是真的,但它属于"并发系统记账 + 生命周期管理"这个通用类别,OS 只是借来的命名和问题清单——而"知道该问哪些问题"恰好是设计早期最值钱的东西。

### 弱点 / 改进点

**OS 清单提示但未实现(类比已经告诉你该补的):**
1. **父死级联缺失**:`children_of`(`domain/scheduler.py:96`)从未被调用;应在 `_drive_agent` 失败分支级联取消/标记子 agent。
2. **僵尸收割缺失**:终态 SCB 永久留在 `entries`,`team_snapshot` 一直列出已死 agent;需区分"终态未收割"与"已收割"。
3. **wait 语义不完整**:fire-and-forget spawn 无任何回收通道。
4. **运行态不持久化**(与七.弱点 1 同根):补 phase/used_tokens/pending_events 才能兑现"PCB 可恢复"。

**类比诱导的设计债(该剪不该补):**
5. 偏薄 phase(SCHEDULED/IDLE/AUTOSAVING)——为概念对称留状态是负债。
6. 两套并行预算模型(团队侧 `split_budget` vs workflow 侧 `WorkflowBudget`)应统一。
7. workflow 侧 `isolation` 参数名实不符:要么落地 worktree 化,要么从签名删掉。

## 九、严格 Clean Architecture 的价值评估

> 回答的问题:**adapters→application→domain 单向依赖、16 个 port、bootstrap 组合根、边界测试强制——是真价值,还是研究原型上的过度工程?** 同前两轮口径:"有潜力"≠"已验证"。本节直接检验三.速览和四.对比里的核心主张"16 个 port 每个都是独立实验变量(可消融)",并据证据降级。

### Port 审计表(全报告最重要的交付物)

把 `application/ports.py` 的 16 个 port 按"接缝成色"分三类。**真接缝** = ≥2 个生产实现已在跑;**测试接缝** = 1 个生产实现 + 测试用 fake 行使过;**仪式抽象** = 1 个生产实现且测试连 fake 都不替。

| Port (file:line) | 生产实现数 | 测试 fake | 分类 |
|---|---|---|---|
| `EnvironmentPort` (`ports.py:13`) | **3**:`LocalEnvironment`/`WorktreeEnvironment`/`DockerEnvironment`(`adapters/env.py:55/93/201`),Docker 在 harness 真跑、Worktree 在 team pool 真跑 | `FakeEnv`/`FakeRemoteEnv`/`FakeDocker`(6 文件) | **真接缝** |
| `EventPublisherPort` (`ports.py:76`) | **4+**:`TuiEventSink`(`tui/session_adapter.py:16`)、`AutoSaveSubscriber`(`application/autosave.py:26`)、`HookEventSubscriber`(`application/hooks.py:29`)、CLI `_ConsoleEventSink`(`cli/workflow.py:48`),经 `EventBus` 同时扇出 | `FakeEventPublisher`/`RecordingSink`(3 文件) | **真接缝**(多订阅者并存,非互换) |
| `ShaperPort` (`ports.py:81`) | **6**:`PerToolResultBudgetShaper`/`ToolOutputClearShaper`/`OldHistorySnipShaper`/`AutoCompactShaper`/`ContextCollapseShaper`/`ShaperPipeline`(`application/shaping/`),组成默认 pipeline | `test_shaping.py`(19 测试)直接行使 | **真接缝**(组合式,非互换式;接缝形态偏"策略管线") |
| `LLMPort` (`ports.py:274`) | **1 类 / 2 代码路径**:单一 `LLMClient`(`adapters/llm/client.py:16`)内部按 provider 派发到 `complete_anthropic` / `complete_openai`(`anthropic_provider.py:47` / `openai_provider.py:59`) | `FakeLLM`/`FakeLLMClient`(8 文件,最多) | **真接缝(半行使)**:见下"多 provider 兑现" |
| `SafetyPolicyPort` (`ports.py:36`) | 1:`SandboxInterceptor`(`adapters/safety.py:39`) | `FakeSafetyPolicy`/`SpySafetyPolicy`(4 文件) | **测试接缝** |
| `PermissionPort` (`ports.py:57`) | 1:`TuiPermissionPolicy`(`tui/session_adapter.py:33`) | `FakePermissionPolicy`(4 文件) | **测试接缝** |
| `AskUserPort` (`ports.py:62`) | 1:`TuiAskUserPolicy`(`tui/session_adapter.py:55`) | `FakeAskPolicy`(1 文件) | **测试接缝** |
| `SessionStorePort` (`ports.py:287`) | 1:`SessionStore`(`adapters/storage.py:8`) | `FakeStore`(1 文件) | **测试接缝** |
| `TracePort` (`ports.py:310`) | 1:`Tracer`(`adapters/trace.py:19`) | `FakeTracer`(3 文件) | **测试接缝** |
| `ToolPort` (`ports.py:109`) | 多(每个工具一个,但都继承同一 `Tool` 基类) | `FakeTool`(2 文件) | **测试接缝**(同族多态,非异质实现) |
| `SchedulerPort` (`ports.py:194`) | 1:`Scheduler`(`application/scheduler.py:54`) | `FakeScheduler`(2 文件) | **测试接缝** |
| `SessionFactoryPort` (`ports.py:130`) | 1:`DefaultSessionFactory`(`bootstrap/session_factory.py:193`) | `FakeFactory`(3 文件) | **测试接缝** |
| `WorkflowSessionFactoryPort` (`ports.py:173`) | **2**:`WorkflowSessionFactory`(`bootstrap/workflow_runtime.py:45`,TUI/CLI 路径)+ `_EvalSessionFactory`(`harness/evaluator.py:90`,headless 路径) | `FakeFactory`(workflow 测试) | **真接缝(小)**:正是"双 delivery 共享 workflow 引擎"的接合点 |
| `HookPort` (`ports.py:95`) | 1:`ShellHookRunner`(`adapters/hooks.py:31`) | `test_hooks.py`(20 测试) | **测试接缝** |
| `WorktreePoolPort` (`ports.py:331`) | 1:`WorktreePool`(`adapters/worktree_pool.py:18`) | 真实 pool + `tmp_path`(`test_worktree_pool.py`) | **测试接缝** |
| `DiffCapablePort` (`ports.py:24`) | 1:`WorktreeEnvironment.get_diff`(`adapters/env.py:139`) | `test_worktree_diff_delivery.py` | **测试接缝**(`@runtime_checkable` 能力探测,非可换实现) |
| `CompletionResponse`/`CompletionUsage`/`TokenEstimatorPort` | 结构契约 / 单 callable(`llm/types.py:79`) | 经 `FakeLLM` 间接 | **结构契约**(让 application 不 import adapter 响应类型) |

**统计**:**真接缝 4-5 个**(Environment、EventPublisher、Shaper、WorkflowSessionFactory,LLM 算半个)。**测试接缝 ~10 个**(都有 fake 行使,支撑纯净快测,但生产侧只有一个实现)。**纯仪式抽象 0 个**——每个 port 至少被一个测试 fake 行使过,没有"测试都不替"的死接口。git log 全仓**没有任何一次"换两个生产实现跑对比"的痕迹**(`grep -i ablat|swap|compar` 只命中 `--no-hints` 这一个 prompt 级消融和 provider 重构提交,都不是 port 级实现互换)。

### 兑现清单(带证据)

1. **双 delivery 共享同一核心(最强兑现,强)**:TUI/CLI 和 headless harness 都经 `bootstrap.build_session` 构造会话(`harness/evaluator.py:139`、`bootstrap/workflow_runtime.py:91`),都用同一个 `WorkflowContext` 引擎(`application/workflow.py`)跑同一批 workflow(`workflows/split_solve.py` 等)。这是教科书 clean architecture 头号承诺"一个 use case、两个 delivery 机制"的**真实兑现**,也是"评测一等公民"主张的**结构基础**——能 headless 批量评测不是补丁,是同一核心换个适配器。

2. **可测试性(强)**:568 测试(已超 CLAUDE.md 记的 490 基线)**1.69s 全绿**;**0 个测试 import 真实 `openai`/`anthropic` SDK 或打网络**,LLM 全走 fake;无 `@pytest.mark.slow/integration/network`。这个速度与纯净度直接归功于 domain/application 只依赖 stdlib + port —— 任何外部副作用都能在边界处用 fake 截断。

3. **多 provider(真接缝,但只半行使,中)**:`LLMClient` 双路径已实现,**但生产配置 `provider=openai` + DashScope base_url 跑 kimi-k2.6**(`configs/.env`),即**走 OpenAI 兼容路径指向异厂**。这条路径的可移植性是真兑现的(OpenAI/DashScope/kimi 同一适配器只换 base_url)。但**native Anthropic 分支 `complete_anthropic` 在任何 committed 配置里都没被选中**(`grep provider.*anthropic configs/ = 空`)——所以"两个 provider 实现"严格说是"一条路径在产、一条路径写好待命"。

4. **边界强制(有,但软,中)**:`tests/test_application_boundaries.py` 与 `test_domain_boundaries.py` 用**正则扫 `from/import opencollab.{core,tools,bootstrap,cli,adapters,team}`**,不是 AST。够抓"误手 import 了外层"这类回归,但绕得过(字符串拼接、`importlib`、注释提到不算)。git log 多次出现 `refactor: ...boundaries` / `pin ToolSpec boundary`(`07a4476`、`3753f2f` 等)说明**边界是被持续维护的活线**,不是一次性摆设。

### 成本清单(反面证据,带证据)

1. **跨层能力的穿透成本(实打实)**:加一个跨层能力要改一串文件。证据——分层上下文/shaper 那次(`02c48b5`)**碰了 22 个文件**:`ports.py` 新增 16 行(加 `ShaperPort`)、`session_run`/`tool_execution`/`scheduler` 各改、`bootstrap/container.py` +179 行、`domain/context.py` 新建,外加一圈测试。对比:加一个 **workflow** 只要一个文件(`workflows/self_collab.py` 304 行,importlib 发现,`cf2f483`),加一个 **adapter 内能力**(repo map,`e363ccc`)只碰 5 个文件。**结论:沿 port 注入轴扩展便宜,横切 application 数据流的能力贵。**

2. **占位接口反模式(确诊一例)**:`WorkflowSessionFactoryPort.build_workflow_session` 的 `isolation: bool` 参数被一路接受、透传(`ports.py:189`→`workflow.py:144/164`→两个工厂),但**两个生产工厂都忽略它、都建 `LocalEnvironment`**(`workflow_runtime.py:93` 注释直言"accepted for forward-compatibility... currently runs in a local environment")。这是"接口先行、实现缺位"的占位接口——clean architecture 习惯确实诱导这种"先把缝留好"的写法。(七.弱点 7 已记同一处。)

3. **Shallow / pass-through 风险(轻微,2 处)**:多数 port 是深模块(safety、shaping、scheduler 都封了真复杂度)。但 `_EvalSessionFactory` 与 `WorkflowSessionFactory` 是两个几乎同构的薄工厂,各自只做"组 Agent → 调 `build_session`"(`evaluator.py:122` vs `workflow_runtime.py:78`),body 高度重复——Ousterhout 会判为偏薄。`LLMPort.complete` 之上的 `LLMClient` 也接近 pass-through(真逻辑在 provider 模块),但它承担了 provider 派发,不是纯转发。

4. **re-export shim(可控,2 处)**:`bootstrap/container.py` 用 PEP 562 `__getattr__` 懒重导 `session_factory`/`scheduler_factory` 的名字(`container.py:278`),为打破导入环、保持 `from container import X` 不变。这是 CLAUDE.md "拆模块保留公共名"纪律的直接产物——目前只 2 处、有明确理由,不算债务堆积,但属于纯为兼容旧导入路径而存在的胶水。

### 维度速览(成色 / 强度)

| 维度 | 兑现成色 | 强度 |
|---|---|---|
| 双 delivery 共享核心 | 已兑现:TUI/CLI 与 harness 同一 `build_session` + 同一 workflow 引擎 | **强** |
| 可测试性 | 已兑现:568 测试 1.69s,0 网络,纯净核心是直接成因 | **强** |
| 多 provider / 模型可换 | 半兑现:OpenAI 兼容路径在产(kimi),Anthropic 原生分支待命未选中 | **中** |
| 可消融性("每个 port 都是实验变量") | **大幅高估**:真接缝 4-5,测试接缝 ~10,**port 级实现互换实验=0 次** | **弱(潜力,非已验证)** |
| 边界保护 LLM 辅助开发 | 部分:正则边界测试 + 持续 refactor 提交说明它是活线;但没抓到过"违规被挡下"的明确 commit | **弱-中** |
| 仪式成本 | 真实但有界:横切能力穿 ~20 文件,占位接口 1 处,薄工厂 2 处,shim 2 处 | — |

### 对师兄可说的话

> Clean architecture 在 OC 上**兑现了三件实打实的回报**:(1) TUI 和 headless 评测器跑同一个 `build_session` + 同一个 workflow 引擎,这让"评测是一等公民"不是口号而是结构;(2) 568 个测试 1.69 秒全绿、零网络,因为 domain/application 只认 port、不认 SDK;(3) LLM 走 OpenAI 兼容适配器,换 base_url 就从 OpenAI 跳到 DashScope 上的 kimi——LangChain 那种"换厂要重写大段代码"的耦合在这里不存在。**但"16 个 port 每个都是独立实验变量"这句话当前是高估,得降级。** 诚实账本:16 个里只有 4-5 个是真接缝(环境 Local/Worktree/Docker、事件多订阅者、shaper 管线、workflow 工厂双路径),其余 10 个是只有单一生产实现、靠测试 fake 行使的"测试接缝"。**全仓没有任何一次真把两个生产实现摆上去做 A/B 消融**——`--no-hints` 那个是 prompt 级开关,不是 port 级换实现。所以正确措辞是:**这些接缝把"可消融"做成了低成本可达的状态(换实现只动 adapter + bootstrap,不动核心),但"已消融"还没发生。** 这面墙现在的成色是"承重墙已浇筑、还没挂过重物",不是"已验证的承重墙"。它是好习惯,且对一个要拿 SWE-bench 度量"harness 贡献 vs 模型贡献"的研究平台是**对的**好习惯;但把它当成对 CC 的核心差异、并声称"已经在消融"——守不住。

### 弱点 / 改进点

**"可消融性"措辞必须降级(对外口径):**
1. 第三/四节的"每个 port 都是独立实验变量""16 个 port,每个都是独立实验变量"应改为:**"port 边界把消融变成低成本操作(换实现只触 adapter+bootstrap);其中 4-5 个已有多生产实现,其余为单实现+测试接缝;尚无 port 级 A/B 实验。"** ——"可消融"是**架构属性**(已具备),"已消融"是**实验事实**(未发生),两者别混。

**最该先行使哪个接缝、做哪个消融(把潜力变证据):**
2. **最值钱的第一个消融:`WorkflowSessionFactoryPort` 的 `isolation`**——把占位参数落地成 `WorktreeEnvironment`,然后在 SWE-bench 上跑"串行共享工作树 vs 并行隔离工作树"A/B。一举两得:消掉占位接口反模式(成本清单 2),又产出第一个真正的 port 级消融数据点,直接给"可消融"主张挂上重物。
3. **第二个消融:`ShaperPort` 管线**——逐个关掉 shaper(no-op 替换)跑 SWE-bench,量化每层压缩对分数/token 的贡献。这是现成真接缝(6 实现已在),消融几乎零额外代码,且正好回答"harness 的上下文管理贡献多少"——平台的招牌问题。
4. **把 LLM 真接缝补全到在产**:跑一次 `provider=anthropic` 原生路径的对照(同 prompt、同 workflow,kimi vs Claude),让"两个 provider 实现"从"一条待命"变成"两条都验证过"。

**架构债(该剪该补):**
5. 两个薄会话工厂(`_EvalSessionFactory` / `WorkflowSessionFactory`)body 重复,可抽一个共享 builder,消 shallow-module 气味。
6. 边界测试从正则升级到 AST(`ast.walk` 找 `Import`/`ImportFrom`),堵掉字符串拼接/`importlib` 绕过;并考虑加一条"port 不得 import 任何 adapter 具体类型"的反向断言,防 port 照抄某实现形状(目前 `CompletionResponse` 等结构契约已是好榜样)。

## 十、尚未打出的牌:有潜力但未被主张的卖点

> 七~九是对已有设计决策的防守性质询;本节反向盘点:哪些结构性能力还没被当成卖点。按**独特性 × 兑现成本**排序。措辞纪律同前:每条都标注"已具备"与"未发生"的边界。

### 1. Topology 作为实验变量(独特性最高)

八.已发现 Topology 是对 OS 模型的偏离(更像 SELinux 式 MAC 策略),但只当成防守点。反过来:**全行业没有第二个框架把"谁能和谁通信"做成声明式、运行前可校验的一等对象**——CC Teams 由 lead 即兴决定,LangGraph 的图是控制流不是通信权限。这意味着:

- **实验**:"同任务、同模型、同预算,只换拓扑(星型 vs 层级 vs 全连通)对分数/token 的影响"——**只有 OC 能跑**,且一组对照只是几个 YAML(`domain/team.py:16`,`_check_topology` 在 spawn/message 前强制)。可能是比 workflow-vs-team 更新颖的论文轴。
- **附赠安全叙事**:agent 治理时代,"coder 永远 message 不到 deployer"是可静态审计的性质,接上 blast-radius 收敛的行业话题。

已具备:Topology 强制 + YAML 声明。未发生:任何拓扑对照实验。

### 2. 录制-回放:把"统计可复现"升级为"确定性回放"(兑现成本最低)

一.承认两边都只能统计可复现——但这个弱点 OC 有独有解法:`LLMPort` 已支撑 FakeLLM(8 个测试文件在用),离"录制真实响应 → 确定性回放"只差一个 RecordingLLM/ReplayLLM 适配器。回放模式下,**harness 改动(换 shaper、改调度策略)可在冻结的模型行为上零 token 回归测试**。CC 没有注入响应的接缝,做不到。这一条把"可消融性"和"可复现性"两个主张焊在一起,且顺手产出第一批 port 级换实现实践(九.最缺的东西)。

已具备:LLMPort 接缝 + fake 先例。未发生:Recording/Replay 适配器本身。

### 3. 异构模型团队(CC 结构性做不到)

per-role `model:` override 已存在(`bootstrap/context_builder.py:169`、`team.example.yaml` 注释明示),domain `Agent` 自带 `base_url`(`domain/agent.py:25`)——"强模型 lead + 便宜模型 coder + 强模型 reviewer"的**按角色成本最优模型分配**在结构上已支持。CC Teams 全员 Claude,无混厂选项。成本敏感多智能体是热点方向且此实验 OC 独占。

已具备:per-role model/base_url 结构。未发生:committed 配置里没有任何异构组合跑过。

### 4. 预算分配策略 + 资源压力下的失败模式分类学

`split_budget`(`domain/scheduler.py:20`)是策略接缝(当前为静态切块),CC 只有顶层美元帽、无可编程层级预算。且已有**第一个标本在手**:lead 在 budget_exceeded 时会无限重生同类 agent(已在生产 trace 中观察到)——这就是"资源压力下 LLM 团队协调病理"的开篇案例。FSM 把 `BUDGET_EXCEEDED`/`ERROR` 做成一等状态,意味着 OC 天然是**故障注入测试台**:杀 mid-run agent、注入预算枯竭、观察团队恢复。没人能对 CC Teams 做故障注入。

已具备:预算接缝 + 失败一等状态 + 一个已观察病理。未发生:系统性故障注入实验。

### 5. A/B 的副产品是带标注的多智能体协调数据集

TracePort + harness 在受控拓扑/预算下产出完整交互 trace。workflow vs team 的 n=300 跑完,手里就是几百对"同任务、不同协调机制"的配对轨迹——稀缺数据(协调失败分析、过程监督、编排策略蒸馏都用得上)。budget-loop 病理正是从读 trace 得来,证明这条管线产得出 finding。

### 6. 成本归一化的评测方法论

baseline 档案已含 tokens/calls/wall/phases 全套计量,而行业排行榜只报 resolved%。"resolved per dollar / per token" 这个指标 OC 的仪器天然支持——methodology 短文材料,与"不竞争性能、竞争可分解性"(五.)完全一致。

### 7. 开放参考实现(社区牌,非论文牌)

learn-claude-code 逆向分析仓库的热度证明了需求:大家想读懂 agent harness 机制,但 CC 是混淆代码。OC 是**可运行、568 测试 1.69s、严格分层的 CC 级 harness 参考实现**——"agent harness 界的 minGPT"这个位置目前空着。

### 优先级建议

**1 和 2 最值得先做**:拓扑实验是别人结构上做不了的新轴;录制回放用已有接缝修补自己最大的弱点(统计可复现),还顺带补上九.缺的第一次 port 级换实现。3、4 紧随其后——都符合"接缝已浇筑、挂重物便宜"的现状。

## 来源

- [Claude Code Agent Teams 官方文档](https://code.claude.com/docs/en/agent-teams)
- [Claude Code Subagents 指南](https://aicrossroads.substack.com/p/claude-code-subagents)
- [Run Claude Code programmatically(headless)](https://code.claude.com/docs/en/headless)
- [Claude Code CI/CD 无人值守实践](https://hidekazu-konishi.com/entry/claude_code_cicd_and_headless_automation.html)
- [Kimi 官方:在第三方 Coding Agent 中使用 Kimi](https://platform.kimi.ai/docs/guide/agent-support)
- [Claude Code Monitoring(OTEL)官方文档](https://code.claude.com/docs/en/monitoring-usage)
- [Kimi K2.5 vs Claude Opus 4.6 基准对比](https://zoer.ai/posts/zoer/kimi-k2-5-vs-opus-4-6-benchmark-comparison)
- [Kimi K2.6 vs Claude Opus 4.6 vs GPT-5.4 对比](https://lushbinary.com/blog/kimi-k2-6-vs-claude-opus-gpt-5-4-gemini-comparison/)
- [Claude Agent SDK:agent loop 文档](https://code.claude.com/docs/en/agent-sdk/agent-loop)(七、FSM 评估)
- [learn-claude-code:CC 主循环逆向分析](https://github.com/shareAI-lab/learn-claude-code)(七、FSM 评估)
- [Claude Agent SDK:hooks 事件列表](https://code.claude.com/docs/en/agent-sdk/hooks)(七、FSM 评估)
- [AIOS: LLM Agent Operating System(arXiv)](https://arxiv.org/abs/2403.16971)(八、OS 模型评估)
- [MemGPT:虚拟上下文管理(arXiv)](https://arxiv.org/abs/2310.08560)(八)
- [Karpathy LLM OS 构想解读](https://www.mindstudio.ai/blog/software-3-0-explained-karpathy-context-window-ram-model-weights-cpu)(八)
- [Kilocode #8637:无界递归 spawn 生产事故](https://github.com/Kilo-Org/kilocode/issues/8637)(八)
- [LangGraph vs AutoGen vs CrewAI 框架对比](https://www.datacamp.com/tutorial/crewai-vs-langgraph-vs-autogen)(八、九)
- [Ousterhout vs Uncle Bob:APoSD 与 Clean Code 公开辩论(GitHub)](https://github.com/johnousterhout/aposd-vs-clean-code)(九、薄层/过度分层批评)
- [A Philosophy of Software Design 摘要:深模块 vs 浅模块、pass-through 红旗](https://www.mattduck.com/2021-04-a-philosophy-of-software-design.html)(九)
- [LangChain 批评:抽象泄漏、换 OpenAI→Anthropic 要重写大段代码](https://sider.ai/blog/ai-tools/is-langchain-still-worth-it-a-2025-review-of-features-limits-and-real-world-fit)(九、port 作为对冲)
- [六边形架构对冲 LLM provider SDK 变化:IntelligencePort 隔离 prompt 与具体厂商](https://medium.com/@martia_es/applying-hexagonal-architecture-in-ai-agent-development-44199f6136d3)(九)
- [LM Evaluation Harness:标准化 LM 后端 + 可插拔任务模块 = 可复现研究基座](https://github.com/EleutherAI/lm-evaluation-harness)(九、研究平台先例)
- [模块化 Agent Harness:逐个消融 perception/memory/reasoning 模块隔离贡献(arXiv)](https://arxiv.org/html/2507.11633v1)(九、"接缝即实验变量"先例)
- [AI agent 部署 blast-radius 收敛:进程边界/最小权限/执行隔离限制误动作半径(Sophos)](https://www.sophos.com/en-us/blog/inside-the-lethal-trifecta-blast-radius-reduction-in-ai-agent-deployments)(九、边界保护 LLM 辅助开发)
