# SoulDrive 工作区清单与运行状态快照原子发布优化

日期：2026-09-15

## 外部实践对照

- [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery)：恢复入口应依赖已持久化的完整 checkpoint，而不是半写状态。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与 [isolation](https://www.sqlite.org/isolation.html)：本机状态更新要明确可见边界；读者不应看到写入中的中间内容。
- [Python `os.replace` / `os.fsync`](https://docs.python.org/3/library/os.html)：在替换工作区可见文件前先同步完整临时文件。

## 发现的缺口

工作区首次初始化的 `workspace.json` 原来直接写入；运行状态虽使用固定 `.tmp` 后替换，但没有 `fsync`。在写入失败或并发状态更新时，可能留下半写 JSON、固定临时文件冲突，恢复逻辑随后只能把坏快照当作默认状态，掩盖实际状态丢失。

## 最小实现

- `workspace.json` 和 `runtime_state.json` 统一使用 `core.atomic_file.atomic_write_text()`。
- 写入路径改为同目录唯一临时文件、`flush/fsync`、`os.replace`，异常时清理临时文件并保留旧完整快照。
- 不改变工作区目录布局、运行状态字段、索引状态机、锁定策略或恢复语义。

## 验证证据

- 工作区/运行状态定向：`19 passed`。
- 索引 worker、索引幂等、诊断、审计、运行状态审计和 RAG pipeline diagnostics：`56 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1599 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。
- 失败优先回归确认 fsync 失败时 manifest/state 不出现，且同目录临时文件无残留。

## 边界

- 这是单机 JSON 快照的原子可见性改进，不证明多进程读改写合并、跨主机 exactly-once、断电目录项持久化或 SQLite/文件跨存储事务。
- 真实 Redis/PostgreSQL、Tauri/WPS UI/E2E、打包启动和长时间崩溃恢复仍未验证。
