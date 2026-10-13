# 2026-09-14 项目记忆最终读取范围闸门优化

日期：2026-09-14  
范围：`gate_memory_context()` 的 `project_id`、`include_global` 和项目/全局记忆边界。

## 1. 本轮发现的真实缺口

LearningStore 查询已经按项目范围过滤，但最终 memory gate 只有候选列表，没有当前项目和 global-only 策略参数。旁路调用可以把项目 A 的候选直接交给项目 B 的 gate；也可以在 project-only 读取中把 global 候选交给 gate。原有项目优先冲突测试还暴露出：项目优先这个判断本身必须绑定明确项目，不能在无项目上下文里比较两个 namespace。

## 2. 对照的开源机制

- [Mem0 memory search filters](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 把实体/会话和 metadata 过滤作为搜索契约，而不是把过滤留给生成模型。
- [LangGraph stores](https://docs.langchain.com/oss/python/langgraph/stores) 用 namespace 区分长期记忆空间；本地将 `project_id` 作为既有 SQLite namespace 的最终读取绑定，不复制新存储。
- [Graphiti group/episode scope](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 通过 group/episode 作用域保留事实归属；本地继续采用项目和 workflow 的显式范围，而不是依赖文件名或问题文本推断。

本轮只借鉴显式过滤和最终读取边界，不引入远程记忆服务、Neo4j 或新的 namespace 依赖。

## 3. 最小实现

`gate_memory_context()` 新增 `project_id` 与 `include_global`：

1. project memory 没有当前项目时拒绝 `project_memory_scope_required`；
2. project memory 的 `project_id` 与当前项目不一致时拒绝 `project_memory_scope_mismatch`；
3. `include_global=False` 时拒绝 global memory，原因 `global_memory_scope_disabled`；
4. 非法 scope fail-closed；
5. 项目 ID与 global 策略进入既有 decision digest；
6. 聊天运行时和 `/learning/search` 从服务端 scope policy 统一透传，UI 不承担范围安全职责。

安全/敏感内容检查仍优先于 namespace 检查，保持已有 `untrusted_instruction`/`sensitive_content` 诊断稳定。

## 4. 失败优先与验证

旧实现对跨项目反例直接不支持 `project_id`，对无项目 project memory 则会接受；新增 global-only 反例同样要求显式策略。实现后：

- `tests/test_memory_runtime.py`：`19 passed`；
- `tests/test_memory_gate.py tests/test_memory_runtime.py`：`35 passed`；
- `tests/test_memory_quality_eval.py`：`3 passed`；冻结评测重新运行 `7` 个场景、`8/8` 查询通过，项目隔离和 project constraint gate reason 保持通过。
- 截至本轮最新完整工程门：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。

中途全量复核发现两个旧的“项目优先冲突”测试未提供项目上下文，导致它们在新 fail-closed 契约下失败；已将测试绑定到 `project-a`，没有放宽生产范围门。

## 5. 明确边界

- 这证明最终 prompt gate 的项目/global 范围防线，不证明上游检索器、向量投影或 GraphRAG 的召回质量。
- `project_id` 是范围绑定，不是用户身份认证；本机单用户授权边界仍由现有工作区/解锁和项目 API 负责。
- 不自动修复旧的错误范围数据；doctor、历史版本和派生索引继续按既有诊断/重建流程处理。
