# 记忆冲突替换的有效边界优化

日期：2026-10-11；第二十一轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只修复“显式时间窗口 + `conflicts_with` 冲突替换”的边界计算。默认没有显式 `valid_from` 的替换仍按当前替换时间生效；不修改冻结数据、gold、模型、提示词或评测指标，不引入 Graphiti/Agentic RAG/外部协调服务。

验收条件：

1. 新事实的 `valid_from` 早于当前写入时间时，旧事实的历史版本在该边界前可查、边界后不与新事实重叠。
2. 新事实的 `valid_from` 晚于当前写入时间时，旧事实保持到未来边界，不提前产生空窗。
3. 未指定 `valid_from` 的现有冲突替换、项目范围和 outbox 行为保持不变；执行完整 `scripts/check.ps1`。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 将事实有效时间、失效窗口、来源 provenance 与增量更新分开。这里借鉴“替换边界是事实有效时间，不等于 ingestion time”的原则，不引入图数据库。
- [LongMemEval 固定仓库 commit](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 把时间推理和知识更新作为独立能力。这里只将其转成 point-in-time 本地回归，不复制 benchmark 数据，也不把外部结果当 SoulDrive 质量结论。

现状缺口是：上一轮已经允许审核决定提交显式 `valid_from`，但冲突归档仍使用当前 `superseded_at` 作为旧事实的历史 `invalid_at`。历史生效的新事实会与旧事实重叠；未来生效的新事实会让旧事实在现在就被截断，造成查询空窗。

## B0 与候选实现

### B0

- 条件：临时加密 SQLite 工作区；两个同范围 `project_fact`，通过明确 `conflicts_with` 替换；不调用模型或外部后端。
- 失败优先回归：`tests/test_learning_store.py -k conflict_supersession_uses_explicit_valid_from_boundary`。
- 旧实现结果：**1 failed, 98 deselected**；在 `as_of=200` 同时返回旧事实和新事实，证明旧版本边界错误地停在当前时间 300，而不是新事实的 `valid_from=150`。

### Candidate

- `_supersede_conflicting_memories_locked()` 增加独立的 `validity_boundary`。
- 归档行的 `updated_at` 仍使用当前 `superseded_at`，避免更新时间倒退；历史版本的 `invalid_at` 使用显式 `valid_from`，因此同时支持历史切换和未来排程。
- 没有显式 `valid_from` 时，`validity_boundary` 回退到原有当前替换时间，保持旧行为。
- 新增历史切换与未来切换两条回归，验证旧事实、新事实在三个 point-in-time 区间内互斥且连续。

## 验证证据

- 新增边界回归：**2 passed, 98 deselected**。
- 学习存储完整回归：**100 passed in 24.93s**。
- 反思/记忆 API 扩大回归：**17 passed, 1 warning in 38.27s**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1739 passed, 1 warning，448.48 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG **4/4**；workflow 角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 完整日志：[memory-conflict-validity-boundary-full-check-20261011.log](../../build/quality-gates/memory-conflict-validity-boundary-full-check-20261011.log)。
- 当前 SHA-256：
  - `core/learning_store.py` = `6bd5624c711737bf0b3b788896128d3462cba3dcc20401fd7cd19e601cfbf517`
  - `tests/test_learning_store.py` = `cb762cb2e2d93cf07a60e9374144e47a8e0149e26212272e1667e023f9497395`

## 边界与下一轮

本轮只保证人工明确声明的 `conflicts_with` 和 `valid_from` 在本地历史查询中形成连续边界；不会自动判断两条自然语言事实是否冲突，也不会从会话创建时间推断事实有效期。当前固定记忆质量门仍只有 7 个场景、8 个查询，不是 LongMemEval 评分；没有证明开放式跨会话答案质量、拒答正确率、真实 Redis/PostgreSQL、断电恢复、跨主机 exactly-once 或真实 UI/E2E。

下一轮继续检查冲突替换后的来源会话/消息 provenance 是否随旧版本和新版本分别保留，并确认无证据时 API 是否明确拒答，而不是只返回一个“最新事实”。
