# RAG 旧后端 query variants 可观测性

## 外部机制与本地缺口

[Haystack Pipelines](https://docs.haystack.deepset.ai/docs/pipelines)把检索链路建模为显式组件连接的有向多重图，支持条件分支与循环，并在运行前校验组件连接。SoulDrive 不引入 Haystack 运行时；本轮只借鉴“计划中的分支必须能说明实际是否执行”的边界。

`RAGEngine._search_with_optional_project_focus()` 为兼容旧知识库后端，会在后端签名不接受 `extra_query_variants` 时移除该参数，继续执行基础 query。这个回退语义是有意保留的，但此前返回结果没有标记 variants 是否真正进入后端；`rag.retrieval_planned` 中的计划数量可能被误读为实际应用数量。当前默认本地后端支持 variants，因此不是默认链路的检索语义回归，而是旧兼容旁路的审计缺口。

## 最小修复

- 在检索结果的 `observability` 中记录 `extra_query_variants_requested`、`extra_query_variants_applied`。
- 旧后端丢弃参数时记录 `extra_query_variants_dropped`；调用方超过本地上限时记录 `extra_query_variants_truncated`。
- 实际应用数按“限额后的参数数 − 兼容丢弃数”计算，不把调用方原始数量冒充为后端实际收到的数量。
- 不改变旧后端的基础 query 回退，也不引入 Agentic RAG、Haystack 或新的检索依赖；scope contract 仍保持 fail-closed。

## 验收证据

- 旧后端反例：`1 passed`；上限计数反例：`1 passed`。
- `tests/test_rag_prompting.py`：`127 passed`。
- 第四十二轮独立完整 `scripts/check.ps1`：退出码 `0`；后端 **1774 passed、1 warning，440.05 秒**；记忆质量 **7/7 场景、8/8 查询、Recall@K=1.0、陈旧/禁止泄漏率=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；episodic runtime **5/5**；前端生产构建成功、UI **66 passed**；`compileall` 与锁定 Cargo 检查通过。

## 仍未证明

- 该改动只证明旧后端兼容分支的执行状态可观察，不证明 Recall@K、MRR/nDCG、证据覆盖率、答案准确率或延迟收益。
- `extra_query_variants_applied=0` 时系统仍会执行基础 query；这不是自动触发多轮 Agentic RAG，也不代表回答必然失败。
- 结果观测仍是本机调用边界；不证明真实外部检索后端、跨进程/跨主机 exactly-once、真实 UI/E2E 或开放式业务数据质量。

## 2026-10-13 边界反例与计数修正

原有两条回归分别覆盖旧签名和支持 variants 的限额分支，但没有覆盖“旧签名且超过限额”的组合。继续扩展反例发现：兼容循环先删除 `extra_query_variants`，随后才计算 bounded 数量，导致丢弃被再次算作截断。即便只请求 1 条（没有超限），也错误得到 `truncated=1`。

- B0：在相同 `souldrive` 环境下新增边界断言，`1 failed, 1 passed, 125 deselected`；失败为预期截断 0、实际 1。
- Candidate：把 bounded 数量快照移到兼容删除之前；旧签名请求 1 条时 `requested=1/applied=0/dropped=1`，没有 truncated；请求 4 条时 `requested=4/applied=0/dropped=3/truncated=1`。不改变限额或基础 query 回退。
- 扩展既有测试而非改评测数据；与 compaction 反例的组合 `3 passed, 278 deselected, 1 warning`，后续完整工程复核也通过。
- `applied` 的准确含义是**被透传到后端调用边界的 variants 数量**，不是后端去重后真正执行的查询数；后端内部执行仍需独立轨迹。上文“实际应用”不得扩大解释。

本次先后完整检查后端为 `1778 passed`、`1779 passed`（后者包含新增产物冒烟回归）。两份 transcript 未完整捕获原生命令输出，因此只作启动/结束记录；随后完整输出归档复跑退出 0，后端 `1779 passed, 1 warning`、459.61 秒，前端 66 项及其他工程门通过。最终 1086 行原始日志与指纹见 `2026-10-13-artifact-smoke-confirmed-workflow.md`。历史第四十二轮成绩保留，不将它追溯解释为已覆盖这个新组合反例。
