# RAG 派生索引 lineage 优化

日期：2026-09-13

## 发现

上一轮评测运行清单已经记录数据集指纹、Git 状态、运行环境、模型名和检索参数，但没有记录实际被读取的本机派生索引。对于 SoulDrive 的混合 RAG/GraphRAG，代码和标注相同并不代表 `keyword_index.sqlite`、父文档库、图索引或安全向量库相同；缺少这一层会让“可复核运行”仍停留在半条链路。

## 公开实践对照

- [DVC 的 `.dvc` 文件](https://github.com/treeverse/dvc.org/blob/main/content/docs/user-guide/project-structure/dvc-files.md) 用内容校验和、大小和路径作为大文件/目录的可版本化指针；本地借鉴“稳定路径 + 内容哈希”，不引入 DVC 依赖或远端存储。
- [MLflow tracking API](https://github.com/mlflow/mlflow/blob/master/docs/docs/classic-ml/tracking/tracking-api/index.mdx) 将数据输入、模型和 artifact 作为运行 lineage，而不是只记录运行脚本；本地把可观察的派生索引作为 evaluation run 的 component metadata。
- [Qdrant snapshots](https://github.com/qdrant/qdrant/blob/master/src/common/snapshots.rs) 强调索引/collection 恢复需要显式快照和校验；本地只做运行时文件 fingerprint，不声称它等价于数据库原子 snapshot 或恢复能力。
- [LlamaIndex StorageContext persist](https://docs.llamaindex.ai/en/v0.9.48/module_guides/storing/customization.html) 将 document/vector/index stores 视为需要一起持久化的组成部分；本地沿用已有 SQLite 路径，不扩大到用户文档和模型目录的递归扫描。

## 实现

1. `scripts/eval_rag.py` 的 `save_run_manifest()` 新增 `workspace_path`，运行时把索引 lineage 作为 `components.workspace_index_snapshot` 写入既有 `souldrive_run_manifest_v0.1`。
2. 固定允许列表包括关键词索引、父文档库、普通/安全图索引、安全向量库和文档 manifest；每个现存文件记录相对路径、字节数和 SHA-256，并对排序后的列表再生成 `artifact_fingerprint`。
3. 没有工作区、工作区不存在或没有允许列表文件时分别标记 `unbound`、`missing_workspace` 或 `empty`，不伪造“已绑定索引”；`--from-results` 也会尝试从显式/运行时工作区读取这层信息。
4. 不递归哈希用户源文档、模型目录或整个 workspace，避免运行清单膨胀、泄露路径和无界延迟。

## 失败优先与验收

- 新增回归第一次运行真实失败：`save_run_manifest()` 尚不接受 `workspace_path`，pytest 抛出 `TypeError`。
- 实现后评测资产专项 `53 passed`；清单回归确认捕获两个模拟索引文件并生成 `sha256:` fingerprint。
- 完整 `scripts/check.ps1` 已通过：后端 `1224 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是 lineage fingerprint，不是 SQLite/向量库的并发安全快照；评测期间若索引仍被写入，哈希只能代表读取时观察到的文件内容。
- 只捕获已知文件路径，不证明 embedding 权重、外部 Ollama/云模型、GPU 状态或未列入允许列表的索引组件完全一致；这些限制会继续留在 manifest 中。
- fingerprint 不能替代项目/文档范围校验，也不能把跨项目泄漏风险变成“已解决”；它只是帮助复核运行时到底读取了哪一组本机派生文件。
