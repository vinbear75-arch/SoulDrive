# 记忆读取决策摘要指纹优化

日期：2026-09-13

## 发现

上一轮已经让 `/learning/search` 复用 `gate_memory_context()`，但读取门只返回 admitted/rejected 的结构化摘要。相同输入是否产生了相同的记忆上下文、以及一次重试是否实际改变了准入集合，仍没有一个轻量、可比较的决策标识。对诊断、重放和 durable execution 来说，仅记录“有几条记忆”不够；同时也不能把记忆正文或用户查询原文复制进审计日志。

## 公开实践对照

- [zmem](https://github.com/zerkerlabs/zmem) 将 memory search 与 memory injection 分开，并为 injected/withheld/budget-dropped 决策保留 action receipt；其上下文 digest 只承诺实际提供的上下文，不把它当成记忆事实真实性证明。
- [zmem 功能指南](https://github.com/zerkerlabs/zmem/blob/main/docs/ZMEM_FEATURE_GUIDE.md) 进一步把 `why`、withheld memory 和 receipt verification 作为可审计边界。SoulDrive 当前是单机 SQLite/本地服务，不需要复制 Merkle 日志，先采用相同的“决策可比较、正文不进摘要”最小原则。

## 实现

1. `gate_memory_context()` 继续返回原有 `memory_gate` 字段，并增加 `decision_digest`，格式为 `sha256:<hex>`。
2. 摘要指纹绑定：读取门 schema、查询规范化摘要、最终准入 memory ID、按稳定键排序后的拒绝原因/冲突关系、字符预算、item 上限、已用字符数和 memory capsule 的正文摘要。
3. 查询与胶囊正文只参与本地哈希，不出现在 gate 返回值、审计事件或接口响应中；拒绝项只保留 memory ID、原因和胜出 ID。
4. `/learning/search` 和聊天注入路径继续复用同一个读取门，因此 API 搜索回放与实际 RAG memory injection 看到同一种决策凭据。

## 失败优先与验收

- 先加入两条回归：旧实现没有 `decision_digest`，分别以 `KeyError` 失败；这确认测试不是只验证预先存在的字段。
- 实现后 `tests/test_memory_runtime.py` 为 `10 passed`：逆序输入产生相同指纹，准入集合变化产生不同指纹，指纹本身不包含查询或记忆正文。
- 读取门、MCP 学习 API、LearningStore、记忆角色专项组合为 `172 passed, 1 warning`。
- 完整 `scripts/check.ps1` 通过：后端 `1239 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。唯一已知警告仍是测试环境的 Starlette/httpx 弃用提示；前端构建的 chunk 大小提示不是失败。

## 边界

- 这是本地可重放的决策摘要，不是 Merkle receipt、签名证明、跨进程 exactly-once 或外部系统发送回执；哈希也不证明记忆内容为真。
- 指纹只承诺当前代码/schema、输入候选、预算和胶囊摘要对应的读取决定，不代表模型答案正确，也不替代 RAG 的来源覆盖、引用支撑和事实核验指标。
- 本轮没有新增统一 episodic 数据库。会话、实验台账和 TaskJournal 仍是事件事实源；下一步应先评估它们能否生成只读、带来源的 episodic 视图，再决定是否需要新的持久化投影。
