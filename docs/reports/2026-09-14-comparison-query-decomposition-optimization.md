# 比较问题子查询分解完整性优化

## 结论

本轮修复了一个会让 Agentic RAG “看起来已分解、实际上没有子查询”的边界：`query_requires_source_diversity()` 能识别自然中文比较问法，但 `split_comparison_query()` 只覆盖带“两类资料/两个方案/冒号”等后缀的句式。像“请对比项目A与项目B的安全风险”原先会得到 `action=decompose`、`subqueries=[]`，纠偏轮无法按两侧来源分别检索。

现在在已有书名号、数量词和资料类型解析之前，保留一个有界的比较主体兜底解析：识别“对比/比较 + 左侧 + 与/和/及/vs + 右侧”，并在“的/在/之间/方面/是否/如何/句末”等边界截断主体。结果仍是确定性的 `左侧 核心证据`、`右侧 核心证据`，不引入新的 Agent 框架或额外模型调用。

## 外部实践与本地映射

- [LlamaIndex DecomposeQueryTransform](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/query/query_transform/base.py) 将复杂问题转换为子查询；[SubQuestionQueryEngine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/sub_question_query_engine.py) 再分别执行并汇总。SoulDrive 采用更窄的确定性比较主体抽取，保留现有两次检索预算。
- [Haystack QueryExpander](https://github.com/deepset-ai/haystack/blob/main/haystack/components/query/query_expander.py) 要求扩展结果有界、结构化并保留原问题；本地继续把原查询作为第一变体，子查询只作为一次纠偏/分解入口。
- 本轮没有照搬 LLM 自由分解，因为本项目当前优先保证项目范围、来源多样性和可复现性；自然语言解析失败仍回到原有查询路径，不扩大召回范围。

## 失败优先与修复

- 反例：`请对比项目A与项目B的安全风险`。旧实现 `query_requires_source_diversity=True`，但 `split_comparison_query=[]`，形成空分解计划；修复后得到 `项目A 核心证据` 和 `项目B 核心证据`。
- 反例：`比较甲方案与乙方案的安全审计做法。` 同样进入两个主体的子检索；带书名号、`两类资料：` 的既有路径保持原结果。
- 解析只接受 2 至 80 个字符的主体，连接词和结束边界固定，最多仍受 `expand_query_variants(limit=5)` 限制，避免自由文本把整句问题误当作来源名称。
- 单文档规则“实验指标在什么条件下可以比较？”仍不触发来源多样性和分解。

## 已验证

- 先运行旧逻辑的新增反例：`split_comparison_query()` 返回空列表，测试失败。
- 修复后查询扩展、上下文门、RAG 纠偏和 Agentic trace 定向回归：`127 passed`。
- GraphRAG 身份改动后的前一轮完整工程门为 `1440 passed, 1 warning`；本轮查询分解改动后的完整工程门为后端 `1441 passed, 1 warning`、前端 8 组脚本 `62 passed`、锁定 Cargo 通过。

## 边界

- 该改动只补齐显式比较主体的确定性分解，不证明子查询召回率、最终答案准确率或跨语言比较解析覆盖。
- 不引入 HyDE、LLM 自主多跳规划、工具调用循环或 Agentic RAG 新状态机；这些候选仍需冻结评测集后单独比较 baseline。
- 不改变原始用户问题作为证据门契约，也不绕过项目/会话/来源范围过滤。
