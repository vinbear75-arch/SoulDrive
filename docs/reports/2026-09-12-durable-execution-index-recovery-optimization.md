# Durable execution 与增量索引恢复优化

日期：2026-09-12

## 目标

在不引入 DBOS、Temporal 或新的运行时依赖的前提下，检查本机 SQLite 任务日志、PPT 外部步骤和增量索引是否能在进程退出后安全恢复。验收重点是：重试不误认旧 worker、索引不因“新记录已写入但旧记录尚未清理”而永久跳过收尾、同名文档不跨项目删除。

## 外部实践对照

- [DBOS Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial) 将 workflow ID 作为幂等键，已完成 step 不再重跑；但普通 step 仍是至少一次执行，外部副作用必须自行具备幂等语义。
- [DBOS Transactions](https://docs.dbos.dev/golang/tutorials/transaction-tutorial) 明确指出：应用写入与 durable checkpoint 不在同一事务时，进程可能在写入成功、checkpoint 尚未落盘的窗口重跑，因此需要事务绑定或可重放的幂等收尾。
- [AWS transactional outbox](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) 要求把事实写入与待发送事件放在同一事务，并让消费者按事件身份幂等；这对应 SoulDrive 的“索引事实先写、旧派生记录可重试清理”边界。
- [SQLite transactional guarantees](https://www.sqlite.org/transactional.html) 支持本机状态表的原子提交，但不能把 SQLite 事务自动扩展成 Chroma/关键词索引/父文档库之间的跨存储事务。

## 失败优先证据

1. 旧任务 owner 只保存 `PID:nonce`。构造当前 PID 加旧启动时间的 owner 身份时，旧逻辑仅检查 `pid_exists`，会错误地把 PID 已复用的无关进程当作原 worker。
2. 文档更新的实际顺序是“按新 hash upsert -> 删除旧 chunk”。模拟进程在两步之间退出：下一次 `_document_index_status()` 看到新 hash 已存在就返回 `current`，旧 hash 记录因此永久残留。
3. 两个项目使用同名文件时，旧清理逻辑按 `source_filename` 兜底收集记录；更新项目 A 会把项目 B 的同名旧 chunk 一并列入删除集合。

## 最小修复

- 新任务 owner 使用 `PID:process_start_time:nonce`；恢复时同时核对 PID 和进程启动时间，拒绝 PID 复用。无法读取 `psutil` 启动时间时保留 `unknown` 回退，并兼容旧的两段式 owner 字符串。
- 增量索引在确认当前 hash 后，按精确 `source_path` 检查同一文档是否还存在其他 hash；发现残留返回 `stale_cleanup`，下一轮重新摄取并完成旧记录清理。空 hash/缺失 hash 也视为未完成收尾。
- `_delete_previous_chunks()` 接收当前 `project_id`；路径和文件名两路收集都只保留同项目记录。项目元数据缺失时不凭文件名跨项目删除。

## 验证

- durable task、PPT、索引、父子块、关键词索引、会话 outbox 组合：`125 passed`。
- 索引范围专项：`19 passed`；全量后端：`1193 passed, 1 warning`。
- 前端生产构建通过；7 组前端测试全部通过，共 `49 passed`。
- `cargo check --locked` 通过。

## 仍未宣称的边界

- TaskJournal 仍是本机、至少一次的 step 执行；它没有把 Chroma、父文档 SQLite、关键词 SQLite 变成一个跨存储 exactly-once 事务。
- WPS 启动属于外部进程副作用；现有 marker 能避免大多数恢复重启重复打开，但无法在操作系统崩溃恰好落在 `Popen` 与 checkpoint 之间时证明外部进程 exactly-once。
- 本轮没有引入分布式 worker、跨主机租约、Agentic RAG 框架，也没有把全量回归当成真实 Tauri/WPS/UI/E2E 或安装包验收。
