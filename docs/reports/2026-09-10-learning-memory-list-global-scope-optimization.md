# 学习记忆列表全局范围优化记录

日期：2026-09-10  
范围：`GET /learning/memories` 的项目/全局列表语义

## 外部实现对照

- [Open WebUI memory router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py) 的列表和搜索均先按当前用户资源取数，不把裸资源 ID 当作授权；其记忆数据模型也保留类型、路径和时间元数据。
- [Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 要求检索显式携带范围过滤；项目本地对应为 `project_id`，缺失时不能默认放宽为所有项目。
- [Letta local memory prompt](https://github.com/letta-ai/letta-code/blob/main/src/agent/prompts/letta_local_memfs.md) 将记忆绑定到明确的 `agent_id`，说明跨会话/跨身份共享必须是显式关系，不能由“新会话”隐式发现全部记忆。

## 失败回归与根因

新增 `McpServerLearningTests.test_learning_memory_list_without_project_id_excludes_project_memories`，创建一条全局记忆和一条 `project-a` 记忆，再调用不带项目参数的列表接口。修复前响应同时包含两条记忆，证明列表入口仍会泄露项目范围资产。

根因是接口调用 `list_memories(project_id=None, context_only=False)`；存储层在该分支按所有活动记忆返回。接口响应还把无项目状态标成 `all_active`，与问答上下文已经使用的 `global_only` 语义不一致。

## 最小修复

- 无项目参数时调用 `list_memories(..., context_only=True)`，复用存储层已有的 `scope='global'` 约束。
- 有项目参数时保持原有项目加全局策略，不改变项目记忆管理、编辑、撤销和删除的显式项目门。
- 返回 `scope_policy` 的真实值；无项目时现在是 `global_only`，不再返回误导性的 `all_active`。

没有新增依赖，也没有改变加密事实源和历史版本保留策略。

## 验证

- 修复前列表回归失败：响应为 `['项目列表范围', '全局列表范围']`，期望仅全局记忆。
- 两条新增范围回归（列表、搜索）：`2 passed, 1 warning`。
- 学习模块全量：`59 passed, 1 warning`。
- 仓库全量 `scripts/check.ps1`：后端 `1073 passed, 1 warning`；Python compileall、前端生产构建、7 组 UI 测试共 `48 passed`，Tauri `cargo check --locked` 均通过。

## 边界

该修复只保证列表入口的项目范围语义，不等同于完成记忆语义召回、向量搜索或答案正确率评测；这些仍需在冻结数据集上单独验证。
