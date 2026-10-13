# 2026-09-14 RAG 子问题证据覆盖优化

日期：2026-09-14  
范围：比较型检索的子问题覆盖门、纠偏重试选择和 Agentic Retrieval trace。

## 1. 本轮发现的真实缺口

原链路已经能识别“比较甲方案与乙方案的安全审计做法”并得到两个来源，但最终只按 `source_count >= 2` 判断是否可以继续生成。两个来源可能分别返回甲方案的审计证据和乙方案的缓存证据；来源数量满足，乙方案的比较分支却没有被回答问题真正覆盖。

这会把“来源多样性”误当成“子问题覆盖”，属于检索计划与证据门之间的状态断链，而不是简单的 Top-K 调参问题。

## 2. 对照的开源机制

- [LlamaIndex SubQuestionQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/sub_question_query_engine.py) 把每个子问题作为独立检索分支，再把分支结果交给合成阶段；本地实现保留轻量方案，不引入框架，但补上分支是否有证据的确定性检查。
- [LlamaIndex DecomposeQueryTransform](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/query/query_transform/base.py) 将分解结果作为明确的子问题集合；因此“有两个来源”不能替代“每个子问题都有支持证据”。
- [Haystack QueryExpander](https://github.com/deepset-ai/haystack/blob/main/haystack/components/query/query_expander.py) 将扩展查询作为有界、可观察的检索输入；本地仍把 query variants、subqueries 和 corrective pass 分开记录。

## 3. 最小实现

新增 `evaluate_subquery_coverage()`：

- 对每个实际生成的子问题建立不含正文的 `subquery -> evidence_ids` 覆盖摘要；
- 同时检查来源/章节/正文身份和原始问题的非泛化关注点，避免仅因两个文件不同就判定覆盖完整；
- 缺失任一分支时触发一次既有纠偏预算；重试不得用更高分但缺少分支的结果替换完整覆盖；
- 最终在回调 trace 中输出 `subquery_coverage`，缺失时以 `subquery_coverage_unmet` 拒答，不调用答案模型。

没有把普通单文档问题强行拆分，也没有增加无限 Agent loop、HyDE、额外模型评审或新的检索框架。

## 4. 失败优先与验证

- 旧实现反例：甲方案审计证据 + 乙方案缓存证据仍满足两来源并继续生成；新增测试先复现该行为。
- 修复后 `tests/test_context_gate.py tests/test_rag_prompting.py tests/test_secure_retrieval_flow.py tests/test_graph_rag.py`：`185 passed`。
- 反例要求 `missing=["乙方案 核心证据"]`、trace `stop_reason=subquery_coverage_unmet`，且答案模型调用次数为 `0`；正例要求两侧审计证据均覆盖。

## 5. 明确边界

- 这是确定性分支覆盖门，不是模型级答案正确率、召回率或因果证明；覆盖摘要只说明证据是否命中分支，不证明证据本身为真。
- 仍使用现有一次纠偏重试和混合检索，不宣称完整多跳 Agentic RAG；后续若要引入逐子问题独立检索，必须用冻结数据集比较召回、延迟和显存。
- 本轮代码稳定后的完整工程门已通过：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。
