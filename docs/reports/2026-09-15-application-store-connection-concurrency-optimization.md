# SoulDrive 应用存储连接并发优化

日期：2026-09-15

## 外部实践对照

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)、[SQLite isolation](https://www.sqlite.org/isolation.html) 与 [SQLite WAL](https://www.sqlite.org/wal.html)：数据库提供事务和隔离，但 Python `sqlite3` 的同一连接若被多个线程交错调用，仍需要应用层串行化连接操作；`check_same_thread=False` 不是并发一致性保证。
- [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery)：可恢复执行的关键是把状态检查点和结果写入做成可重放、可验证的持久边界；本机单用户版本先保证本地状态库不因线程交错丢写。
- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http)：幂等键只能在状态读写边界被可靠串行化时成立；因此项目创建、删除回执、记忆反馈和 Skill 运行的既有幂等合同不能只依赖 UNIQUE 约束。

## 失败优先发现

静态扫描发现以下本机应用存储都使用了 `check_same_thread=False`，但没有连接级互斥：

- `SecureDocumentStore`：4 个线程各导入 12 个加密 PDF 到同一清单连接，复现 `cannot start a transaction within a transaction`，部分批次失败。
- `ProjectCatalog`：4 个线程各创建 20 个带唯一幂等键的项目，复现 `OperationalError: not an error`，无法保证项目目录完整落盘。
- `LearningStore`：4 个线程各写入 12 条用户确认的检索反馈，复现 `OperationalError/DatabaseError: not an error`，记忆相关持久化入口存在同类风险。

这三个问题都发生在 RAG/记忆链路上游：文档清单缺失会让索引源不完整，项目目录竞争会破坏范围和幂等回放，学习存储竞争会影响记忆、反馈和 Skill 派生投影。它们不需要引入新的 Agentic RAG 框架即可修复。

## 最小实现

- 三个存储各自增加连接级 `threading.RLock` 和可重入同步装饰器，保护所有公开读写/关闭入口；内部公开方法嵌套调用不会因重复加锁死锁。
- SQLite 连接 timeout 统一设为 30 秒；RLock 解决同一 Python 实例的线程交错，timeout 为其他连接的短暂事务竞争提供等待窗口。
- 不改变项目/记忆 schema、幂等键、删除回执、项目范围、加密对象、RAG 过滤或研究窗口角色合同。
- 增加失败回归：文档 4×12 导入、项目 4×20 创建、LearningStore 4×12 反馈写入；均验证零异常与完整记录。

## 验证证据

- 修复后底层定向：SecureDocumentStore `6 passed`；ProjectCatalog `30 passed`；LearningStore `89 passed`。
- 应用交叉回归：MCP 学习/项目/会话 `259 passed, 1 warning`；RAG 文档/索引/知识库组合 `60 passed, 1 warning`。
- 冻结评测：memory `7/7` 场景、`8/8` 查询，recall@k `1.0`，stale/forbidden leakage `0.0`；GraphRAG route `4/4`；episodic workflow `5/5`；research workflow roles `6/6`，各角色/准备度/检索/引用门为 `1.0`。
- 真实本地 GraphRAG acceptance：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全为 `true`；hybrid/global/local 三类查询均通过并保持引用绑定。运行中出现一次 Ollama HTTP 500，既有一次重试后仍完成验收，记录为外部模型服务瞬时波动，不将其隐藏为零错误。
- `scripts/check.ps1`：后端 `1617 passed, 1 warning`；前端生产构建成功、UI 脚本测试通过；Python `compileall` 与锁定 Cargo 通过。

## 边界

- 本轮保证的是同一 Python 存储实例/连接的线程安全；不同进程依赖 SQLite 自身事务/锁和 timeout，不宣称跨主机分布式 exactly-once。
- 锁只保护数据库连接临界区，不把加密对象文件、SQLite 清单、向量/关键词/图索引变成一个跨存储断电原子事务；既有补偿和可重建索引合同保持不变。
- 冻结评测证明固定样本上的作用域、路由和输出合同未退化，不证明模型答案质量、召回率或记忆长期个性化质量提升。
- 真实 Redis/PostgreSQL/SMTP、跨主机 worker、Tauri/WPS UI/E2E、断电恢复和长历史性能仍未验证。
