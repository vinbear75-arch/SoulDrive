# SoulDrive RAG 文档使用账本原子写入优化

日期：2026-09-15

## 外部实践对照

- [atomic-jsonwrite](https://github.com/protectyr-labs/atomic-jsonwrite)：将 agent checkpoint/config 的 JSON 写入固定为临时文件、`fsync`、`os.replace`。
- [Python `os.replace` / `os.fsync` 文档](https://docs.python.org/3/library/os.html)：写入缓冲刷新和文件描述符同步应发生在最终可见替换之前。
- [Tonbo manifest recovery model](https://github.com/tonbo-io/tonbo/blob/main/docs/overview.md)：派生/辅助状态应以完整快照发布，恢复时优先读取最后一个完整版本。

## 发现的缺口

RAG 的 `document_usage.json` 是派生的引用计数/新鲜度账本。`record_document_hits()` 原来使用临时文件但未 `fsync`，`forget_document()` 则直接覆盖目标文件；中断可能留下半写 JSON，导致读取静默回退为空账本，文档权重观测失真。

## 最小实现

- 两个写入入口统一进入 `_write_usage()`。
- JSON 快照写入同目录临时文件，`flush/fsync` 后以 `os.replace` 发布；失败时保留旧快照并清理临时文件。
- 账本仍是派生观测信号；不会改变事实文档、向量索引、GraphRAG 或答案证据边界。

## 验证证据

- `tests/test_rag_foundation_contracts.py tests/test_secure_document_store.py tests/test_mcp_server_papers.py`：`26 passed, 1 warning`。
- 新增 flush/replace 失败回归：旧账本字节不变且临时文件无残留。
- `scripts/check.ps1`：后端 `1593 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是单机派生 JSON 快照的原子可见性改进，不证明并发写入合并、跨进程锁、断电目录项持久化或 RAG 召回/答案质量提升。
- 真实 Redis/PostgreSQL、Tauri/WPS UI、打包启动和跨主机 exactly-once 仍未验证。
