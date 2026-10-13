# GraphRAG 社区报告锚点质量门优化记录

日期：2026-09-13

## 结论

本轮补上社区报告的最小 grounding gate。此前 `GraphRAGBuilder` 只要收到非空模型文本就标记 `report_method=llm`；一个与社区完全无关的天气文本也会通过。现在生成报告必须至少与当前社区的标题、实体、关系或来源文件产生词项交集，否则回退既有 `structured_template`。该门只阻止明显无关摘要，不把社区报告升级为事实证据。

## 对齐的公开实践

- [Microsoft GraphRAG community report prompt](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/prompts/index/community_report.py) 要求只使用输入数据、没有支持数据的内容不要写，并要求报告内容围绕社区实体与关系展开。
- [GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/) 将社区报告与社区实体、关系、文本单元和来源记录分开保存，保留这些关联用于下游检索与追溯。
- [GraphRAG Local Search](https://microsoft.github.io/graphrag/query/local_search/) 把社区报告和原始文本分别作为候选，再进行 ranking/filtering；本项目沿用“报告是导航摘要、原始切片才是事实证据”的边界。

## 失败优先证据

新增 `tests/test_graph_rag.py::GraphRagTests::test_builder_rejects_unrelated_llm_community_report`，用 `"今天北京天气晴朗，气温舒适。"` 作为模型返回值。修复前该值被标为 `llm`；修复后所有社区均使用 `structured_template`。同时保留正向回归，带社区标题的模型报告仍然标记为 `llm`。

## 实现范围

- `core/graph_rag.py`：新增 `_community_report_has_grounding_signal()`，使用已有中文/英文词项切分器比较报告与社区锚点；只影响 `GraphRAGBuilder._report()` 的报告方法选择。
- 不新增模型调用、不改变社区划分、不修改 chunk provenance、不让报告直接进入事实引用；生成失败和 grounding 失败都继续走模板回退。

## 验收证据

- 失败样本修复、正向 GraphRAG 构建和结构化输出回归共 `4 passed`；此前 GraphExtractor/GraphRAG/Ollama 定向回归为 `126 passed`。
- 独立冷工作区的 4 个真实社区报告逐一通过锚点门；冷构建仍为 6/6 文档成功、4/4 社区报告走 `llm`，说明门没有误伤当前固定中文语料的真实报告。
- 该门是必要条件而非充分的事实核验：报告可能仍包含超出输入的同词改写或不准确因果，因此回答阶段继续只回读并引用原始 chunk。

## 边界

- 词项交集不能证明语义正确、事实支持或因果关系成立；它只拦截明显无关文本。
- 没有为报告再引入自动重试、外部评审模型或 Agentic RAG；小项目当前优先保持索引可恢复、可解释和成本有界。
- 代码变更后的完整 `scripts/check.ps1` 已通过：后端 `1342 passed, 1 warning`，前端构建及 8 组 UI 测试合计 `61 passed`，`cargo check --locked` 与 `compileall` 通过。警告仍为既有 Starlette/httpx 弃用提示与 Vite bundle 体积提示；这不等于真实 Tauri/WPS UI、安装包冷启动或外部 exactly-once 已验收。
