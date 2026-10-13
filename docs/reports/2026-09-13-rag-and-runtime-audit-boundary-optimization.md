# RAG 与运行时审计旁路边界优化

日期：2026-09-13

## 对照实践

- [Temporal Retry Policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 要求 Activity 可重试且具备幂等性；工作流状态不能因为某个可重试副作用失败而被错误重放。
- [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) 把日志/遥测导出作为独立处理链，并记录导出失败；其公开 issue 也说明遥测导出器自身可能失败或递归，不能反过来阻塞业务。
- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle) 与 [Dapr Transactional Outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 说明真正的 durable/outbox 需要持久化状态与重放协议；本轮没有把本地 JSONL 审计伪装成事务性 outbox。

## 失败优先发现

- RAG 流式回答的多个阶段都会写 `rag.*` 审计事件。审计 sink 抛异常时，旧路径可能在证据已经确定或回答已经可生成时中断 SSE，业务结果与观测副作用混为一个失败。
- 运行时解锁/锁定、sidecar 启动/信号/停止和 PPT 任务收尾也存在同类直接写入；这些事件失败会把已完成的状态转换或产物任务误报为失败。
- 各模块各自捕获异常会造成口径漂移：有的回显异常文本，有的吞掉失败，有的递增指标。需要一个小而明确的本地边界。

## 本轮修改

- 在 `core.audit_log.append_audit_event_best_effort()` 建立统一旁路入口：成功返回 `True`，写入失败返回 `False`，递增 `runtime_metrics.audit_failures`，日志只记录事件类型，不回显 payload、查询、路径或异常文本。
- MCP API 继续把失败映射为固定 warning；RAGEngine 的审计包装、运行时锁定/解锁、sidecar 生命周期/信号、PresentationService 统一复用该入口。
- RAG 业务链路不改变项目过滤、GraphRAG 路由、证据门、引用校验、查询改写或有限重试；这里只修复“审计失败覆盖业务结果”的边界。
- 安全授权仍保持 fail-closed：审计 best-effort 不会让未解锁请求获得数据，也不改变真实检索/模型/文件操作错误。

## 验收证据

- 失败注入定向回归：`131 passed, 1 warning`，覆盖审计 helper、RAG 流、运行时锁定/解锁、PPT 审计和 `/shutdown`。
- 最新 `scripts/check.ps1`：后端 `1335 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。
- `python -m compileall -q core tests` 通过；当前源码中 RAG 的直接调用均经 `RAGEngine.audit_logger` 属性包装，其他运行时调用经统一 helper。

## 边界

- 这是本机单用户审计旁路与业务状态的故障隔离，不是审计 outbox、自动补偿、远程 trace、跨进程 exactly-once 或 durable execution 引擎。
- `audit_failures` 只说明本次进程内观测写入失败次数，不证明事件已补偿、已上传或不可篡改审计链仍完整。
- 这不构成 RAG 质量提升结论；Recall@K、MRR、nDCG、证据覆盖、引用正确性、串项目率和答案正确性仍需冻结数据集后单独评测。
- 完整检查不等于真实 Tauri/WPS UI、安装包冷启动、Windows 信号链或用户点击 E2E；这些仍是独立交付门。
