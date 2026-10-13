# 研究窗口检索焦点预算保留

日期：2026-10-11；第二十五轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只修复研究窗口在有界检索变体预算下可能失去自身角色焦点的问题。`paper_review`、`experiment_compare`、`meeting_actions` 已经生成的 workflow focus variant 必须真正进入当前请求的检索计划；反馈和 episodic 活动可以参与检索，但不能在存在 workflow focus 时把全部预算占满。

本轮不修改冻结评测集、gold、模型、索引内容、项目/记忆权限、答案评分或外部后端。

验收条件：

1. 只有反馈时，最多保留两条反馈，为 workflow focus 保留至少一个槽位。
2. 反馈与 episodic 活动同时存在时，两者合计仍不能占用 workflow focus 的保留槽位。
3. 无 workflow focus 时保持原有最多三个反馈/活动提示的行为。
4. 审计事件继续报告 `workflow_retrieval_variant_count`，并通过完整工程检查。

## 外部机制与本地缺口

- [Haystack 固定 commit](https://github.com/deepset-ai/haystack/tree/6ca8349091c09af89ec0090aa13cb22de1d88bfb) 的 pipeline/router 设计强调组件路由和运行路径应显式、可观察；本轮只借鉴“路由结果必须可观测”的原则，不引入 Haystack。
- [Self-RAG 论文 v1](https://arxiv.org/abs/2310.11511v1) 将是否检索、何时检索作为受控决策，而不是无界循环；本轮把同一原则收敛为本地固定 `MAX_RETRIEVAL_VARIANTS=3` 预算，不复制训练方法或宣称 Self-RAG 质量。

现有 RAG 已有 workflow focus、用户反馈和 episodic 活动三类变体，但组装顺序先加入反馈和活动，再加入 workflow focus。预算为 3 时，三条反馈会让 focus 数为 0；第一次修复只限制反馈后，`2 条反馈 + 1 条 episodic 活动` 仍会复现同一问题。

## B0 与候选实现

### B0 反例一：反馈填满预算

- 新增回归：`tests/test_rag_prompting.py -k bound_workflow_reserves_a_focus_variant`。
- 旧行为结果：**1 failed, 112 deselected in 0.27s**；`paper_review` 的 `workflow_retrieval_variant_count` 为 0。

### Candidate 一：反馈保留槽位

- 将反馈预算从 3 条收紧为“存在 workflow focus 时最多 2 条”。
- 该候选定向通过，但随后审查组装顺序发现 episodic 活动仍可填满保留槽位。

### B0 反例二：反馈与 episodic 活动共同填满预算

- 新增回归：`tests/test_rag_prompting.py -k feedback_and_episodic_hints`。
- 候选一结果：**1 failed, 113 deselected in 0.28s**；真实失败证明窗口角色仍可能变成通用活动检索。

### Candidate 二：统一预留 workflow focus

- 新增 `retrieval_hint_budget_before_workflow`，在 workflow focus 存在时将反馈和 episodic 活动的合计预算限制为 `MAX_RETRIEVAL_VARIANTS - 1`。
- 没有 workflow focus 时预算仍为 3，不改变通用会话的已有行为。
- workflow focus 随后进入同一检索计划，并通过既有 `workflow_retrieval_variant_count` 审计字段暴露。

## 验证证据

- 最终窗口定向回归：**6 passed, 108 deselected in 0.11s**。
- RAG/索引/多模态扩大回归：**173 passed, 1 warning in 3.22s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1746 passed, 1 warning，421.57 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/rag_engine.py` = `A38FDF20C1FA0D83016433A786F19829BE6209D78738CA1192718D81FB7B6E84`
  - `tests/test_rag_prompting.py` = `42A0F495C2ACA0477D07D1C3DFCB89EFCC99ACBD3774EA4B2D2F335B986690E9`

## 边界与下一轮

本轮只证明在本地固定预算和测试 fixture 下，绑定 workflow 不会被反馈/episodic hints 挤掉；不证明召回率、证据覆盖率、开放式回答质量、跨会话答案质量、Self-RAG 分数、真实外部 Haystack/GraphRAG/Temporal 后端、断电/跨主机 exactly-once 或真实 UI/E2E。总预算仍为 3，代价是绑定 workflow 下反馈和活动提示最多共享 2 个槽位。

下一轮继续审查三类窗口的记忆 scope、episodic 选择、证据引用 namespace 与 skip reason 是否在同一项目内形成可观察差异；优先寻找能先写出失败回归的实际交叉缺口，不为“看起来不同”引入无证据的历史注入。
