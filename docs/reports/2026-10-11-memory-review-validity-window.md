# 记忆审核有效时间窗口优化

日期：2026-10-11；第二十轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只补记忆审核接受接口的显式 `valid_from` / `invalid_at` 能力。底层记忆表、历史版本查询、项目范围、来源会话绑定和默认写入行为不变；不增加图数据库、Agentic RAG、后台 worker 或外部记忆服务。业务种子、冻结评测集、gold、提示词、模型和指标均未修改。

验收条件：

1. 人工审核接受一条记忆时，可以在同一事务中提交有效时间窗口。
2. 窗口内的 point-in-time 查询命中，窗口外查询不命中；已有 `as_of` 合同保持有效。
3. 审核决定摘要绑定时间字段；同一审核用原窗口重放，改窗口重试必须冲突。
4. HTTP 审核接口把字段传入存储层并返回窗口；执行完整 `scripts/check.ps1`。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 README 描述了带有效时间窗口、来源 provenance、增量更新和历史查询的 temporal context graph。这里只借鉴“事实有效性不能只由写入时间代替”的边界，不引入 Graphiti 图运行时。
- [LongMemEval 固定仓库 commit](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 将时间推理、知识更新和跨会话信息作为独立能力，并区分交互时间戳和显式时间信息。这里只把它转译为本地审核/检索契约，不复制其数据或把论文指标当成本地结论。

现状缺口是：`memories` 和 `memory_versions` 已支持有效区间，`/learning/memories/{memory_id}` 也能事后修改区间，但 `ReflectionDecision` 原先只有接受/拒绝和正文编辑字段。跨会话提炼出的历史事实必须先按当前时刻接受，再额外 PATCH；审核提交本身无法留下“这条事实从何时到何时成立”的确认记录。

## B0 与候选实现

### B0

- 条件：临时加密 SQLite 工作区；不调用模型、Redis、PostgreSQL 或外部副作用；只验证审核决定与 point-in-time 检索。
- 失败优先回归：`tests/test_learning_store.py -k review_decision_can_commit_explicit_memory_validity_window`。
- 旧实现结果：**1 failed, 97 deselected**；失败为接受结果缺少 `valid_from`，说明决定字段被完全丢弃。

### Candidate

- `ReflectionDecision` 新增可选、非负的 `valid_from` 和 `invalid_at`。
- `_candidate_final_payload()` 仅对 memory candidate 规范化并校验时间窗口；无字段的旧调用保持默认行为。
- `_decision_digest()` 纳入两个时间字段，保证审核重放必须复用同一窗口。
- 非 memory candidate 携带记忆有效字段会明确拒绝，避免把字段静默写进 skill。
- 新增存储与 HTTP 回归，验证窗口内命中、窗口外拒绝、原窗口重放以及改窗口冲突。

## 验证证据

- 候选记忆有效性回归：**3 passed, 95 deselected**。
- HTTP 反思来源绑定与时间窗口回归：**1 passed, 143 deselected, 1 warning**。
- 学习存储、反思 API、时间解析、记忆质量门、会话存储组合：**345 passed, 1 warning**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1737 passed, 1 warning，434.64 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG **4/4**；workflow 角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 完整日志：[memory-review-validity-window-full-check-20261011.log](../../build/quality-gates/memory-review-validity-window-full-check-20261011.log)。
- 当前 SHA-256：
  - `core/learning_store.py` = `ccc3643c15e0a7b6ef795557b450c4a8e313c82a83113a353ede57bc80493a9a`
  - `core/mcp_server.py` = `f13f1ee3ce6bb409289c8db911fd744592827af6b499d32823a7145a2d928703`
  - `tests/test_learning_store.py` = `6210d6c9100f9383ef32487c9231b5d9ea8a54a89d5bc07f281d3941d7c7e55e`
  - `tests/test_mcp_server_learning.py` = `a1f0d833dca5539969940d8473339db7f39b5c2cb0d03124073384ff7a730a15`

## 边界与下一轮

本轮证明的是本地人工审核可以原子提交显式有效窗口，不是自动从会话日期推断事实有效期。当前窗口仍由审核者提供；没有用户确认就不会把消息时间戳自动升级为记忆 truth interval。现有 7 个固定记忆场景、8 个查询仍是确定性合同门，不是 LongMemEval 质量分数；没有证明跨会话答案质量、拒答率、开放式时间推理、真实 Redis/PostgreSQL、断电恢复、跨主机 exactly-once 或真实 UI/E2E。

下一轮继续审计跨会话更新与历史版本的来源聚合/拒答证据，优先寻找能先写失败回归的真实缺口；不因本轮全量通过而宣称记忆系统或项目整体完成。
