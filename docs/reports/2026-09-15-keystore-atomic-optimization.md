# SoulDrive 工作区 keystore 原子轮换优化

日期：2026-09-15

## 外部实践对照

- [atomic-jsonwrite 的 checkpoint 持久化模式](https://github.com/protectyr-labs/atomic-jsonwrite)：状态 JSON 通过临时文件、`fsync`、原子替换避免零字节或半写文件。
- [AxCrypt 的原子加密文件替换](https://github.com/Siddh7-ai/AxCrypt)：加密文件轮换先写同目录临时文件并同步，再替换旧文件，失败时保留旧密文。
- [Python `os.replace` / `os.fsync` 文档](https://docs.python.org/3/library/os.html)：明确了缓冲刷新、描述符同步和最终替换的职责边界。

## 发现的缺口

工作区 keystore 的初始化、v1→v2 迁移和口令轮换原来直接覆盖 `keystore.json`。一次中断可能让工作区处于“旧密钥已被截断、新密钥尚未完整发布”的不可解锁状态。

## 最小实现

- `_write_payload()` 先将完整 keystore JSON 写入同目录临时文件。
- 临时文件 `flush/fsync` 并关闭后，使用 `os.replace` 发布；失败时清理临时文件，旧 keystore 保持不变。
- 只改变本地 keystore 文件可见性，不改变 KDF、AES-GCM、旧版本兼容或口令语义。

## 验证证据

- `tests/test_workspace_crypto.py tests/test_secure_object_store.py tests/test_secure_document_store.py`：`18 passed`。
- 新增轮换失败回归：`fsync`/替换失败时旧口令仍可解锁，旧 payload 字节不变，临时文件无残留。
- `scripts/check.ps1`：后端 `1590 passed, 1 warning`；前端生产构建成功、UI `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是单个 keystore 文件的原子替换，不证明多进程同时轮换、硬件/磁盘断电、备份恢复或跨文件事务。
- 真实 Tauri 启动、打包安装、Redis/PostgreSQL 和跨主机 exactly-once 仍未验证。
