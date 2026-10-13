# 索引派生投影一致性优化

日期：2026-09-13

## 对照实践

- [Open WebUI Knowledge Base Sync](https://github.com/open-webui/docs/blob/main/docs/ecosystem/knowledge-base-sync/index.md) 用源文件 manifest、路径和 checksum 做增量 diff，并区分同步完成与服务端异步处理完成。
- [Chroma Collection.upsert](https://github.com/chroma-core/chroma/blob/main/chromadb/api/models/Collection.py) 是按记录 ID 的 upsert 接口；它不替本地 parent SQLite、关键词 FTS 和 GraphRAG 派生存储提供跨存储事务。
- [Dapr Transactional Outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 的持久化 outbox 才能把事实提交与后续派生投影连接起来；本机项目当前没有把 Chroma/SQLite/图谱伪装成一个事务。

## 失败优先发现

- `DriveIndexer._document_index_status()` 原先主要根据向量子块的 `document_hash`、元数据版本和模型名判断 current。
- `LocalKnowledgeBase.ingest_chunks()` 虽然在同步异常时尝试回滚，但进程崩溃、底层向量库异常退出或回滚删除失败，都可能留下“向量存在、parent 缺失”或“关键词投影缺失”的半完成状态。
- 下一轮索引若只看到向量记录，会跳过解析和重建，导致父文档扩展或稀疏召回长期缺失；GraphRAG 有独立回填检查，但 parent/keyword 还没有同等级门。

## 本轮修改

- `KeywordIndex.contains_ids()` 只读检查指定 child ID 是否全部存在。
- `LocalKnowledgeBase.check_index_consistency()` 检查当前 hash/source 的向量 child、parent 记录和关键词投影，返回 `current` 或明确的 `parent_store_incomplete`、`keyword_index_incomplete`、`*_unavailable` 重建原因。
- `DriveIndexer._document_index_status()` 在旧的 hash、元数据、stale cleanup 检查之后调用该检查；发现派生层不完整就重新进入正常索引流程，而不是静默跳过。
- 检查不在读路径自动写修复，不删除疑似正确记录，不改变项目过滤；它只是把“不确定完成”转成下一轮可观察、可重建的状态。

## 验收证据

- 索引一致性失败优先回归覆盖：向量存在但 parent 缺失、索引器重建状态和真实关键词 ID 检查。
- 摄取/索引/关键词/安全向量/索引回执专项 `71 passed, 1 warning`；补充关键词投影专项组合 `52 passed`。
- 最新仓库级 `scripts/check.ps1` 已通过后端 `1335 passed, 1 warning`、前端 `60 passed` 和 Tauri `cargo check --locked`。

## 边界

- 这不是 Chroma、SQLite、关键词库、GraphRAG 的跨存储原子提交，也不是外部 exactly-once；崩溃窗口仍可能需要下一轮索引才能修复。
- 只检查 parent/keyword 派生层；GraphRAG 的实体、关系、社区报告继续由现有 graph coverage/backfill 路径负责。
- 这不是召回率或答案准确率提升结论；需要在同一冻结语料和索引版本上单独评测 dense/sparse/graph 各路质量。
- 这不证明真实 Windows 崩溃恢复、Tauri 冷启动或多进程并发压力下的索引行为。
