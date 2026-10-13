# SoulDrive 本地 durable 账本与会话归档并发写入优化

日期：2026-09-15

## 外部实践对照

- [Python `msvcrt.locking`](https://docs.python.org/3/library/msvcrt.html) 与 [Python `fcntl`](https://docs.python.org/3/library/fcntl.html)：用标准库对同目录 sidecar 文件加本机进程锁，兼顾 Windows 主运行环境和测试环境。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与 [SQLite isolation](https://www.sqlite.org/isolation.html)：本地索引事务只能保护 SQLite 自身；与 JSONL/JSON manifest 组合时，仍需显式协调文件事实源的读—改—写边界。
- [atomic-jsonwrite](https://github.com/protectyr-labs/atomic-jsonwrite) 与 [Tonbo manifest recovery](https://github.com/tonbo-io/tonbo/blob/main/docs/overview.md)：原子替换解决半写快照，但不自动解决并发读—改—写的丢失更新。
- [OpenParallax audit recovery](https://docs.openparallax.dev/audit/)：审计和派生记录都必须保留可重建的顺序/来源约束，不能用“最后一次写入成功”掩盖前一次更新丢失。

## 发现的缺口

本轮前，`document_usage.json` 已采用临时文件、`fsync`、`os.replace`，但 `record_document_hits()` 与 `forget_document()` 没有跨实例锁。失败优先用初始计数 1 和两个 barrier 同步的并发命中稳定复现最终计数为 2，而预期为 3。

同样的边界存在于会话事实源：两个独立 `ConversationStore` 实例同时追加同一 session 时，都可能从 `MAX(sequence)=0` 计算出 `sequence=1`；两条正文虽存在，消息顺序和后续摘要覆盖范围却失去可靠性。

记忆加密归档也有相同问题：两个实例同时把同一 `memory_id` 写到新版本时，会竞争固定的 `.tmp` 文件；失败优先验证实际复现了 Windows `WinError 32`，而不是按版本冲突契约稳定返回。

## 最小实现

- 新增 `core/local_file_lock.py`：路径级进程内 `RLock` 加 Windows `msvcrt.locking`/Unix `fcntl.flock`，锁文件只承担本机协调，不承载业务数据。
- 审计链复用该锁，保护读尾 hash、JSONL append、`fsync` 和 checkpoint 更新的同一临界区。
- 文档使用账本在 `record_document_hits()`/`forget_document()` 内完成完整读—改—写；只读 `document_weight()` 继续读取原子完整快照，不把所有检索查询串行化。
- 会话 `create_session()`、同 session `append()`、`write_summary()`、`delete_session()` 使用归档根目录下 `.locks/<session_id>.lock`；锁文件不放在待删除的会话目录内。会话追加同时覆盖加密 JSONL、SQLite rebuildable index 和 manifest 更新。
- 记忆 `archive_memory()`/`delete_memory()` 使用 `.locks/<memory_id>.lock`；同版本不同正文在串行后由既有 `memory version conflict` 门拒绝，不静默覆盖。
- 本地加密 outbox 的追加读—改—写也使用独立 sidecar 锁，保留既有 SQLite claim/lease/token fencing。

## 验证证据

- 失败优先：文档账本并发计数丢失；会话并发追加产生重复 `sequence=1`。
- RAG foundation 定向：`8 passed`；RAG/GraphRAG/安全检索/知识库组合：`109 passed`。
- 会话存储完整定向：`91 passed`；会话、记忆、MCP 学习、项目目录交叉回归：`337 passed, 1 warning`。
- 记忆并发冲突定向：`3 passed`；会话+学习存储回归：`181 passed`。
- `scripts/check.ps1`：后端 `1609 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机遵守同一锁协议的并发保护，不是跨主机锁、分布式事务或外部副作用 exactly-once；真实 Redis/PostgreSQL/SMTP 联调仍未完成。
- JSONL/manifest 与 SQLite 仍是多个事实/派生层，锁只能保证本地写入顺序，不能把跨文件更新变成一个断电原子事务；真实断电、长时间崩溃和目录项持久化仍未验证。
- 其他程序直接编辑文件、绕过锁协议或手工删除 lock sidecar 时，系统只能依靠现有身份/链校验发现异常，不能阻止外部破坏。
- 真实 Tauri/WPS UI/E2E、跨主机部署和长历史性能仍未验证。
