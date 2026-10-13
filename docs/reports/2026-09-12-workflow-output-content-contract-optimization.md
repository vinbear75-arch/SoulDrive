# 研究窗口输出契约内容校验优化

日期：2026-09-12

## 结论

三类研究窗口的回答契约现在不仅检查必需标题是否出现，还检查每个标题下面是否有可见内容；`待确认` 作为明确的缺证据标记仍然算作有效内容。标题齐全但正文为空的回答会进入一次有界重试，不能直接被记录为完整结果。

## 外部实践对照

- [Haystack JsonSchemaValidator](https://github.com/deepset-ai/haystack/blob/main/haystack/components/validators/json_schema.py) 将输出字段是否符合 schema 与事实内容判断分开，并把校验错误送回受控恢复循环。
- [LlamaIndex StructuredLLM](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/llms/structured_llm.py) 要求结构化 LLM 返回指定输出类实例，否则明确失败，而不是把非结构化空壳当成成功。
- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 将 response type 与候选上下文构建作为独立契约；SoulDrive 采用同样的“输出形状先校验、事实支持另行校验”边界。

本地实现仍保持 Markdown 兼容、最多一次回答重试和既有引用/事实证据门，没有引入 JSON 输出依赖或新框架。

## 失败优先验证

新增两层回归：

- `tests/test_multimodal_workflows.py::test_workflow_output_contract_rejects_empty_required_sections`：论文回答只有“研究问题”有内容，其余两个必需标题为空；修复前状态为 `pass`，修复后报告 `empty_sections` 和 `missing_sections`。
- `tests/test_rag_prompting.py::RagPromptingTests::test_playbook_empty_section_triggers_retry`：真实回答流第一轮带空章节，第二轮补齐；确认模型调用次数为 `2` 且最终保留补齐内容。

相关工作流契约与 RAG 提示词用例共 `5 passed, 1 warning`。

## 实现范围

- `core/multimodal_workflows.py::evaluate_workflow_output_contract` 解析必需标题后的 inline/多行内容，空章节进入 `missing_sections`，但不判断事实真假。
- `core/rag_engine.py` 原有回答重试、引用命名空间和事实支持门继续负责其他错误；本轮没有扩大重试次数。

## 全量验证

本轮代码变更后的 `scripts/check.ps1`：后端 `1159 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。专项结果不能替代这些全量证据。

## 边界

这只证明输出契约不再接受空章节，不证明标题下的内容事实正确、引用支持充分、跨窗口质量差异成立或答案准确率提升；事实仍必须通过证据引用和冻结评测集验证。
