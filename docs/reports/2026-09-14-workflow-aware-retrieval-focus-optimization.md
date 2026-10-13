# 研究窗口检索 Focus 落地优化

日期：2026-09-14

## 发现

SoulDrive 已经为 `paper_review`、`experiment_compare` 和 `meeting_actions` 定义了不同
的检索 profile、输出章节和证据命名空间，但 RAG 入口原先只在“选中文档”或“实验结构化
对比已就绪”时注入窗口 focus。会议窗口如果只有项目绑定、没有选中文档，仍会走项目检索，
却不会把“会议结论、行动项、负责人、截止时间”等 profile 重点传给检索器。这样窗口在
规划层有区别，实际召回层却没有完全兑现区别。

## 公开实践对照

- [Microsoft GraphRAG query overview](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md)
  把普通、local、global、DRIFT 和问题生成作为不同查询路径；本轮借鉴“查询路径必须显式
  区分”的原则。
- [Microsoft GraphRAG local search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)
  强调查询与会话历史共同决定实体/原文候选，再按证据上下文生成；本地仍保留当前项目、
  文档和证据门，不把 GraphRAG 社区摘要直接当事实证据。

## 实际改动

当存在有效 `active_project_id` 且会话绑定了已知 `workflow_id` 时，即使没有选中文档，
也生成并限制在现有 `MAX_RETRIEVAL_VARIANTS` 内的窗口 focus。论文窗口和实验窗口的
已有行为不变；会议窗口现在能够把自身的行动项 focus 送入项目检索。该改动是确定性的
查询变体注入，不新增模型调用，不新增自由 Agent 循环，也不改变原始用户问题的证据门。

## 失败优先与验收

- 新增失败优先测试：项目绑定的会议窗口、无选中文档时，修复前
  `workflow_retrieval_variant_count=0`；修复后检索变体包含“行动项/负责人” focus。
- 论文精读、实验对比和会议窗口相关 RAG/工作流组合：`127 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1384 passed, 1 warning`；前端生产构建通过，8
  组 UI 测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。

## 边界

这只证明窗口 profile 在一个实际 RAG 入口被兑现，不证明会议行动项抽取的事实准确率、
跨模型泛化或完整 Agentic RAG 质量。当前系统仍是有界的 query focus + 一次条件改写 +
一次纠错检索；尚未引入 Microsoft GraphRAG DRIFT 式社区追问，也没有因此宣称已经完成
Agentic RAG。后续若引入 DRIFT/多跳规划，必须先冻结多跳评测集并证明召回、引用支持、
延迟和项目隔离没有退化。
