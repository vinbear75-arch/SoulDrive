# 范围契约与记忆时间语义优化

## 结论

本轮继续沿用“公开实践 → 失败回归 → 最小修复 → 聚焦测试”的路径，收紧了两个仍可能被忽略的边界：

1. 检索诊断和离线评测的无项目查询现在显式使用 `global_only=True`；不支持该范围契约的旧后端直接失败，不再把所有项目当作默认语料。
2. 记忆时间解析新增“昨天”和“前天”的本地日终点时语义；同时出现多个相对时间或明确日期与相对时间时保持未解析，避免用当前事实替代不确定的历史事实。

## 外部实践与取舍

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 将事实的 `valid_at`/`invalid_at` 和检索时间过滤作为显式契约，并保留来源可追溯性；本轮只借鉴时间边界显式化，没有引入图数据库。
- [Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 要求搜索携带身份过滤，防止不同用户记忆混用；本轮把同一原则落实为项目范围参数显式传递。
- [MCP progress specification](https://modelcontextprotocol.io/specification/2024-11-05/basic/utilities/progress) 与 [MCP server tool errors](https://ts.sdk.modelcontextprotocol.io/server) 强调长任务状态和失败语义应由协议显式表达；本轮沿用“缺少能力就返回可诊断失败”的做法，没有把旧后端静默降级为越权路径。

## 代码变更

- `core/mcp_server.py::_run_retrieval_test`：无项目诊断强制传递 `global_only=True`；旧签名后端无法接收范围参数时由诊断接口返回 503。
- `scripts/eval_rag.py`：离线检索评测及答案生成失败回退显式传递项目/全局范围。
- `scripts/eval_graphrag_acceptance.py`：GraphRAG B0 基线显式传递无项目全局范围。
- `core/temporal_memory.py`：支持“昨天/前天”日终点时解析；多时间表达式 fail-closed。
- `tests/test_mcp_server_indexing.py`、`tests/test_eval_assets.py`、`tests/test_temporal_memory.py`：增加旧后端范围契约失败回归、评测范围传播和时间歧义回归。

## 已验证

- `tests/test_mcp_server_indexing.py tests/test_rag_prompting.py tests/test_graph_rag.py`：`133 passed`。
- `tests/test_eval_assets.py tests/test_temporal_memory.py tests/test_mcp_server_indexing.py`：`63 passed`。
- `tests/test_temporal_memory.py tests/test_mcp_server_indexing.py`：`18 passed`。
- 全量 `scripts/check.ps1` 已重新执行：后端 `1199 passed, 1 warning`；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。构建仅保留既有的单个 JavaScript chunk 体积提示。

## 边界与未声称事项

- “昨天/前天”只解析为当前本地时区对应日期的结束时刻；“今天、本月、过去几天”等可能需要区间或截止点的表达式仍不自动猜测。
- 这只证明入口范围参数和时间门控不会静默放宽；不等于已证明真实 GPU、Tauri UI、WPS、SMTP 或完整用户界面 E2E。
- `global_only` 不是跨项目浏览授权；若业务确需跨项目比较，仍必须由调用方显式绑定项目集合/比较上下文并经过现有证据门。
