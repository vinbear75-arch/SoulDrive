# 检索诊断与反馈审计边界优化

日期：2026-09-13

## 对照实践

- [Langfuse 开源仓库](https://github.com/langfuse/langfuse) 将 retrieval、embedding、agent action 作为可观测操作，并把用户反馈、人工标注和数据集评测作为独立能力；这说明观测与业务质量反馈应可关联，但不应成为业务主结果的单点失败源。
- [Langfuse Observability 文档](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/observability/overview.mdx) 描述了异步排队、批量发送的 trace 记录方式；本机单用户版本不引入远程观测服务，但沿用“主链路与观测副作用分离”的边界。
- [LlamaIndex RetrieverEvaluator](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/evaluation/retrieval/evaluator.py) 在评测中保留检索节点 ID 与文本，并允许后处理器参与评测；SoulDrive 继续保留项目、来源、页码和证据摘要，不把审计事件当作检索结果本身。
- [Graphiti add_memory 静默丢失问题](https://github.com/getzep/graphiti/issues/1707) 提醒本地知识写入/后台处理必须明确成功与失败信号；本轮只修正已完成诊断或反馈被审计异常覆盖的问题，不宣称已经具备跨存储 outbox。

## 失败优先发现

`/diagnostics/retrieval` 的检索结果计算完成后仍直接写 `retrieval.diagnostics_test`；检索失败路径在准备返回 503 时直接写 `retrieval.diagnostics_failed`。两个审计调用一旦抛错，都会覆盖原本的成功结果或真实的检索失败。

检索反馈保存和撤销也存在同样问题：LearningStore 已经提交反馈事实后，`retrieval.feedback_saved` 或 `retrieval.feedback_revoked` 的审计异常会把 200 改成 500。旧的审计异常字符串还可能包含查询文本，不应直接返回给 UI。

## 本轮修改

- 统一通过 `_append_audit_best_effort` 写检索诊断和反馈审计。
- 诊断成功仍返回 `retrieval_test_v1`；审计失败仅附加 bounded `warning`。
- 诊断真实失败仍返回 503；审计失败不会覆盖真实错误语义，但会附加 warning。
- 反馈保存/撤销在本地事实成功后仍返回 200 和反馈对象；审计失败只附加 warning。
- 审计异常详细信息只保留在本机日志；响应只返回固定提示，避免查询或秘密回显。
- 不改变 project/global 过滤、查询改写、反馈可逆性、`top_k` 或只读诊断契约。

## 验收证据

- 先添加失败优先回归：诊断成功审计失败、诊断失败审计失败、反馈保存/撤销审计失败；旧实现 3 项均返回 500，测试因此先红。
- 修复后检索诊断/反馈定向回归 `8 passed, 1 warning`。
- 检索、学习、RAG 组合回归 `204 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1317 passed, 1 warning`；前端构建通过，8 组 UI 测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是 Recall@K、MRR、nDCG、答案正确率或证据覆盖率提升；本轮只修复诊断/反馈结果与观测副作用的故障隔离。
- 这不是 Langfuse 服务接入、远程 trace、自动反馈学习或 Agentic RAG。
- 这不是事务性审计 outbox、跨存储 exactly-once 或自动补偿；审计 sink 仍可能丢事件，只是不再伪造检索业务结果。
- 这不改变索引、GraphRAG、项目隔离和 fail-closed 授权边界；完整工程门也不等于真实 Tauri/UI/E2E。
