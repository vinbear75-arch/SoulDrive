# 2026-09-14 RAG 检索计划子问题状态优化

日期：2026-09-14  
范围：`plan_retrieval()`、比较/多来源查询的 Agentic Retrieval trace。

## 1. 本轮发现的真实缺口

当查询包含“分别说明两个方案的风险与收益”这类多来源意图时，系统能识别需要至少两个来源，但无法从文本中安全识别两个具体主体。旧实现仍返回 `action=decompose` 且 `subqueries=[]`，导致审计与纠偏状态把“没有生成子问题”描述成“已分解”。

这不是召回率问题，而是 Agentic RAG 状态机的状态不诚实：后续纠偏器只能退回 query variant/top-k 路径，却没有对应的两侧子问题。

## 2. 对照的开源机制

- [LlamaIndex DecomposeQueryTransform](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/query/query_transform/base.py) 将查询分解结果作为明确的子问题列表；没有子问题时不能把结果等同于已完成分解。
- [LlamaIndex SubQuestionQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/sub_question_query_engine.py) 以子问题驱动逐分支检索和聚合；本地保留更小的两轮预算，不引入该框架。
- [Haystack QueryExpander](https://github.com/deepset-ai/haystack/blob/main/haystack/components/query/query_expander.py) 把 query variants 作为显式扩展结果，并限制扩展数量；本地仍区分 variants、subqueries 与 corrective pass。

## 3. 最小实现

`plan_retrieval()` 现在按实际结果选择状态：

- 有两个或以上可识别主体：`action=decompose`，保留有界 `subqueries` 和两轮纠偏；
- 只有多来源/复合意图但没有可安全生成的子问题：`action=retrieve`、`query_kind=multi_source_query`、`subqueries=[]`，增加 `subquery_generation_unavailable` 原因，仍保留两轮纠偏和最终 source-diversity gate；
- 单文档比较规则继续走普通 `retrieve`，社交句继续 `skip`。

这样没有凭空制造“方案一/方案二”名称，也没有因分解失败而取消至少两份来源证据的要求。

## 4. 失败优先与验证

旧实现对“分别说明两个方案的风险与收益”返回 `decompose`，新反例失败；修复后：

- `tests/test_context_gate.py tests/test_query_expansion.py`：`26 passed`；
- 已有自然中文“对比项目A与项目B”分支仍产生两条 `核心证据` 子查询；显式书名号分支不变。

截至本轮最新完整工程门：后端 `1473 passed, 1 warning`；前端生产构建通过，8 组脚本合计 `62 passed`；`compileall` 与 `cargo check --locked` 通过。Vite chunk-size warning 与 Starlette/httpx 弃用提示均为既有非阻塞 warning。

## 5. 明确边界

- 这只修复检索计划状态和可审计原因，不证明子问题召回率、答案准确率或多跳 Agentic RAG 已完成。
- 没有引入 HyDE、LLM judge、DRIFT 或新的 Agent framework；无法安全分解时保守检索并由证据门决定是否拒答。
