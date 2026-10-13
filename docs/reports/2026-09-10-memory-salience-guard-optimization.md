# SoulDrive 记忆写入门：瞬时上下文的 salience 降级

日期：2026-09-10

## 结论

本轮没有新增 episodic 数据库，也没有引入 LLM salience 服务；只在现有 `MemoryWriteGate` 增加一个窄规则：一次性“查看/浏览/打开/检查”类上下文即使带有项目关键词和高置信度，也只能进入 `hold`，不能走自动长期记忆路径。它仍可在审核列表中被人工接受，因此不会静默删除用户明确想保留的事实。

这对应“小项目最小可用”的分层：当前对话/会话历史承担瞬时事件，`memories` 只自动接收更可能跨会话复用的稳定偏好、项目事实和流程事实；没有把 `hold` 误称为通用 salience 模型。

## 外部实践对照

- [Letta archival memory 的官方函数说明](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py)要求长期归档写入的是自包含事实或摘要，而不是会话碎片；这与本地“短期 ConversationStore、长期 LearningStore”分层一致。
- [Graphiti salience RFC #1602](https://github.com/getzep/graphiti/issues/1602)指出长期图记忆若把问候、一次性事件和低信号上下文一律持久化，会造成图膨胀和召回噪声；其提议的三层策略是图记忆、episodic archive、discard。该 RFC 仍是公开提案，不被当作已合并能力。
- [Letta reflection 说明](https://github.com/letta-ai/letta-code/blob/main/src/agent/subagents/builtin/reflection.md)将 load-bearing 的长期内容与可按需检索的外部/历史内容分层，并强调记忆应放在合适 tier；本地采用同一原则，但不引入 MemFS 或后台 dreaming。

## 先失败后修复

新增 `tests/test_memory_gate.py::MemoryGateTests::test_transient_context_is_held_out_of_long_term_memory`，输入：`我正在查看这个项目的文件列表。`。

修复前该候选因为包含“项目”、用户来源、高置信度、证据和足够长度而得到 `review`；修复后断言：

- `decision == "hold"`；
- 原因包含 `transient_context`；
- 不是硬拒绝，人工仍可显式接受。

## 实现范围

- `core/memory_gate.py` 新增中英文窄匹配：当前正在查看/浏览/检查/打开/阅读、刚刚打开/查看，以及少量本次/今天上下文标记。
- 命中后将分数上限设为 `0.71`，低于自动进入人工审核的 `0.72` 阈值，并记录 `transient_context` 与既有 `insufficient_durable_signal`。
- 自动记忆旁路继续只接受 `write_gate.decision == "review"`；反思审核页仍可见并支持人工决定。

## 验证证据

- 失败优先反例先失败，修复后通过。
- 记忆门、候选验证、RAG prompt 定向回归：`100 passed`。
- 学习/MCP/会话/TaskJournal/RAG prompt/记忆组合：`281 passed, 1 warning`。
- 最新 `scripts/check.ps1`：后端 `1130 passed, 1 warning`；前端生产构建通过；前端 7 组测试共 `48 passed`；`cargo check --locked` 通过。

## 尚未声称完成的部分

- 规则只覆盖当前窄的中英文瞬时表达，不能识别所有低价值事件、隐含长期事实、跨语言表达或模型生成的 salience 分数。
- 没有实现独立 episodic archive、自动摘要/梦境整合或低价值历史清理；会话历史与项目资产目录仍是现有事实源。
- 真实业务集上“记忆噪声下降/召回质量提升”尚未测量；需要冻结样本并比较候选数量、人工接受率、记忆精确率和答案质量。
