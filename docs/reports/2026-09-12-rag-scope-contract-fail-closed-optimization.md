# 普通 RAG 范围契约 fail-closed 优化

日期：2026-09-12

## 结论

本轮发现 GraphRAG 范围旁路修复后，普通 dense/keyword RAG 入口仍有同型兼容风险：`RAGEngine._search_with_optional_project_focus()` 对不接收 `project_id` 的旧版 `search_with_evidence()` 会静默省略项目参数；无项目普通问题也没有把 `global_only` 作为显式后端契约传递。旧后端因此可能继续返回混合项目候选。

现在：

- 项目请求必须由检索后端声明 `project_id` 能力；缺失时返回空结果并记录 `scope_contract=unavailable`；
- 无项目且非显式跨项目问题显式传递 `global_only=True`；后端不支持该参数时同样 fail-closed；
- `LocalKnowledgeBase.search_with_evidence()` 支持显式 `global_only`，同时保留原有“无项目 + 非显式全局意图自动 global-only”的默认安全行为；
- 显式跨项目/全局问题仍可走授权的全局路径，项目请求和全局请求不会因同一兼容后端而静默降级到混合范围。

## 外部实践对照

- [Qdrant Filtering](https://qdrant.tech/documentation/search/filtering/)要求把业务条件作为检索/读取过滤条件；[Qdrant 多分区实践](https://qdrant.tech/documentation/tutorials/multiple-partitions/)用租户字段过滤隔离共享集合。
- [Weaviate Multi-tenancy operations](https://weaviate.io/developers/weaviate/manage-data/multi-tenancy)要求每次查询带租户；[Weaviate search basics](https://docs.weaviate.io/weaviate/search/basics)也明确多租户查询必须指定 tenant。

映射到 SoulDrive：`project_id`/`global_only` 是检索边界的一部分，不是调用方可有可无的优化参数；边界能力不明时应拒绝，而不是继续猜测。

## 失败优先证据

新增 `test_retrieval_backend_without_scope_contract_fails_closed`，用只接受旧参数的后端分别模拟项目查询和无项目 global-only 查询。旧实现会返回 `project-b` 的混合证据；修复后两条路径都返回空证据，并将 `retrieval_mode` 标为 `scope_contract_unavailable`、观测标为 `scope_contract=unavailable`。

## 最小实现

- `core/rag_engine.py`
  - `_search_with_optional_project_focus()` 增加 `global_only` 参数；
  - 项目或 global-only 请求发现后端签名无法证明范围时返回结构化空结果；不可检查签名的后端也不接受带范围请求；
  - `rag.retrieval_planned` 与 `agentic_retrieval_trace_v1` 记录 `retrieval_global_only`。
- `core/knowledge_base.py`
  - `LocalKnowledgeBase.search_with_evidence()` 增加显式 `global_only`；
  - 仍禁止 `global_only` 与 `project_id` 同时使用，默认安全推导不被显式 `False` 关闭。
- `tests/test_rag_prompting.py`
  - 增加旧版无范围后端的项目/global-only 双负向回归；
  - 验证有项目和无项目请求的审计范围字段。

## 验证

- RAG/GraphRAG/安全范围组合：`143 passed`；
- 全量 `scripts/check.ps1`：后端 `1196 passed, 1 warning`；
- 前端生产构建通过，7 组测试合计 `49 passed`；
- Tauri `cargo check --locked` 通过；
- `git diff --check` 无差异格式错误；Git 的 LF/CRLF 提示来自工作区已有行尾配置，不是测试失败。

## 边界

本轮证明的是本地已知兼容旁路不会因为缺少范围参数而继续返回结果；不证明第三方后端内部真实执行其声明的过滤，不证明召回率、答案准确率、GraphRAG 社区质量、Agentic RAG 收益或企业级多租户 RBAC。普通检索、GraphRAG 和记忆范围仍需在冻结业务集上做质量评测；真实 UI/E2E、安装包和跨存储 exactly-once 仍未由 `scripts/check.ps1` 证明。
