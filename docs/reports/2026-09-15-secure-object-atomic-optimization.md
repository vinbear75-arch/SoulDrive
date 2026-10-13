# SoulDrive 加密文档对象原子写入优化

日期：2026-09-15

## 外部实践对照

- [AxCrypt 的原子加密文件替换说明](https://github.com/Siddh7-ai/AxCrypt)：加密内容写入同目录临时文件、`fsync` 后再 `os.replace`，避免目标文件处于半写状态。
- [atomic-jsonwrite](https://github.com/protectyr-labs/atomic-jsonwrite)：对 Agent checkpoint/config 使用临时文件、同步、原子替换，并把失败清理作为协议的一部分。
- [AIppocampus encrypted sync architecture](https://github.com/Sapientropic/AIppocampus/blob/main/docs/architecture/ops/encrypted-sync-v1.md)：加密对象先完成，清单指针后写入；事实源和派生/索引边界需要明确。

本轮只借鉴本机加密对象的原子可见性，不引入新的对象存储或跨存储事务框架。

## 发现的缺口

`SecureObjectStore.write_bytes()` 原来直接对最终 `.sdoc` 路径执行 `Path.write_text()`。文档清单在对象写入之后才提交；若加密 envelope 写入中断，最终路径可能被截断，后续读取只能得到认证失败，且清单/对象边界难以恢复。

## 最小实现

- 加密 envelope 先编码到同目录随机临时文件。
- 临时文件写完后 `flush()`、`os.fsync()`，关闭句柄后以 `os.replace()` 发布。
- `fsync` 或替换失败时删除临时文件并保留旧 `.sdoc`，异常继续返回调用方；现有 AES-GCM、AAD、对象 ID 和文档清单语义不变。

## 验证证据

- `tests/test_secure_object_store.py tests/test_secure_document_store.py tests/test_mcp_server_papers.py`：`25 passed, 1 warning`。
- 新增失败优先回归：`fsync`/`replace` 失败时旧加密对象仍可认证读取，临时文件无残留。
- `scripts/check.ps1`：后端 `1588 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是单个加密对象的原子可见性改进，不证明对象文件与 SQLite 文档清单跨存储原子提交；清单提交失败后的孤儿对象回收仍是后续可单独评估的生命周期问题。
- 不证明真实磁盘断电恢复、Redis/PostgreSQL、Tauri/WPS UI 或打包端到端行为。
