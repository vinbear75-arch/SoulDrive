# SoulDrive Memory Doctor 健康审计优化记录

日期：2026-09-10

## 优化目标

记忆系统已经具备项目范围、时间有效性、版本历史、加密存储和派生 outbox，但在记忆管理页只能看到“当前有哪些记录”，不能回答“活动事实是否重复、是否仍有可检索块、派生同步是否待恢复”。本轮补充只读健康审计，先让问题可见，再决定是否需要人工编辑、停用或重建。

## 外部实践对照

- [Letta Memory & dreaming](https://github.com/letta-ai/letta-docs-md/blob/main/configuration/memory/index.md)把 `/doctor` 定位为检查记忆放置、重复和提示词规模漂移的审计工具；更大的重组在备份后进行，而不是隐式覆盖。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)明确区分线程级短期 checkpointer 与跨线程长期 Store，并将持久化状态用于检查、恢复和故障容错；这支持把 SoulDrive 的活动记忆、历史版本和派生检索块分别核验。
- [Mem0 Add Memory](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx)说明不同写入模式可能让同一事实重复保存，也强调 scope/identifier 和过期状态会影响后续检索。

本地取舍：不引入 Mem0/Letta/LangGraph 依赖，不自动做语义合并，不在打开列表时重放 outbox；只在现有 SQLite 事实源上做范围受控的诊断。

## 实现内容

`LearningStore.memory_health()` 返回不含记忆正文的诊断摘要：

- 同 `subtype + scope + project_id + 标题 + 内容` 的活动事实重复；比较前在本地解密，输出只保留 memory ID、数量和范围。
- 活动 memory 没有活动 `learning_chunks`；活动块找不到活动事实，或块与事实的 scope/project 不一致。
- 活动历史版本、同一记忆历史版本的重叠/非法区间、待审核 review、待同步 outbox、重试次数和退避中的同步数量。
- 加密载荷不可读也作为错误项报告；问题列表有上限并返回 `truncated`，避免健康检查本身无限膨胀。
- `status` 只表达诊断状态：`ok`、有待同步但无结构错误时为 `degraded`，出现重复/缺块/孤立块/解密异常时为 `error`。

范围规则与既有记忆管理一致：无 `project_id` 只检查 global；显式项目默认检查该项目加 global；调用方可以选择 project-only。健康检查不修改表、不消费 outbox、不返回加密正文。

## UI/API 落地

- `GET /learning/memories` 继续返回原有 `memories`，额外返回 `health`，因此旧客户端仍可读取原字段。
- 偏好画像页显示活动事实/检索块数量和健康状态；发现问题时提示人工检查，明确不会自动合并或删除记忆。
- API 与 UI 均沿用项目范围门，项目 A 的健康计数不会把项目 B 的事实混入。

## 验收证据

- 新增 LearningStore 回归：重复活动事实与缺失检索块、项目范围隔离、待同步状态与只读/不泄露正文、历史版本孤儿与非法时间区间、历史版本重叠区间，共 `5 passed`。
- 记忆管理 API 回归验证 `health` 字段、项目 A/B 计数隔离和已有编辑/停用/删除流程；当前记忆组合定向回归 `132 passed, 1 warning`。
- 前端 `test:learning` 为 `3 passed`，生产构建通过。
- 全量 `scripts/check.ps1`：后端 `1110 passed, 1 warning`；Python compileall、前端构建及 7 组测试 `48 passed`、Tauri `cargo check --locked` 均通过。

## 边界与下一步

- 健康审计不是自动修复器；重复事实仍需人工决定保留、编辑或停用，避免把同标题不同时间版本误删。
- 当前重复判定是同范围、同标题、同内容的精确归一化，不宣称语义重复检测，也不替代答案质量评测。
- 历史版本审计进一步检查孤儿版本、重叠/非法 `valid_from/invalid_at` 区间和版本与当前事实的范围错配；它只报告损坏，不删除或重写历史。新增回归覆盖孤儿版本、反向时间区间和重叠区间。
- 活动块缺失被识别，但尚未自动重建 embedding；重建需要单独定义失败恢复、版本和评测门。
- 仍未把 Letta 的后台 dreaming 或自由 Agent 循环引入本机小项目；若未来增加自动整合，必须先冻结记忆写入评测集、重复率、时效性和误合并率。
