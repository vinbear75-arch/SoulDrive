# 长期记忆读取门原因可观测性

日期：2026-10-11；第二十三轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围与验收条件

本轮只把上一轮生成的长期记忆 `decision_reason` 贯通到运行指标和本地审计：`/learning/search` 与工作流上下文必须记录记忆门决策；即使没有任何记忆、技能或活动被注入，也要保留一个不含正文的 gate 决策事件。此次不改变记忆召回、scope、有效时间、冻结数据、gold、模型或外部后端。

验收条件：

1. 记忆门 metrics 记录 decision、accepted/rejected 计数和 bounded `decision_reason`，旧的三参数调用仍兼容。
2. 搜索审计包含稳定的 gate 决策摘要；工作流上下文在空结果 skip 时写入独立 `learning.memory_gate_decision`，不把“未注入”误记为“已注入”。
3. 审计失败仍不改变业务结果；执行完整 `scripts/check.ps1`。

## 外部机制与本地缺口

- [MLflow 固定 commit](https://github.com/mlflow/mlflow/tree/09bdb2cca50a1ab31feefd513ea059bfbd252269) 将运行、指标、评估结果和产物 lineage 分开记录。本轮只借鉴“决策结果和运行观测应绑定”的原则，把记忆 gate 的原因加入本地运行轨迹，不引入 MLflow 服务。
- [Temporal Python SDK 固定 commit](https://github.com/temporalio/sdk-python/tree/b3375504ee29bda57585a5eab7e11a7df39b3472) 强调可恢复执行中的状态/回放可观察性。本轮只做本机 best-effort audit 与 metrics，不宣称具备 Temporal server 的 durable execution。

上一轮已经把 skip 原因返回给接口和提示，但 `_learning_context_for_query()` 只有在有内容注入时才写 `learning.injected_into_rag`；`/learning/search` 只记忆数量，不记 gate 原因；`RuntimeMetrics` 也只累计 accept/skip 总数。因此一个“没有可采纳长期记忆”的结果虽然可返回，却不能从运行轨迹稳定区分是无候选、全拒绝、预算截断还是读取异常。

## B0 与候选实现

### B0

- 失败优先回归：`tests/test_mcp_server_learning.py -k audits_memory_gate_reason_when_skipping`。
- 旧实现结果：**1 failed, 144 deselected, 1 warning**；`/learning/search` 返回 skip，但没有调用 `record_memory_gate`，无法保存四元组决策轨迹；同一测试继续检查搜索审计缺少 `memory_gate.decision_reason`。

### Candidate

- `RuntimeMetrics.record_memory_gate()` 增加可选 `decision_reason`，并在 snapshot 暴露最近一次 bounded reason；不改变原有三参数调用的兼容性。
- 工作流上下文将 gate 原因传给 metrics；当没有任何记忆、技能或 episodic 活动时，写入独立 `learning.memory_gate_decision`，有内容时仍使用 `learning.injected_into_rag`，两类事件的语义不混淆。
- `/learning/search` 同样记录 gate metrics，并在 `learning.search_performed` 审计中加入只含 decision/reason/count 的摘要，不写入记忆正文。

## 验证证据

- 搜索空记忆 gate 回归：**1 passed, 144 deselected, 1 warning**。
- 工作流空记忆 gate 回归：**1 passed, 145 deselected, 1 warning**。
- 学习模块完整回归：**146 passed, 1 warning，236.23 秒**。
- 审计/安全组合：**21 passed**。
- RAG 提示与多模态组合：**148 passed, 1 warning**。
- 完整 `scripts/check.ps1`：退出码 **0**；后端 **1743 passed, 1 warning，414.18 秒**；记忆质量 **7/7 场景、8/8 查询、recall@k=1.0、stale/forbidden leakage=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；前端生产构建成功；UI 脚本 **66 passed**；锁定 Cargo 检查通过。
- 当前质量门产物：[memory-quality.json](../../build/quality-gates/memory-quality.json)、[graphrag-route.json](../../build/quality-gates/graphrag-route.json)、[workflow-roles.json](../../build/quality-gates/workflow-roles.json)。
- 当前 SHA-256：
  - `core/observability.py` = `821D28A8C58CAD1A97CF469F11D0E2197893064E16AC01450AD58CAA9F36FD92`
  - `core/mcp_server.py` = `FF2E4D353656932051A0F6AF926F8E1459BF3C499A621E86CDD66DBF7FFD57C8`
  - `tests/test_mcp_server_learning.py` = `347B808C02707FD37262EB0658D604766FF4EE19C5D4F2312712ADBF06F22113`

## 边界与下一轮

本轮只证明本机 metrics/audit 能保留最近一次或单次 gate 原因；审计仍是 best-effort，不证明外部观测系统、跨进程/跨主机 exactly-once、断电恢复、长期统计分布、模型最终拒答率或真实 UI/E2E。新增字段也不等同于 LongMemEval 分数或开放式跨会话质量。

下一轮继续检查三个研究窗口的记忆与 episodic 路由是否具有明确不同作用，尤其核对 `paper_review` 当前不启用 episodic activity 是否是有意的论文证据边界，还是遗漏了跨会话研究上下文；先用真实本地 fixture 写出窗口交叉泄漏/遗漏的失败回归，再决定最小修复。
