# SoulDrive 研究会话文档输入范围校验优化记录

日期：2026-09-09  
范围：`paper_review`、`experiment_compare`、`meeting_actions` 的文档输入预检与 RAG 前边界

## 结论

之前的工作流输入门只验证客户端提交的文件名数量，不能证明文件已经完成索引，也不能证明文件属于当前项目或全局共享范围。这样旧客户端可以把任意文件名声明为“已索引论文”，虽然后续 RAG 通常会因项目过滤找不到证据，但业务会先错误地把任务判为 ready。

现在服务端在工作流计划接口和 `/chat` 入口统一检查本地文档库：

- 文件不存在、尚未完成索引或索引失败：计划置为未就绪，不进入 RAG；
- 显式绑定到其他项目：拒绝作为当前会话证据；
- 已索引的全局共享文档：允许在任意项目会话中使用；
- `plan_workflow` 仍保持纯函数，只判断输入形状；真实存储/范围核验留在服务边界，避免让 UI 声明代替授权和索引事实。

## 外部实践对齐

- [Haystack SECURITY.md](https://github.com/deepset-ai/haystack/blob/main/SECURITY.md) 明确将 URL、文件路径、过滤表达式和查询的输入验证责任放在应用边界，而不是交给底层 pipeline 猜测；本轮沿用这个轻量原则。
- [LangGraph Persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx) 将线程状态和可恢复工作流状态持久化；SoulDrive 当前不引入 Agent 图，而是在已有会话和项目范围边界上先把输入事实固定下来。

## 实现与回归

- `core/mcp_server.py`
  - 新增 `_validate_workflow_document_inputs`，复用本地文档库的索引状态和项目/全局绑定信息。
  - `/chat` 与 `/multimodal/workflows/plan` 都执行同一校验；失败返回现有 `422 + workflow_plan.missing_inputs` 契约。
- `tests/test_multimodal_workflows.py`
  - 新增未完成索引文档不得进入 Fake RAG 的回归。
  - 新增全局共享文档允许、其他项目文档拒绝的 API 回归。
  - 新回归均先在旧行为上失败，再在修复后通过。

## 验证边界

- 会话/GraphRAG/安全检索专项：`65 passed, 1 warning`。
- 该校验证明了入口边界和项目范围，不等于真实模型回答正确；索引完成后的内容质量仍需用冻结评测集验证。
- 当本地文档库无法核验时，工作流按失败关闭并提示刷新文档库；本机模式不引入云端目录或额外分布式服务。
