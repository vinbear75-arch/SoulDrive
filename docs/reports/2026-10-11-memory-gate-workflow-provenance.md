# 记忆读取门的项目与研究窗口 provenance

日期：2026-10-11；第二十四轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只补齐 workflow 上下文记忆 gate 审计的项目和窗口身份。`learning.injected_into_rag` 与空结果的 `learning.memory_gate_decision` 必须能单独回答“哪个项目、哪个会话窗口、为什么接受或跳过”；不修改记忆内容、检索候选、冻结数据、gold、模型或窗口输出合同。

验收条件：

1. 空结果 gate 事件包含 `project_id` 和 `workflow_id`，且保留上一轮的 `decision_reason`。
2. 有记忆/技能/episodic 内容时的注入事件也携带同样的项目/窗口身份；`/learning/search` 既有身份字段保持不变。
3. 运行完整 `scripts/check.ps1`，确认审计字段扩展不改变其他链路。

## 外部机制与本地缺口

- [Graphiti 固定 commit](https://github.com/getzep/graphiti/tree/17ed4b707320f092b5e79bdbd9d45130c7f84259) 的 episode/group provenance 说明事实来源和范围必须可回指。本轮将同一原则落到 SoulDrive 的本地 workflow gate 事件，不引入 Graphiti 图数据库。
- [LongMemEval 固定仓库 commit](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 与 [论文版本 2410.10813v2](https://arxiv.org/abs/2410.10813v2) 将跨会话记忆能力与证据边界分开评估；本轮只保证本地轨迹能区分窗口，不把审计字段当作跨会话答案质量证明。

上一轮已经记录 `decision_reason`，但 `learning.injected_into_rag` / `learning.memory_gate_decision` 的 payload 没有 `project_id`、`workflow_id`。当论文、实验和会议窗口连续运行时，单独读取审计日志无法确认某条记忆 gate 属于哪个范围，难以审查窗口隔离是否真的生效。

## B0 与候选实现

### B0

- 失败优先回归：`tests/test_mcp_server_learning.py -k audit_binds_memory_gate`。
- 旧实现结果：**1 failed, 146 deselected, 1 warning**；空 gate 事件已经存在，但读取 `payload["project_id"]` 时 `KeyError`，证明审计身份没有闭合。

### Candidate

- `core/mcp_server.py` 的 workflow learning audit payload 增加 `project_id`、`workflow_id`，与已存在的 `memory_scope_policy`、`memory_time_scope`、gate reason 一起保留。
- 有内容的 `learning.injected_into_rag` 和无内容的 `learning.memory_gate_decision` 共用同一身份字段；事件类型仍区分“发生注入”和“只记录读取门决定”。
- 搜索接口原有 `project_id`/`workflow_id` 审计字段不改，仅在上一轮的 gate 摘要基础上继续复用。

## 验证证据

- 身份/原因回归：**2 passed, 145 deselected, 1 warning**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1744 passed, 1 warning，414.26 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/mcp_server.py` = `F448BC0576DC717A9AB23D9C5931A2FB1B528037E4B17948236E7CDF3D08A35C`
  - `tests/test_mcp_server_learning.py` = `3F9C46BDAEC30A076FEFE46CFE2B059586D20EF07AAB3877B75411E8F39C1B99`

## 边界与下一轮

本轮只证明本地 audit event 的 project/workflow provenance 字段存在且由当前上下文绑定；不证明历史日志迁移、外部审计 sink、跨进程/跨主机 exactly-once、真实 Redis/PostgreSQL、开放式跨会话答案质量或真实 UI/E2E。审计仍是 best-effort，不能反向替代事实源和权限门。

下一轮继续做研究窗口行为差异审查：用同一项目的 paper/experiment/meeting 会话和记忆 fixture，逐项比较 workflow namespace、episodic 选择、检索 query variants、证据引用命名空间和 skip reason；如果只发现设计上有意的差异，就补一条防回归契约，不为了“看起来不同”引入无必要的历史注入。
