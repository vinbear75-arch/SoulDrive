# GraphRAG 源快照新鲜度与陈旧索引拒答优化

日期：2026-09-10

## 结论

本轮修复了 GraphRAG 派生索引缺少源版本绑定的问题。

此前社区索引只保存 `built_at`、社区数量和内容。如果图抽取、文档绑定或关系更新后，新的 GraphRAG 构建失败，旧索引仍可能被诊断为 ready 并参与检索；这会把过期社区摘要送入后续证据链。

现在的行为是：

- 构建 GraphRAG 时对原始图快照生成确定性 SHA-256 指纹，并写入 `source_snapshot_digest`；索引 schema 标记为 `souldrive_graphrag_index_v3`；
- 检索/诊断加载新格式索引时重新计算当前图快照指纹；不一致则拒绝使用，检索返回 `graphrag_index_stale`，诊断返回 `status=stale`；
- 图快照读取失败时返回 `graphrag_source_snapshot_unavailable`，不把无法核验的新鲜度当成 ready；
- 旧版本没有指纹的索引暂时保持兼容，但诊断明确标记 `source_freshness=unverified`，需要后续重建；
- 项目索引缺失或陈旧时仍可按既有策略尝试安全的 `global_shared` 回退，但不会绕过全局索引自身的新鲜度检查；
- 没有引入图数据库、队列或后台 worker，也没有把社区摘要提升为原文事实源。

## 外部实践对照

- [Graphiti graph ingestion](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py) 保留 episode 的时间语义，并在事实解析时处理重复与 supersession；这说明图派生数据必须绑定可解释的来源/时间，而不是只有一次生成时间。
- [Graphiti edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 将新事实、重复边和失效边区分处理；SoulDrive 不直接引入其 LLM 去重链，而是先补更小、更确定的源快照 fencing。
- Graphiti 的[事实失效问题记录](https://github.com/getzep/graphiti/issues/1841)与[未限定关系候选范围的问题记录](https://github.com/getzep/graphiti/issues/1728)也说明时间/范围错误会让图索引看似可用却给出错误事实；本地实现选择陈旧时 fail-closed，并继续保留项目/全局边界。
- [Qdrant Data Synchronization](https://qdrant.tech/documentation/data-synchronization/)把向量/检索索引视为可重建派生系统，强调事件、版本、批量重建和 reconciliation；GraphRAG 本轮采用同一原则的最小单机版本：索引与快照指纹不一致就等待重建，不静默沿用旧派生结果。

## 代码变化

### 1. 构建时保存源快照指纹

`GraphRAGBuilder.build` 在 scope 截取前读取原始图快照，并对稳定 JSON 生成 `source_snapshot_digest`。指纹覆盖当前实体、关系、文档、chunk 引用和项目字段，能够发现图抽取或文档绑定的变化。

这里使用原始快照指纹而不是仅比较社区数量：社区数量不变、关系事实变化同样必须触发重建。

### 2. 读取时做 freshness gate

`GraphRAGIndex` 新增 checked load：

- 新格式索引与当前图快照不一致时不返回社区证据；
- source snapshot 无法读取时不伪装成 ready；
- 兼容旧索引，但通过 `source_freshness=unverified` 把其限制公开给诊断；
- global/shared fallback 也经过同一个 checked load，不允许回退分支绕过新鲜度门。

### 3. 对后续证据链的影响

陈旧 GraphRAG 只会减少一条派生导航路径，不会删除原始文档、向量或图事实。普通关键词/向量检索仍可运行；GraphRAG 调用方看到空结果和 stale reason 后，应等待 indexer 完成重建，而不是把旧社区摘要交给模型。

## 失败优先与验证

- 新增 `test_graph_index_refuses_stale_payload_after_source_snapshot_changes`：先构建并确认可检索，再修改源图文档摘要；旧实现仍返回社区，修复后检索返回 `graphrag_index_stale`，诊断返回 `stale`。
- GraphRAG、图存储、加密图存储和项目诊断组合：`53 passed, 1 warning`。
- `scripts/check.ps1`：本次 GraphRAG 改动后的快照为后端 `1117 passed, 1 warning`；随后持久游标改动的最新全量为后端 `1118 passed, 1 warning`。两次均通过 Python compileall、前端生产构建、前端 7 组测试 `48 passed` 和 Tauri `cargo check --locked`。
- `git diff --check` 需在本轮文档更新完成后再次执行；当前没有发现代码空白错误。

## 未宣称的能力与后续边界

- 指纹门只能识别“图快照已变”，不会自动调度或修复失败的 GraphRAG 构建；索引重建仍由现有 indexer 生命周期负责。
- 旧索引暂时只标记为 unverified，未强制删除；后续可在工作区迁移/重建窗口中决定是否 fail-closed，需结合真实历史索引兼容性验证。
- 当前指纹是原始图快照级别的保守校验；项目索引的 chunk 项目映射变化若不反映到图快照字段，仍需后续补充映射版本水印。
- 没有冻结业务评测集，因此不能声称 GraphRAG 答案准确率提升；本轮证明的是陈旧派生索引不会继续作为已验证证据使用。
