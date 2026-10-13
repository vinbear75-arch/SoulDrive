# GraphRAG 本地模型结构化输出与上下文预算优化记录

日期：2026-09-13

## 结论

本轮处理了 GraphRAG 索引构建中已经真实出现的两类降级：实体关系抽取的本地模型输出偶尔不是可解析 JSON，以及社区报告请求曾出现 `Requested tokens (11976) exceed context window of 8192`。修复保持小范围：支持 `response_format` 的模型使用实体/关系 JSON Schema；社区报告只发送必要字段，并按 `graph_n_ctx - 输出预算 - 保留量` 形成有界、仍为合法 JSON 的输入；失败时继续使用已有结构化模板，不把社区摘要伪装成原始事实证据。

这不是“模型质量已证明提升”的结论。它证明的是本地索引阶段的输入/输出契约更清晰、超上下文请求有预算门，并且降级状态不会阻塞主索引。

## 真实失败证据

前一轮在 `eval/runtime/rag_cn_dev_unencrypted_v0.1` 的 6 份中文资料上首次构建 GraphRAG 时，日志出现多次模型输出无法解析为 JSON，以及一次社区报告请求超过本地 8192 上下文窗口。构建仍通过结构化模板降级形成了可用图索引，但这意味着 LLM 报告覆盖率和抽取覆盖率不能被默认视为完整。

## 对齐的公开实践

- [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs) 支持把 JSON Schema 传入 `format`，并建议将 Schema 同时写入提示词、降低温度并对响应做解析验证。本项目只在后端模型明确声明 `response_format` 时传递该约束，保留 llama.cpp 或测试替身的兼容路径。
- [Microsoft GraphRAG 详细配置](https://microsoft.github.io/graphrag/config/yaml/) 将社区报告的 `max_length` 与 `max_input_length` 分开管理；[Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 也明确把候选结构化数据和原始文本放入预定义上下文窗口，并在候选集合上排序过滤。本项目采用同一预算思想，但没有引入完整 GraphRAG 框架或额外分布式索引。
- [GraphRAG Global Search 示例](https://microsoft.github.io/graphrag/examples_notebooks/global_search/) 对 8K 模型建议降低上下文与输出预算。本项目的社区报告是索引期摘要，不是回答阶段的 map-reduce，因此只在索引调用点做预算收缩。

## 实现范围

- `core/graph_extractor.py`
  - 增加实体/关系抽取 JSON Schema；只有 `create_chat_completion` 明确暴露 `response_format` 参数时才发送，避免改变其他本地后端接口。
  - 社区报告输入只保留社区 ID、标题、实体、来源、关系和受限文档摘要，不携带大体积 `chunk_ids`；输入始终选择不超过预算的合法 JSON 表示。
  - 输出预算与 `graph_n_ctx` 联动，并保留原有空输出/异常时的 `structured_template` 回退。
- `tests/test_graph_extractor.py`
  - 先验证支持结构化输出的模型确实收到 Schema，而不是只验证提示词文本。
  - 用超长实体、关系、来源和摘要反例验证社区输入仍为合法 JSON 且不超过 4096 上下文下的预算。

## 验收证据

### 失败优先与定向回归

新增 GraphExtractor 回归后，`tests/test_graph_extractor.py tests/test_graph_rag.py tests/test_rag_prompting.py` 共 `126 passed`。这覆盖 Schema 传递、社区输入预算、既有 GraphRAG 构建/新鲜度/范围、Ollama 请求格式和回答提示词，不等于完整项目门。

### 隔离真实 Ollama 烟测

使用当前本机 `qwen3.5:9b` Ollama 服务和临时 SQLite 图存储执行同一抽取/摘要路径：

- 实体抽取：`3` 个实体、`2` 条关系成功落盘到临时存储；
- 社区报告：生成 `313` 字；
- 临时目录在测试结束后清理，未修改项目评测工作区；
- 这证明真实 HTTP 模型路径能够接受 Schema 并完成一次端到端调用，但只有一个短样本，不能外推到 6 份资料的全量覆盖率。

### 全量冷构建

在新的 `eval/runtime/rag_cn_cold_candidate_20260913` 工作区中只复制 6 份评测 PDF，不复用既有向量库、关键词库或图数据库，使用本轮代码完成冷构建。结果为：6/6 文件成功、`failures=0`；GraphRAG 构建 `completed`，6 个文档、193 个实体、128 条关系、4 个社区，目标范围 `[3,4]` 通过；4 个社区报告的 `report_method` 均为 `llm`，最终 `global_shared` 索引就绪。原始图快照为 196 个实体、199 条关系，GraphRAG 受限构建的实体/关系数量差异来自已有噪声过滤和图索引范围规则，不能把二者直接当作质量提升指标。

### GraphRAG 运行时验收

新冷构建工作区的验收结果保存于 `eval/results/graphrag_acceptance_cold_candidate_20260913.json`：索引 `ready=true`、社区数 `4` 且处于目标 `[3,4]`、scope freshness `verified`；普通问题为 `hybrid` 且不启用图，全局问题触发 `global` 图路由，关系问题触发 `local` 图路由；3 条候选回答均完成、无拒答且带引用。验收脚本还修复了两个测量问题：使用运行时 `RAGEngine.graph_rag` 的范围验证提供器，而不是让裸 `GraphRAGIndex` 把已验证索引误报为 `unverified`；把“是否主动请求项目范围”保留为信息项，不把无项目的合法全局验收误判为失败。

## 边界与后续

- 原评测工作区的旧索引仍是在本轮代码修复前构建的；本轮已在独立冷工作区完成 6 份资料的全量冷构建，且 6/6 无索引失败、4/4 社区报告走 `llm`。这证明本轮输入预算和 Schema 路径在该固定小语料上可运行，但没有旧/新同条件逐片段失败率日志，不能把“无索引失败”解释成模型抽取质量提升，也不能外推到更大语料。
- 旧构建出现过 JSON 解析失败与上下文超限；当前系统的结构化模板回退仍是合法降级，不代表所有实体关系都被抽取，也不代表 LLM 社区报告一定成功。
- 社区报告只用于导航；回答阶段仍需按报告中的 `chunk_ids` 回读原始切片，事实回答必须引用原始证据。该边界没有放宽。
- 本轮没有引入 Agentic RAG、自动重试抽取、后台队列或跨进程 exactly-once；若后续要重试，必须先定义按文档/切片的幂等键、尝试记录和失败上限，避免索引阶段重复写图。
- 本轮代码变更后的完整 `scripts/check.ps1` 已通过：后端 `1341 passed, 1 warning`，前端构建通过且 8 组 UI 测试合计 `61 passed`，`cargo check --locked` 和 `compileall` 通过。警告为既有 Starlette/httpx 弃用提示与 Vite bundle 体积提示；这仍不等于真实 Tauri/WPS UI、安装包冷启动或外部服务 exactly-once 已验收。
