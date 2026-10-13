# 本机 SQLite 创建回执原子幂等优化

日期：2026-09-13

## 发现

TaskJournal、项目目录和产物目录都已经有唯一幂等键与冲突回读逻辑，但部分入口采用“先查询、再插入、捕获唯一约束异常”的实现。它在常规重试下可以收敛，却把正确性依赖在异常路径上；跨进程 SQLite 竞争时，创建是否成功、是否回放、是否应该继续写步骤/审计，不应由一次竞态异常来决定。

本轮没有把已有并发回归伪装成失败：新增回归在旧实现上也能通过，说明原有兜底能够救回该场景。本轮修复的是实现契约，让唯一索引直接成为并发序列化点，并保留输入摘要冲突检查。

## 公开实践对照

- [SQLite UPSERT](https://www.sqlite.org/lang_upsert.html) 将唯一约束冲突交给数据库的原子写入语义处理，而不是把预查询当作并发保护。
- [issuance-gateway 的 SQLite 原子幂等设计](https://github.com/lemmebee/issuance-gateway/blob/master/docs/ARCHITECTURE.md) 使用 `INSERT ... ON CONFLICT DO NOTHING` 后重新读取已有记录，并区分同键重放与输入冲突。
- [sqlite-durable-workflow](https://github.com/eatdrop/sqlite-durable-workflow) 将 SQLite 唯一键、租约和至少一次执行放在同一个单节点 durable 边界内；它也明确不把本地回执宣传为外部 exactly-once。

## 实现

1. `TaskJournal.create_task()` 对带幂等键的任务使用原子忽略插入；冲突后按 `(kind, idempotency_key)` 回读并比较输入摘要，只有同输入才返回已有任务。
2. `ProjectCatalog.create_project()` 对项目幂等键使用同一模式，保留显式 `project_id` 重用等无关约束的严格异常。
3. `ProjectCatalog.create_artifact()` 对产物幂等键使用同一模式，按现有产物输入摘要拒绝同键异内容，不重复写产物步骤或后续交付状态。
4. 没有幂等键的旧调用仍使用严格插入；本轮没有把所有普通资源创建都强行改成幂等。

## 失败优先与验收

- `tests/test_task_journal.py`：跨两个独立 SQLite 连接并发创建同一任务键，两个调用都成功回读同一 `task_id`，数据库只有一条任务记录；TaskJournal 全量 `21 passed`。
- `tests/test_project_catalog.py`：跨两个独立目录连接并发创建同一项目键和同一产物键，均只保留一条记录；目录全量 `24 passed`。
- 受影响的项目、文档、邮件和 PPT 组合回归：`106 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1376 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。

## 边界

- 这是本机单节点 SQLite 的创建回执原子性，不是跨机器分布式锁，也不是外部文件、SMTP、模型调用或其他副作用的 exactly-once。
- `INSERT OR IGNORE` 只用于带幂等键的入口；若冲突无法由幂等键解释，会重新执行严格插入以保留原始约束错误。
- 并发回归使用短时双连接场景，不等于长时间多进程压力测试、断电恢复演练或真实 Tauri 网络断线测试。
