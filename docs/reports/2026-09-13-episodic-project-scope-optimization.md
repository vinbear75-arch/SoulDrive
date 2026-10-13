# Episodic 项目范围 fail-closed 优化

## 对照实践

- [LangGraph Persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx) 将短期状态绑定到 `thread_id`，把跨线程长期记忆放在显式 store；范围键不是可选装饰字段。
- [LangGraph Checkpointer](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx) 的读取主键由 `thread_id` 与 namespace 组成，缺少定位键时不能恢复对应状态。
- [Graphiti project/group isolation](https://github.com/getzep/graphiti/pull/1209) 明确用 `group_id` 做逻辑隔离，并修复写入与查询使用不同 group 导致“数据存在但查不到”的问题。
- [Graphiti cross-group read issue](https://github.com/getzep/graphiti/issues/1795) 说明长生命周期客户端的组状态漂移会造成静默空结果；本地单机实现同样应避免把缺失范围猜成当前项目。

## 失败优先发现

- `build_episodic_view()` 对会话和 durable task 要求精确 `project_id`，但对实验运行和决策记录允许缺少该字段，并把它们投影为当前项目事件。
- 目录主查询通常已按项目过滤，因此常规项目总览测试没有暴露问题；但只读投影本身的契约是不安全的，任何未来复用、缓存或错误 SQL 投影都可能把无范围记录带入项目历史。
- 项目总览的实验/决策 SQL 也没有把 `project_id` 投影出来；直接收紧读取规则会把合法记录误删，说明来源投影与消费门没有对齐。

## 本轮修改

- Episodic view 对四类输入统一执行精确项目匹配；缺失项目范围和其他项目范围都拒绝进入事件列表，不再把空范围当作当前项目。
- 新增 `scope_rejected_count`，区分“被范围门拒绝”和“事件字段无效”，并纳入 `view_digest`，让范围丢弃可观察且可复现。
- 项目总览的实验运行、决策查询补齐 `project_id` 投影；合法项目记录继续进入历史视图。

## 验收证据

- 先加入无范围/跨项目实验与决策反例，旧实现出现 2 个失败；修复后 episodic view 与项目 API 定向回归 `39 passed, 1 warning`。
- 反例确认：无范围或跨项目历史不会出现在 `events`，`scope_rejected_count` 精确记录被拒绝的 4 条；项目总览中的合法实验仍可见。
- 最新完整 `scripts/check.ps1` 已通过：后端 `1336 passed, 1 warning`，前端构建及 8 组测试 `60 passed`，Tauri `cargo check --locked` 通过。

## 边界

- 这是读取投影的项目范围安全门，不是数据库外键补全、跨存储事务或历史脏数据迁移；源表仍由项目目录约束。
- `scope_rejected_count` 只报告本次传入集合被过滤的数量，不证明调用方没有遗漏记录，也不替代 SQL 层的项目条件。
- 这不构成记忆质量、RAG 召回率或答案准确率提升结论；这里只保证 episodic 历史不会凭空猜测项目归属。
