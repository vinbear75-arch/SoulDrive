# 程序性记忆与研究窗口作用域优化

日期：2026-09-14

## 结论

本轮把现有 Skill 作为程序性记忆的作用域边界补完整：未声明窗口的 Skill 继续作为工作区共享流程；声明了 `workflow_ids` 的 Skill 只会在对应的研究窗口中被检索、匹配和生成执行计划；没有当前窗口时，作用域 Skill fail-closed，不会因为文本相似而进入普通对话。

这解决的是“不同会话窗口是否真正使用不同程序性记忆”的可复现边界问题，不是引入一个自由循环 Agent，也不是声称 Skill 已经具备自动学习或语义正确性。

## 公开开源实践对照

- [Mem0 memory types](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-types.mdx) 将 procedural memory 定义为工作流步骤知识，并要求通过 `agent_id` 绑定作用域；本地采用同一原则，但把已存在的 `workflow_id` 作为受控窗口身份，不新增云端 agent 账户。
- [Mem0 memory benchmarks](https://github.com/mem0ai/memory-benchmarks) 把记忆链路拆成 Ingest → Search → Evaluate；本轮沿用 SoulDrive 的本地冻结评测，把“错误窗口拒绝”加入搜索/执行计划评测，而不是只看最终文本关键词。
- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 与 [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将 Skill 视为外部程序性记忆，并通过 agent 上下文发现；本地保留加密 LearningStore 和声明式 Skill 适配契约，不把远程脚本或自由文本直接塞进提示词。

## 可复现缺口

旧链路只筛选 `target=conversation` 和 `compatibility_status=verified`，随后用查询词匹配所有 Skill。一个“会议行动项” Skill 因此可能在 `paper_review` 窗口命中；LearningStore 的 context-only Skill 搜索也没有窗口过滤。

失败优先回归先真实暴露了三类问题：matcher 不接受 `workflow_id`、上下文搜索返回错误窗口 Skill、MCP 学习上下文会把会议 Skill 注入论文窗口。修复前的 targeted run 为 `3 failed, 9 passed, 1 warning`。

## 实现

1. `core/skill_runtime.py` 新增受控 `workflow_ids` 规范化和 `is_skill_visible_in_workflow()`：空作用域表示共享；非空作用域必须匹配当前窗口；没有当前窗口时不放行作用域 Skill。
2. `core/project_skills.py` 支持在 `SKILL.md` front matter 中声明 `workflow_ids: meeting_actions, paper_review`，并把作用域写入本地声明式适配器；缓存适配器在作用域变化时重新编译。
3. `LearningStore.search(..., context_only=True)` 在解密载荷后再次执行 Skill 窗口门；`core/mcp_server.py` 的聊天自动匹配、手动匹配接口和 Skill 计划接口均携带 `workflow_id`。跨窗口手动生成执行计划会被拒绝；共享 Skill 的既有行为保持不变。
4. `scripts/eval_learning_loop.py` 支持按评测记录的 `workflow_id` 运行程序性记忆匹配和计划生成；`eval/learning_loop_v0.1.jsonl` 新增同时包含会议 Skill 与论文 Skill 的竞争样本。

## 验收证据

- 作用域 Skill、项目 Skill 解析、LearningStore 和 MCP 组合：`200 passed, 1 warning`。
- 学习评测辅助回归：`19 passed`；冻结学习评测 `4/4`、`pass_rate=1.0`。新增样本在 `paper_review` 查询中只命中 `Paper review`，`Meeting actions` 被拒绝。
- 完整 `scripts/check.ps1`：后端 `1400 passed, 1 warning`；前端生产构建通过，8 组 UI 测试 `61 passed`；`compileall` 通过；`cargo check --locked` 通过。
- 前端既有 bundle 大小提示（约 `584.62 kB`）和 Starlette/httpx 弃用提示仍存在，均不是本轮失败。

## 边界与后续

- 当前 Skill 存储仍属于本机工作区级共享资产；本轮增加的是工作流窗口作用域，不是项目级 Skill 目录隔离。若未来要求同一工作区内不同项目拥有不同 Skill 集合，需要再设计 `project_id` 目录/存储迁移和删除边界。
- 作用域是显式声明的，不自动从 Skill 文本猜测窗口；未声明的 Skill 保持共享，避免把已有本地流程静默隐藏。
- 本轮不宣称 procedural memory 自动抽取、Skill 语义准确率、模型答案准确率、图记忆、DRIFT、多跳 Agentic RAG、跨存储 exactly-once 或真实 Tauri/UI E2E 已完成。
