# 记忆质量评测闭环优化

日期：2026-09-13

## 发现

SoulDrive 已经具备记忆的项目/工作流范围、版本历史、时间有效性、冲突淘汰、幂等写入和 durable projection，但原有评测主要验证结构与接口回归，没有一个小而可复现的“记住—检索—更新/回溯—检查泄漏”质量门。这样容易证明数据库没有报错，却不能证明当前记忆真的被找回、旧事实不会覆盖新事实。

## 公开实践对照

- [Mem0 memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) 将记忆评测拆成 Ingest → Search → Evaluate，并按记忆召回、时间推理、多会话和冲突解决等能力分组；本地采用相同的阶段思想，但保留离线、无外部模型的确定性断言。
- [LangMem 长期记忆概念](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) 区分 semantic、episodic、procedural memory，并明确记忆应经过提取、合并和更新；本地场景用 `user_preference`、`project_fact`、`workflow_fact` 对应业务作用，而不是把所有内容混成一个召回池。
- [Graphiti](https://github.com/getzep/graphiti) 保留事实的时间有效区间、来源 episode 和历史关系；本地不引入新的图数据库，只用现有 `memory_versions` 与 `valid_from/invalid_at` 验证同一条事实的当前/历史视图。
- [Letta memory architecture](https://github.com/letta-ai/skills/blob/main/letta/agent-development/references/memory-architecture.md) 将始终在上下文中的 core memory 与按需搜索的 archival memory 分开；本地对应地把 `context_only`、项目/工作流门和按需 `search()` 作为可测试边界。

## 实现

1. 新增 `scripts/eval_memory_quality.py`，使用真实 `LearningStore` 临时工作区执行冻结事件序列，不调用 LLM judge，也不依赖网络服务。
2. 新增 `eval/memory_quality_v0.1.jsonl`，包含 6 个场景、7 个查询：基础召回、项目隔离、研究窗口隔离、更新后当前事实、按时间回溯历史事实、显式冲突淘汰。
3. 每个查询输出 `memory_recall_at_k`、必需/禁止别名命中、期望/禁止内容命中和 `stale_or_forbidden_leakage`；报告只保存别名和指标，不把运行时诊断扩展成长期记忆正文导出。
4. 评测夹具把人类可读别名解析为实际 memory ID 后再调用冲突接口，避免测试数据格式掩盖生产接口的 ID 合同。

## 失败优先与验收

- 首次新增测试在实现前真实失败：`eval_memory_quality` 尚不存在，pytest 收集阶段 `ImportError`。
- 首版评测实现加入时间/冲突场景后，7 个查询中只有 `6 passed`；根因是夹具把 `old_notification_preference` 别名直接传入 `conflicts_with`，未转换为真实 memory ID。
- 修正别名映射后，专项回归 `2 passed`；命令行冻结数据集评测结果为：6 个场景、7 个查询、`query_pass_rate=1.0`、`memory_recall_at_k=1.0`、`stale_or_forbidden_leakage_rate=0.0`。
- 完整 `scripts/check.ps1` 已通过：后端 `1224 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是一组小型确定性契约测试，不是 LoCoMo、LongMemEval 或真实用户长期记忆准确率；没有外部 LLM judge，也没有宣称覆盖所有多跳、多会话和大规模容量问题。
- 评测当前验证检索与状态转换，不自动测量 embedding 质量、语义同义改写、用户主观满意度或真实工作区的索引规模。
- 评测使用临时工作区，不能替代真实迁移、崩溃恢复、PostgreSQL/Redis 适配和 Tauri UI 端到端测试；这些仍由既有专项和完整工程门分别负责。
- 下一步若扩展数据集，必须冻结新版本并保留逐场景结果；不能用 sealed-test 错误反推提示词或记忆策略。
