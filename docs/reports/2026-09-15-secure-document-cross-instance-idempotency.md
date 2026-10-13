# SoulDrive 加密文档跨实例导入幂等优化

日期：2026-09-15

## 外部实践对照

- [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests)：同一输入重试应回放既有结果，而不是把一次已开始的操作误报成新失败或再次产生副作用。
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) 与 [SQLite isolation](https://www.sqlite.org/isolation.html)：唯一约束可以拒绝重复记录，但不能单独协调“检查清单—写加密对象—提交清单”的整个本机临界区。
- [Python `msvcrt.locking`](https://docs.python.org/3/library/msvcrt.html) 与项目已有 `local_file_lock`：Windows 本机多实例需要同一 sidecar 锁协议，不能只依赖每个 Python 对象自己的 `RLock`。

## 失败优先发现

两个独立 `SecureDocumentStore` 实例同时导入相同内容时，双方都可能先读到清单中不存在该摘要，然后并发写同一个确定的 `.sdoc` 对象路径。在 Windows 上稳定复现了 `WinError 5` 的原子替换冲突；即使对象写入侥幸完成，清单唯一摘要约束也可能把第二次幂等重试暴露为异常，而不是 `already_present`。

## 最小实现

- 在文档清单旁使用已有 `local_file_lock` 的 sidecar 锁，覆盖 `import_document()` / `import_document_bytes()` 的“查摘要、写加密对象、插入清单”完整临界区。
- 保留实例内 `RLock`，两层锁分别解决同一实例线程交错和本机多实例/多进程竞争。
- 第二个相同内容请求在锁释放后重新读取清单，返回既有 `already_present` 结果；不改变加密对象格式、清单 schema、项目范围或 RAG 索引合同。

## 验证证据

- 失败优先：跨实例同内容导入复现 Windows `WinError 5`。
- 修复后文档存储定向：`8 passed`，其中跨实例结果严格为一个 `imported`、一个 `already_present`，清单记录数为 1；同一实例 4×12 并发导入也完整通过。
- 文档/RAG 交叉回归：`32 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1618 passed, 1 warning`；前端生产构建成功、UI 脚本测试通过；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这是本机遵守同一 sidecar 锁协议的跨实例幂等，不是跨主机分布式锁，也不宣称外部存储或外部 RAG 服务 exactly-once。
- 锁保护导入临界区，但 SQLite 清单和加密对象仍是两个持久层；断电时的跨层原子提交仍依赖既有补偿/重建路径，未做真实断电测试。
- 同一内容的业务语义被视为重复导入；不同显示名仍由内容摘要与对象 ID 规则决定，未扩大文档去重策略。
