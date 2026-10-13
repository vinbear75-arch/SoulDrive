# Durable Task 工作流身份向情景路由透传优化

日期：2026-09-14  
范围：`TaskJournal.public_task()`、项目活动投影、研究窗口隔离。

## 本轮发现的真实缺口

任务创建输入已经保存 `workflow_id`，但安全的公开任务投影只返回项目、计划和产物身份，
没有返回窗口身份。于是 `episodic_view` 看到 durable task 时会把它当成未绑定任务；同一个
会议行动任务可能被 `experiment_compare` 的时间查询选中，破坏了“不同会话窗口有不同作用”的
边界。

## 公开实践对照

- [Temporal Workflow ID/Run ID](https://docs.temporal.io/workflow-execution/workflowid-runid)
  将业务工作流身份与执行实例身份分开；本地继续用 TaskJournal 的 `task_id` 作为执行身份，
  只把已存在输入中的 `workflow_id` 作为业务路由元数据透传。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  要求恢复和读取使用同一 `thread_id` 上下文；本地对应为任务的项目和研究窗口身份必须随
  公开投影一起进入选择器，不能在解密后静默丢失。

这些机制只证明身份应贯穿持久化和读取边界，不证明 SoulDrive 已实现 Temporal/LangGraph 的
完整运行时。

## 最小实现

`TaskJournal.public_task()` 将 `workflow_id` 作为受控的输入元数据字段返回；不返回其他加密输入
或任务正文。既有 `episodic_view` 已按 `event.workflow_id` 做不相等拒绝，因此无需增加新的
路由规则：会议 durable task 可进入 `meeting_actions`，在实验/论文窗口中 fail-closed。

## 失败优先与验证

- 旧实现的新单元用例因公开任务没有 `workflow_id` 而失败。
- 旧实现的跨模块用例实际让会议任务进入 `experiment_compare`，证明不是字段形式问题。
- 修复后：TaskJournal `25 passed`；学习窗口隔离定向 `2 passed, 1 warning`；情景/目录回归
  `35 passed`；完整活动评测继续为运行时 `5/5`。

## 边界

- 仅透传已存在且受控的窗口字符串，不自动从任务标题猜测窗口；没有窗口身份的 durable task
  仍按当前允许来源规则处理，不能被伪造为某个窗口的事实。
- 这只修复活动定位的窗口路由，不把 durable task 状态或结果直接当作文档引用，也不证明任务
  外部副作用 exactly-once、模型答案准确率或真实 Tauri/WPS UI/E2E。
