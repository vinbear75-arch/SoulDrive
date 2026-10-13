# 记忆派生投影回执可见性优化

日期：2026-09-15  
范围：反思/运行时记忆写入后的本地事实源与派生投影 outbox 确认状态。

## 外部实践与当前缺口

本轮对照 [Debezium Outbox Event Router](https://debezium.io/documentation/reference/transformations/outbox-event-router.html) 的事件唯一 ID/下游去重边界、[Kafka transactional messaging](https://cwiki.apache.org/confluence/spaces/KAFKA/pages/66854913/KIP-98%2B-%2BExactly%2BOnce%2BDelivery%2Band%2BTransactional%2BMessaging) 的 producer/transaction 确认边界，以及 [Temporal retry policy](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 对 Activity 重试幂等性的要求：主状态已提交不等于派生消费已确认，确认失败必须保留可重试身份，不能把未确认投影伪装成完成。

SoulDrive 的 replay worker 已经检查 `complete_memory_sync()` 返回值；但同步 API 的 `_mark_learning_memory_sync()` 只调用该方法而忽略布尔结果，导致文件事实源成功、投影回执因并发 claim/租约栅栏未确认时，响应仍保持普通 `storage_sync.status=ok`。

## 最小改动

- `_mark_learning_memory_sync()` 返回实际完成/失败回执结果；异常也转为未确认并记录 warning，不把异常静默视为完成。
- `_mark_accepted_memory_syncs()` 将每个 memory item 写入 `sync_receipt=completed|pending`；只要有一个投影回执未确认，整体 `storage_sync.status` 从 `ok` 降为 `degraded`。
- 事件仍留在可重放 outbox 中，后续 replay 可以继续完成投影；本轮没有把本机链路宣称为 exactly-once。

## 回归证据

- 失败优先：模拟 `complete_memory_sync() -> False`，验证 API 投影状态为 `degraded`，item 为 `sync_receipt=pending`；该路径旧实现不会暴露未确认状态。
- `tests/test_mcp_server_learning.py`：`129 passed, 1 warning`，约 210 秒。
- 完整工程门禁：后端 `1581 passed, 1 warning`（约 363 秒），前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机事实源与本地派生 outbox 的状态诚实性改进，不是跨主机 exactly-once。
- 没有真实 Redis/PostgreSQL、CDC connector、长时间崩溃恢复或外部向量服务联调；投影召回率、p95 和模型答案准确率不由本轮证明。
