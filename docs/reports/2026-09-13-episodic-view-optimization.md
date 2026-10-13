# 情景记忆只读投影优化

日期：2026-09-13

## 发现

SoulDrive 已分别持久化会话 manifest、实验运行、项目决策和 TaskJournal，但这些事实源没有统一的事件身份和时间语义。直接新增一套 episodic 数据库会制造第二份真相；不做投影又会让研究窗口无法查看“这条活动来自哪个来源、属于哪个项目/工作流、在什么时间发生”。

## 公开实践对照

- [Graphiti](https://github.com/getzep/graphiti) 用 `group_id` 做记忆范围，并区分事件实际发生的 `reference_time` 与系统摄取时间，同时保留 episode provenance。本地借鉴范围、来源和时间语义，但不引入图数据库或 LLM 抽取。
- [projectmem](https://github.com/riponcm/projectmem) 用追加事件作为事实源，再确定性投影出 AI 可读视图，并把判断层与事实写入分开。本地沿用“投影只读、事实源不复制”的最小实现。

## 实现

1. 新增 `core/episodic_view.py::build_episodic_view()`，将现有会话 manifest、实验运行、项目决策和 durable task 投影为有界事件列表。
2. 每个事件携带稳定 `event_id`、`source_type`、`source_id/source_ref`、严格 `project_id`、可选 `workflow_id`、`event_time`、`event_time_semantics`、标题、受限摘要和状态。
3. 支持 `as_of` 回放；无项目的 durable task、其他项目会话和显式错范围记录不会进入视图；无效事件时间计入 `invalid_event_count`，非法 `as_of` 直接拒绝。
4. 对排序后的可见事件和范围/时间元数据生成 `view_digest`；来源输入逆序时摘要不变，事件摘要或来源集合变化时摘要变化，便于诊断重试是否真正改变了投影。
5. `/projects/{project_id}/overview` 增加 `episodic_view`：读取现有会话列表与 TaskJournal，并复用 overview 已加载的实验/决策记录；不读取会话正文、不创建第二份事实存储、不自动把事件注入 RAG。

## 失败优先与验收

- 先运行新测试，旧代码在收集阶段因 `core.episodic_view` 不存在而以 `ModuleNotFoundError` 失败。
- 实现后情景视图专项 `5 passed`；项目 overview 与视图组合为 `26 passed, 1 warning`。
- 完整 `scripts/check.ps1` 通过：后端 `1244 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是 UI/诊断可用的只读活动视图，不是新的长期记忆类型，也不证明情景记忆召回率或模型答案准确率。
- `view_digest` 是本地摘要承诺，不是签名、Merkle proof 或事实真实性证明；它只绑定当前投影输出和输入元数据。
- 当前会话事件使用 manifest 的 `last_updated`，不是逐条消息的发生时间；尚未声称实现 Graphiti 的完整双时间模型。
- 当前视图不执行实体抽取、关系推理、GraphRAG 或 Agentic RAG；后续只有在冻结数据集证明统一事件视图能改善窗口检索/证据选择时，才考虑接入读取门。
