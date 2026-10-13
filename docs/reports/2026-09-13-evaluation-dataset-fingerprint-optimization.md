# 评测数据版本指纹与结果可复现性优化

日期：2026-09-13

## 发现

窗口 `workflow_id` 已经进入评测结果，但结果文件仍可能只凭样本 ID、问题和来源字段被读取。若后续修改同一条样本的 rubric、关键词、拒答标记、跨文档要求、图谱标记或文档身份，旧结果就可能被新标签重新评分，造成“结果没变、评价标准变了”的隐性漂移。

## 公开实践对照

- [OpenAI Evals 的 eval 命名约定](https://github.com/openai/evals/blob/main/docs/build-eval.md) 用 `<eval_name>.<split>.<version>` 区分可比较的评测版本；本地没有引入注册中心，而是对当前 JSONL 的评分字段计算稳定指纹。
- [Phoenix 数据集概念](https://arize.com/docs/phoenix/learn/datasets-and-experiments/datasets-concepts) 将数据集更新版本化，使实验可以固定到具体版本。
- [Phoenix 稳定 example ID 更新](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-datasets/updating-datasets) 使用稳定样本 ID 对样本变更做差异更新；本地保留样本 ID，同时把完整评分标签纳入指纹，避免只校验 ID 的假一致。

## 实现

1. `evaluation_dataset_fingerprint()` 对排序后的样本 ID、问题、来源、类型、rubric、关键词、拒答/多文档/图谱标记、项目、窗口和评测文档身份做规范化 JSON + SHA-256，包含 schema version 前缀。
2. 现场检索/答案生成结果保存该指纹；读取时如果结果只部分带指纹、包含多个指纹或与当前数据集不一致，直接拒绝评分。
3. 没有指纹的历史结果仍可兼容读取，但报告显式显示“旧结果未绑定”，不会伪装成已验证版本；新结果报告显示 verified 指纹。
4. 修复序列化边界：`retrieval_observability: null` 是合法的空观测值，不应被结果读取器误判成非法对象。

## 失败优先与验收

- 新增 mismatch 回归第一次运行确实未抛错，证明旧读取器会静默接受错误数据集标签。
- 实现后评测资产测试 `52 passed`；评测/窗口/RAG 提示组合 `172 passed, 1 warning`。
- 中途 round-trip 回归发现 `null` 观测字段兼容错误并修复；之后完整 `scripts/check.ps1` 通过：后端 `1221 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 指纹证明的是评测标签和 schema 版本一致，不证明索引、模型、embedding、提示词、硬件或外部数据源一致；这些仍需在评测运行 manifest 中单独记录。
- 历史无指纹结果没有被伪造迁移；它们只能以兼容的 legacy_unbound 状态读取。需要严格发布门时，应拒绝 legacy 结果并重新运行。
- 这不是业务答案质量提升，也不是 Phoenix/Langfuse 服务替代品；本轮只收紧本机 JSONL 评测的版本绑定和失败边界。
