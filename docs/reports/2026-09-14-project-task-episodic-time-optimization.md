# 项目任务时间语义与情景活动投影优化

日期：2026-09-14  
范围：`project_tasks` schema、项目概览、情景活动路由和会议窗口检索上下文。

## 本轮发现的真实缺口

情景活动投影已经接入会话、实验运行、项目决策和 durable task，但此前没有接入项目任务。
原因不是遗漏一条 SQL，而是 `project_tasks` 只有 `due_at`，没有创建、更新或完成时间；如果把
截止时间当作活动发生时间，系统会把“未来要做什么”误报成“过去发生了什么”，破坏“回顾上次
会议行动项”的时间语义。

## 公开项目对照

- [Temporal Visibility schema](https://github.com/temporalio/temporal/blob/main/schema/sqlite/v3/visibility/schema.sql)
  将 `start_time`、`execution_time`、`close_time` 和稳定运行 ID 分开保存，并用时间字段与 ID
  形成可复核的排序键。本地只借鉴“事件时间不能由截止时间替代”的原则。
- [Dalvelac/Tasker](https://github.com/Dalvelac/Tasker) 的任务模型同时保存
  `created_at`、`updated_at` 和 `due_date`，把任务生命周期和计划时间区分开。本地沿用字段分层，
  没有引入新的任务服务。
- [daruma architecture](https://github.com/tupical/daruma/blob/main/docs/ARCHITECTURE.md)
  将任务状态与 `occurred_at` 活动记录分开；本地仍使用已有项目任务表和只读活动投影，不新增事件总线。

这些项目只提供机制参考，不证明 SoulDrive 的业务答案准确率；本轮只把已有任务记录变成可审计的
活动定位输入。

## 最小实现

1. `project_tasks` 增加可空的 `created_at`、`updated_at`、`started_at`、`completed_at` 字段和
   `(project_id, updated_at, task_id)` 索引，schema 版本从 11 升到 12。
2. 旧任务迁移保持时间字段为 `NULL`。没有可靠 `updated_at` 的旧行不会进入情景活动视图，
   `invalid_event_count` 会记录被拒绝的行；`due_at` 只保留为截止时间，不能作为活动时间。
3. 项目概览和聊天活动投影读取任务时间字段，并把 `project_task` 只开放给
   `meeting_actions`。没有 workflow 绑定的普通项目任务不会进入 `experiment_compare` 或
   `paper_review`，避免窗口职责混淆。
4. 活动选择结果保留 `created_at`、`updated_at`、`due_at`、`completed_at`，但仍只是定位提示，
   不直接成为回答引用证据。

## 失败优先与验证

- 新增项目任务投影测试后，旧实现因 `build_episodic_view()` 不接受 `project_tasks`、旧 schema
  没有时间列而失败；修复查询时又复现了一个真实边界错误：项目概览未选择 `project_id`，任务
  被正确地按未绑定范围拒绝。补齐查询后通过。
- `tests/test_episodic_view.py tests/test_project_catalog.py`：`35 passed`。
- 项目概览与聊天情景路由：分别 `3 passed`、`1 passed`（各含既有 Starlette/httpx 弃用警告）。
- 冻结活动路由集扩展为 5 个场景：无活动 baseline `2/5`，运行时投影 `5/5`，`pass_rate=1.0`。

## 边界

- 旧任务没有可靠时间时不会被自动补时间，也不会用 `due_at` 猜测发生时间；需要由真实任务写入
  或迁移流程提供时间后才可进入活动定位。
- 任务事件只帮助会议窗口定位行动项，不能替代会议原文、确认记录或文档证据；最终回答仍须
  通过既有引用和事实支持门。
- 本轮没有引入完整事件溯源、Graphiti 双时间图、Agentic RAG、DRIFT、多跳规划或跨存储
  exactly-once。
