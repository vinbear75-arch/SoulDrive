# 检索反馈数据库约束与历史重复诊断

日期：2026-09-15  
范围：`LearningStore.retrieval_feedback` 的数据库唯一性、旧库兼容和只读健康诊断

## 结论

本轮补齐了检索反馈幂等修复之后仍留下的两个边界：

1. 新工作区或没有 active 冲突的旧工作区，会建立按范围区分的 partial unique index：global 反馈按 `query_digest` 唯一，project 反馈按 `(project_id, query_digest)` 唯一；只约束 `status = 'active'`，不改变已经撤销的历史记录。
2. 如果旧库已经存在 active 重复，初始化不会因为创建唯一索引失败而阻断用户打开工作区；`retrieval_feedback_health()` 会以不含明文 query 的 opaque ID 和 digest 报出重复组，并明确标记数据库保护尚未建立。系统不自动选择、删除或合并用户确认过的改写。

这使当前链路形成“应用层事务幂等 + 数据库层防护 + 只读 doctor”的三层边界；历史数据修复仍需用户审阅后另行执行。

## 外部实践对照

- [SQLite partial indexes](https://www.sqlite.org/partialindex.html) 说明 partial unique index 可以只对满足条件的行强制唯一性，适合这里的 active 反馈集合。
- [SQLite CREATE INDEX](https://www.sqlite.org/lang_createindex.html) 明确说明 SQLite 的 UNIQUE 中多个 `NULL` 彼此不同；这解释了旧的 `UNIQUE(scope, project_id, query_digest)` 为什么不能单独保护 global 反馈。
- [LangMem semantic memory updates](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/) 将更新/删除作为显式操作，并允许应用决定软删除、硬删除或降权；本轮只增加诊断与约束，没有把历史反馈隐式合并。

## 失败优先与实现

- 既有约束保留以兼容历史 schema，但新增两个 active partial unique index：
  - `idx_retrieval_feedback_global_active_unique`
  - `idx_retrieval_feedback_project_active_unique`
- 初始化前先检查 active duplicate group。没有冲突时创建索引；有冲突时跳过创建并继续启动。
- `retrieval_feedback_health(project_id, include_global, max_issues)` 只读扫描当前可见范围，返回 active/revoked 计数、重复组数、索引保护状态、截断信息和 opaque 诊断项。
- `/learning/memories` 增加 `feedback_health`，使记忆管理界面可以显示反馈数据健康状态，但不把诊断内容注入问答上下文。

## 验收证据

- 人工构造旧库重复：删除新索引后插入第二个相同 global active row；doctor 报 `status=error`、`duplicate_active_groups=1`、两个 `feedback_id`，且列表仍保留两条，证明没有静默删改。
- 重新打开前的初始化路径不会因为重复数据自动清理或伪装成正常唯一约束；后续需人工处理重复后再重建数据库 guard。
- `tests/test_learning_store.py`：`91 passed`；学习/索引/项目/安全检索/RAG 交叉回归：`311 passed, 1 warning`。
- 学习 API 回归：`test_decide_accept_creates_searchable_memory` 通过，且 `/learning/memories` 暴露 `feedback_health.status=ok` 的正常路径。
- 完整工程门禁：后端 `1620 passed, 1 warning`；前端生产构建成功，UI 脚本 `62 passed`；`compileall` 与锁定 Cargo 通过。
- 冻结评测：memory `7/7` 场景、`8/8` 查询、recall `1.0`、陈旧/禁止泄漏 `0.0`；GraphRAG route `4/4`；episodic `5/5`；workflow roles `6/6`。
- 真实本地 GraphRAG acceptance：索引、社区目标、完成态、非拒答、引用绑定、hybrid/global/local 路由、项目索引等 acceptance flags 全部为 `true`。

## 边界

- 这不是历史数据自动迁移器，也不替用户决定保留哪条改写。
- partial index 只在 active 集合上约束；旧库中已存在 active duplicate 时必须先审阅，再由后续显式修复流程处理。
- 当前证据仍是本机 SQLite；未证明跨主机锁、真实 PostgreSQL/Redis、模型回答质量或真实 Tauri/WPS UI/E2E。
