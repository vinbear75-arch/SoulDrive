# SoulDrive 加密文档删除失败补偿优化

日期：2026-09-15

## 外部实践对照

- [AIppocampus encrypted sync architecture](https://github.com/Sapientropic/AIppocampus/blob/main/docs/architecture/ops/encrypted-sync-v1.md)：加密对象与权威清单有明确的发布顺序，不能让清单指向不存在的对象。
- [SlateDB manifest two-phase protocol](https://github.com/slatedb/slatedb/blob/main/rfcs/0001-manifest.md)：对象写入/删除与权威 manifest 分开处理，失败后依靠恢复或 GC，而不是把未完成状态当作成功。
- [portable-memory deletion/tombstone specification](https://github.com/MacPaw/portable-memory/blob/main/Spec/portable-memory-spec.md)：删除需要覆盖事实源和派生索引，并保留可追踪的未完成边界。

本轮只补本机 SQLite 清单提交失败时的可逆恢复，不引入 tombstone/GC 框架。

## 发现的缺口

`SecureDocumentStore.delete_document_object()` 原来先删除 `.sdoc`，再提交 `documents` 表删除。如果清单提交失败，表中仍保留记录，但对象已经消失；后续读取会变成认证/文件缺失错误，现有删除 receipt 也无法安全地再次读取同一版本。

## 最小实现

- 删除对象和清单期间捕获异常。
- 对 SQLite 清单执行 `rollback()`。
- 用已经读出的明文 payload 重新生成同一 object ID 的加密对象；恢复失败才升级为“对象无法恢复”的异常。
- 清单提交成功后的物理删除/外部 receipt 语义不改变，继续由上层 `uncertain` 处理无法证明的结果。

## 验证证据

- `tests/test_secure_document_store.py tests/test_secure_object_store.py tests/test_mcp_server_papers.py tests/test_project_catalog.py`：`54 passed, 1 warning`。
- 新增失败优先回归：清单 commit 被中断时，旧记录仍可读；随后同一对象可正常删除。
- `scripts/check.ps1`：后端 `1591 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 如果数据库提交结果在底层已经提交但调用方没有收到确认，无法在本机证明文件与 SQLite 的跨存储原子性；上层仍必须返回 `uncertain`，不自动伪报 completed。
- 孤儿对象的后台 GC、真实断电恢复、Redis/PostgreSQL、Tauri/WPS UI 和打包端到端仍未验证。
