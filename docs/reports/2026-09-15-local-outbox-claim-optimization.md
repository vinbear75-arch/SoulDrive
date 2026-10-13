# 本机加密 outbox 跨实例 claim 优化记录

日期：2026-09-15  
范围：`ConversationStore` 本机加密 JSONL outbox、`ProductionSessionStore.replay_outbox()`、SQLite 控制面 claim 状态。

## 1. 外部实践与本项目缺口

本轮对照了 SQLite 官方事务/隔离说明、PostgreSQL `FOR UPDATE SKIP LOCKED`、DBOS 的数据库 checkpoint/recovery 以及 Restate 的持久幂等键边界：

- SQLite 的 `BEGIN IMMEDIATE` 会先取得写事务；SQLite 在一个数据库文件上串行化写入，适合本机多连接 claim 控制面，不适合被描述成跨主机队列。
- PostgreSQL outbox 已使用行锁、`SKIP LOCKED`、lease 和 claim token；本轮发现本地降级 outbox 仍只持有 `ProductionSessionStore` 实例内的线程锁。
- durable execution 仍需区分“只提交了 durable 状态”和“外部副作用已经完成”：worker 在副作用后崩溃时仍可能 at-least-once，因此完成提交必须受 token fencing，外部投影也必须依赖事件身份幂等。

可核查资料：

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite isolation](https://www.sqlite.org/isolation.html)
- [PostgreSQL SELECT locking](https://www.postgresql.org/docs/current/sql-select.html)
- [DBOS architecture and recovery](https://docs.dbos.dev/architecture)
- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http)

旧实现的可复现缺口是：两个独立 `ConversationStore`/`ProductionSessionStore` 实例同时读取同一个 pending 事件时，实例内锁互不相识，会把同一个本地事件 dispatch 两次。

## 2. 最小改动

- 在会话 SQLite 控制面新增 `outbox_delivery_claims`，不把业务正文或密文搬出加密 JSONL；表只保存事件 ID、状态、owner、claim token、lease 到期时间和错误摘要。
- 新增 `claim_outbox()`：使用 `BEGIN IMMEDIATE` 原子抢占，活动 lease 阻止其他连接重复消费，过期 lease 可恢复。
- `pending_outbox()` 读取 claim 状态：活动 claim 不再进入待消费列表，`completed/cancelled` 事件即使旧 JSONL 快照仍为 pending 也不再返回。
- `mark_outbox()` 支持 token fencing：旧 worker 或过期 token 不能覆盖新 worker 的完成/失败状态；完成状态保持单调，关闭后的 archive 仍保留原有只读 pending 语义。
- 本机 replay 在真正 dispatch 前 claim，在成功/失败提交时携带 token；外部副作用仍按 at-least-once 处理，不宣称 exactly-once。

## 3. 失败优先与回归证据

先加入双实例回归，旧实现稳定复现 `dispatch_count=2`；修复后：

- 独立 session store 并发 replay：同一事件只 dispatch 一次；
- 活动 lease 对其他 worker 隐藏；
- lease 到期后可被新 worker 接管；
- 旧 claim token 的完成提交返回失败，新 token 可以完成；
- 原有单实例 replay、失败重试、关闭后读取和记忆向量 outbox 行为保持通过。

本轮定向结果：

- `tests/test_conversation_store.py`：`90 passed`；
- `tests/test_conversation_store.py tests/test_mcp_server_learning.py tests/test_mcp_server_projects.py`：`252 passed, 1 warning`；
- `scripts/check.ps1`：后端 `1576 passed, 1 warning`（约 349 秒），前端生产构建成功，8 组 UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 通过；
- 随后 PostgreSQL completion fencing 回归后的完整门禁：后端 `1577 passed, 1 warning`（约 347 秒），前端生产构建成功，UI `62 passed`，`compileall` 与锁定 Cargo 通过；
- `git diff --check`：无 whitespace error；已有工作树仅报告 CRLF 转换提示。

## 4. 边界

- 这是本机同一 SQLite 文件的跨进程/跨实例 claim 证据，不是跨主机队列或分布式锁。
- worker 在外部副作用完成后、状态提交前崩溃，重启后仍可能重复 dispatch；事件接收端必须继续使用已有事件身份、版本和幂等语义。
- 没有真实 PostgreSQL/Redis 联调、长时间进程崩溃注入、打包 Tauri sidecar 或真实 UI/WPS E2E；这些不由本轮测试覆盖。
