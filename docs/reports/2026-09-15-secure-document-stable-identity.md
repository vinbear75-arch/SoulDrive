# 加密文档稳定索引身份优化

日期：2026-09-15  
范围：加密文档临时解密、索引状态判断、派生索引清理和本地一致性检查

## 发现的真实缺口

`DriveIndexer` 会把加密文档解密到每次索引都新建的运行目录。旧实现没有保留安全对象身份，`_stable_document_id()` 因而会把随机临时路径哈希成文档 ID。这样同一个受管文档跨两次索引的身份会漂移；当多个加密文档使用相同展示名时，按 `source_filename` 清理还可能把不属于当前对象的旧片段纳入候选。

## 外部实践对照

- [Haystack Document 实现](https://github.com/deepset-ai/haystack/blob/main/haystack/dataclasses/document.py) 使用规范化内容/元数据生成确定性文档 ID，并将 ID 用于文档存储中的去重和持久化匹配。
- [Haystack 元数据过滤](https://docs.haystack.deepset.ai/docs/metadata-filtering) 将元数据作为检索和路由前的范围约束；本轮将同一原则用于索引状态与清理，而不是把展示名当成唯一身份。
- 这只借鉴“稳定身份 + 明确范围”机制，没有引入新的文档框架、数据库或迁移工具。

## 失败优先验证

新增 `SecureIndexerDocumentsTests.test_secure_document_identity_does_not_depend_on_runtime_temp_path`。修复前调用新身份合同会失败（旧方法不接受安全对象身份，且只能按路径计算）；修复后两个不同运行目录对同一安全对象产生相同 ID，不同安全对象产生不同 ID。

## 实现

- `core/indexer.py`：在安全临时文件映射中保留不暴露明文的安全对象 ID；`_stable_document_id()` 优先使用该身份；临时目录清理同步清除映射。
- `core/indexer.py`：安全文档的索引状态、派生投影一致性和旧片段清理按 `document_id` 过滤；旧的普通文件路径/展示名路径保持兼容。
- `core/knowledge_base.py`：一致性检查支持可选 `document_id`，避免同名安全文档共享一致性判断。

## 验证证据

- 索引、知识库和父子存储定向回归：`65 passed`。
- 完整 `scripts/check.ps1`：后端 `1631 passed, 1 warning`；前端生产构建通过，8 组 UI 脚本合计 `62 passed`；Python compileall 与锁定 Cargo 检查通过。Vite 保留既有单 bundle 体积警告（约 `585.53 kB`）。
- 冻结记忆质量：`7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`，`stale_or_forbidden_leakage_rate=0.0`。原始结果：[memory_quality_after_secure_document_identity_20260915.json](../../eval/results/memory_quality_after_secure_document_identity_20260915.json)。
- 冻结 GraphRAG 路由：`4/4`，路由/图门/信号合同准确率均为 `1.0`。原始结果：[graphrag_route_after_secure_document_identity_20260915.json](../../eval/results/graphrag_route_after_secure_document_identity_20260915.json)。
- episodic workflow routing：`5/5`；研究窗口角色：`6/6`，角色、就绪、检索画像、输出合同、引用命名空间、RAG 门和来源多样性指标均为 `1.0`。episodic 结果来自本轮命令标准输出；角色原始结果：[workflow_roles_after_secure_document_identity_20260915.json](../../eval/results/workflow_roles_after_secure_document_identity_20260915.json)。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_secure_document_identity_20260915.json](../../eval/results/graphrag_acceptance_after_secure_document_identity_20260915.json) 中 `index_ready`、`candidate_all_completed`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界与下一步

- 该身份对同一个受管安全对象稳定；安全对象内容发生变化时，安全存储会形成新的内容对象，本轮不宣称它自动拥有跨版本的资产 ID。项目目录绑定的版本身份仍由 `asset_id`/`asset_version_id` 负责。
- 本轮修复的是索引内部身份和派生清理，不修改文档列表、预览、删除等仍以展示名为入口的 UI/API；同名加密文档的对象级选择和操作仍应继续改为显式 ID，并补真实 UI 回归。
- 不证明跨主机 exactly-once、断电时跨存储原子性、真实 Redis/PostgreSQL、模型答案事实准确率、真实 Tauri/WPS UI/E2E 或大规模数据下的性能。
- 真实 GraphRAG 全局查询仍可能使用受控多来源回退；验收项为真不等于模型已经稳定产出高质量跨文档综合答案。
