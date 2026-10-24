---
{
  "id": "evaluation-boundary",
  "title": "用 OCE 评测 OC 的 Agent、Team 和 Workflow",
  "category": "observability",
  "summary": "OC 负责跑任务，OCE 负责出题和打分。想跑 benchmark 先弄清各自管什么。",
  "aliases": [
    "OpenCollab-Eval",
    "跑题",
    "Terminal-Bench",
    "DeepSWE",
    "SWE-bench",
    "官方评分",
    "跑分"
  ],
  "keywords": [
    "评测",
    "OCE",
    "Benchmark"
  ],
  "questions": [
    "理解 OC 与 OCE 的分工，找到正式运行和收集评分结果的入口？",
    "想用 SWE-bench 测我的 Team，从哪里开始？",
    "运行显示 completed，算不算题目通过？"
  ],
  "related": [
    "run-results",
    "native-test-evidence",
    "token-cache-cost"
  ],
  "verified_ref": "0c47f74a"
}
---

# 用 OCE 评测 OC 的 Agent、Team 和 Workflow

OC 只负责执行 Agent、Team、Workflow。benchmark 数据、批量运行、候选评分和实验分析都在独立仓库 OpenCollab-Eval（OCE）。OCE 通过 OC 的 SDK 发起运行，再用 benchmark 自己的验证器打分，所以 `RunResult.status == "completed"` 不等于题目通过。本文说明两者的分工、一次评测要保留的材料，以及从哪里进入 OCE。

## 怎么开始

1. 确定要比较的 OC 运行方式：单 Agent、团队文件，还是工作流。
2. 自定义 Workflow 先在普通项目上跑通，确认输入、输出和交付行为正常。
3. 到 OCE 配置数据集和验证器。
4. 评测器通过 SDK 调用 OC 时，传入模型、预算、时限、环境和产物位置。
5. 正式运行前，确认两个仓库的版本和要用的功能。

当前评测命令与数据集设置以 [OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme) 为入口。具体 benchmark 的安装步骤和评分参数，见对应的 OCE 指南。OC 这边的配置运行时文档说明 SDK 的预算、取消、清理与结果接口。项目里历史实验的命令，要对照当时的提交来读。

## 一次评测包含什么

通常有这些材料：

- 任务输入
- 初始仓库或镜像
- 模型与运行配置
- 生成的候选
- 原始验证结果
- 用量记录

## "跑完了"不等于"做对了"

评测里有几个不同阶段，结果里要分开看：

1. 候选生成完成
2. 验证器启动
3. 测试通过
4. 最终成绩被采用

OC 的 `RunResult` 只说明运行怎样结束。题目是否通过，由所选 benchmark 的验证规则决定。举两个例子：

- 运行正常结束，但留下了失败的测试。
- 基础设施出错，验证根本没能启动。

结果读取方法见 [读取运行结果与判断任务是否完成](run-results.md)。

## 让比较公平

这些设置都会影响成绩：模型提供方、角色提示词、工具集合、执行环境、验证器资源。比较不同协作方案时，把这些设置写清楚，并保留能对应到每道题的记录。

## 代码怎样交给验证器

不同评测适配器的交付方式可能不同：

- 补丁
- 最终工作树
- 完整容器状态

选哪种要遵守任务规则。如果任务涉及未提交文件、启动的服务或容器内产物，尤其要注意。候选具体走哪条路径送进验证器，要到 OCE 对应的适配器里核对。

## 常见问题

如果基础设施出故障导致没评上分，就在恢复后用相应的恢复流程，把已保存的候选重新评分。是否允许重新生成候选，由实验协议决定。建议保留原尝试、恢复过程和最终结果，方便解释前后差异。

## 源码与进一步阅读

[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md) · [docs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/README.md) · [scripts/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/scripts/README.md)

[读取运行结果与判断任务是否完成](run-results.md) · [用实际测试验证 Agent 的交付](native-test-evidence.md) · [统计 token、缓存命中和费用](token-cache-cost.md)
