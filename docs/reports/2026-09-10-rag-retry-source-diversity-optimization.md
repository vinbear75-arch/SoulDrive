# RAG 纠错检索的来源覆盖保护优化

## 结论

本轮修复了一个会让比较类问题退化的真实链路缺口：首轮检索已经保留多个来源时，纠错检索只按置信分数选择结果，可能用更高分的单一来源替换原有多来源上下文。现在对于需要来源多样性的查询，纠错结果不得降低首轮已获得的来源覆盖；如果纠错结果覆盖不足，系统保留首轮证据并继续走现有证据门控，而不是生成一个片面的比较答案。

## 外部实践对照

- [Microsoft GraphRAG Query Engine](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md)：将 local、global、DRIFT 和 basic search 区分为不同查询路径；global search 面向全库综合，不能只看单个局部命中。
- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)：结构化图信息与原始文本块共同进入上下文，并在上下文窗口前经过排序和过滤。
- [DSPy CompleteAndGrounded](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/diving-deeper/metrics-and-evaluation.md)：将答案完整性与上下文支撑度分别纳入评测，不能用一个更高的局部得分替代整体 groundedness。

本项目不引入 GraphRAG/DSPy 运行时；只把“多来源覆盖是比较问题的约束”落到已有的有界纠错循环中。

## 实施

- `core/rag_engine.py`
  - 新增 `_retry_preserves_source_diversity`。
  - 只对 `query_requires_source_diversity` 判定为真的问题启用保护；普通事实查询仍沿用原有分数优先规则。
  - 将来源按 `project_id + source_filename` 组成稳定键，避免同名文件跨项目被误合并。
  - 比较查询的重试最多保留已有来源覆盖的分支数量；首轮只有一个来源时，重试得到两个来源仍可升级，首轮已有两个来源而重试只剩一个时拒绝替换。
  - `rag.retrieval_retry` 审计事件新增 `source_diversity_preserved`，能够区分“重试没用上”是分数不足还是覆盖约束不满足。

- `tests/test_rag_prompting.py`
  - 新增回归：比较甲乙方案时，单来源高分重试不得替换首轮双来源证据；最终证据保持双来源，并在证据门仍不满足时拒答。

## 验证

- 定向 RAG、上下文门、GraphRAG、答案质量和评测回归：`190 passed`。
- 本轮失败测试先复现旧行为：高分单来源重试将最终证据数从 2 降为 1；加入保护后同一测试通过。
- 项目统一检查已通过：后端 `1071 passed, 1 warning`，Python compileall 通过；前端构建通过，7 组 UI 测试共 `48 passed`；Tauri `cargo check --locked` 通过。之后的 `experiment_compare` 工作流级来源策略和历史记忆时点回归也已纳入该全量结果。

## 边界

- 这是来源覆盖保护，不是对答案正确率或语义完整性的证明；没有冻结的多来源答案集时，不宣称质量提升百分比。
- 保护只约束显式需要来源多样性的查询；普通单文档事实查询不增加额外检索成本。
- 如果首轮本身只召回一个来源，系统不会凭空制造第二个来源；是否能补齐仍由第二轮检索、证据门控和最终人工/评测数据决定。
