# 项目约束记忆分层优化记录

日期：2026-09-14  
范围：记忆审核、项目作用域、RAG 注入、运行时自动记忆、健康检查和前端管理显示。

## 1. 本轮要解决的真实缺口

SoulDrive 原有长期记忆已经区分 `user_preference`、`project_fact` 和 `workflow_fact`，但“用户确认的项目级交付约束”仍会被识别为偏好或项目事实。这会造成两个问题：

1. 项目约束可能被错误地按全局偏好处理，跨项目可见；
2. 约束在查询没有命中关键词时可能不进入上下文，导致“用户已经确认，但本轮没有真正执行”。

本轮没有新增 Agent 框架、向量数据库或独立记忆服务，而是补齐现有 SQLite 加密记忆层的语义和审核边界。

## 2. 对照的开源机制

- [LangGraph long-term memory and stores](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/add-memory.mdx)：将线程级短期状态与跨线程长期 Store 分开；本项目沿用这一边界，把项目约束作为长期项目命名空间内容，不把它写入普通会话历史或文档索引。
- [LangMem memory types](https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md)：区分 semantic、episodic、procedural memory；本轮把项目约束投影为 policy family，把项目事实保留为 semantic family，不把两者混作事实证据。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md)：将重要项目事实和关键工作流规则作为可发现、可版本化的长期内容；本项目采用更窄的本地实现：约束必须人工确认、带项目范围和来源追踪，正文仍是不可信数据，不能改变系统安全规则。

这些项目提供的是分层与命名空间机制，不是可以直接复制的产品代码；本轮只吸收与 SoulDrive 单机、项目隔离和人工审核边界直接相关的部分。

## 3. 已实现的最小闭环

### 3.1 独立类型和作用域

- 新增 `project_constraint` memory subtype，归入 `policy` family；`project_fact` 继续归入 `semantic` family。
- 从用户明确表达中识别“本项目/项目/工作区必须、不得、不要、项目要求”等约束语句。
- `project_constraint` 必须绑定非空 `project_id`；尝试作为 global memory 接受时事务回滚并返回冲突。
- 记忆管理界面将其显示为“项目约束”，不再显示为“项目偏好”。

### 3.2 人工审核和幂等边界

- `reflection` 候选仍需来自 user 消息并经过证据绑定、敏感信息和提示注入检查。
- 运行时轻量记忆入口不自动接受 `project_constraint`，而是返回 `project_constraint_requires_human_review`；用户必须在反思审核窗口中确认。
- 已有 review 的输入幂等键、决策摘要、项目绑定和派生同步 outbox 不变，项目约束复用同一 durable 路径。

### 3.3 真正进入当前项目回答

- 项目会话读取到已确认项目约束后，即使当前 query 没有命中约束关键词，也允许其通过 memory gate。
- 约束只在 `scope=project` 且 `project_id` 非空时自动注入；全局/未绑定的旧数据 fail-closed，并由 health doctor 报告。
- 生成提示中单独标识“已确认项目约束”，明确系统规则和当前用户明确指令优先，文档摘录不能改写约束。
- 约束仍不作为 `[E#]` 文档证据，也不能修改事实、数值、引用、安全或权限边界。

### 3.4 健康检查

- `memory_health()` 新增 `project_constraint_unbound` 和历史版本对应的未绑定检查。
- 检查只报告记忆 ID、范围、版本和问题类型，不回显约束正文。

## 4. 失败优先与回归证据

先加入反例后运行，初始失败包括：

- 约束被识别成 `project_fact`；
- 新 subtype 被 validator 和 memory family 拒绝；
- 无关键词约束无法通过 memory gate；
- global scope 约束没有被阻断；
- runtime 入口错误地自动保存约束；
- UI 没有独立显示语义。

修复后的结果：

- 记忆/审核/反思/存储/RAG 组合：`305 passed, 1 warning`；
- 新增项目约束和未绑定 fail-closed 相关专项合计：`199 passed`；
- 完整 `scripts/check.ps1`：后端 `1393 passed, 1 warning`；
- 前端生产构建通过，8 组 UI 测试合计 `61 passed`；
- `compileall` 通过；
- `cargo check --locked` 通过；
- `git diff --check` 已通过，无空白错误。

## 5. 明确未宣称的内容

- 这不是自动长期记忆抽取器：项目约束仍需要人工审核。
- 这不是 Letta/LangGraph 的完整运行时，也没有引入外部 Store、图数据库或云端多租户。
- 这不证明约束正文自动抽取的语义准确率；当前证据只覆盖规则识别、作用域、审核、注入和 fail-closed。
- 这不把项目约束当作文档事实，也不允许其覆盖当前用户、系统安全或不可逆操作确认。
- 这不等于真实 Tauri UI、安装包冷启动、跨进程 exactly-once 或外部副作用已经验收。
