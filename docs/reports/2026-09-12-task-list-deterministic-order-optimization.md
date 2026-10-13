# TaskJournal 任务列表确定性排序优化

日期：2026-09-12

## 结论

`TaskJournal.list_tasks()` 现在按 `updated_at DESC, task_id ASC` 返回任务。同一更新时间的任务不再依赖 SQLite 插入/返回顺序，任务恢复、任务面板和重启后的任务发现顺序可复现。

## 外部实践对照

- [Temporal Visibility schema](https://github.com/temporalio/temporal/blob/main/schema/sqlite/v3/visibility/schema.sql) 的执行索引在时间字段后保留 `run_id`，把稳定执行身份作为末级排序键。
- [Temporal List Filter 文档](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/visibility/list-filter.mdx) 明确列出默认时间排序，并说明不同可见性存储可能需要额外运行 ID 才能区分同时间结果。
- [Prefect FlowRun 模型](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/server/database/orm_models.py) 将幂等键和 flow run 唯一身份持久化在数据库，而不是把一次列表位置当作运行身份。

这些项目只提供“时间排序后保留稳定运行身份”的设计参考；SoulDrive 没有因此引入 Temporal、Prefect 或新的任务服务。

## 失败优先验证

新增 `tests/test_task_journal.py::TaskJournalTests::test_list_tasks_uses_stable_task_id_tie_breaker`：逆序创建两个同类型任务，再把二者的 `updated_at` 固定为 `100.0`。

- 修复前返回创建顺序，证明同时间列表依赖 SQLite 返回顺序。
- 修复后按字典序稳定返回 `task_id`；任务状态、租约、步骤恢复和幂等键语义没有改变。

## 回归证据

- TaskJournal/PPT 定向回归：`37 passed`。
- 全量 `scripts/check.ps1`：后端 `1160 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明任务列表在并列更新时间下的确定性顺序，不证明任务执行的跨进程 exactly-once、外部副作用幂等、分页游标或真实 Tauri UI 交付；`task_id` 仍只是稳定身份，不是分布式排序时钟。
