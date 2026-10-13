# 工作流查询与输出契约分离优化

## 结论

本轮修复了会让不同研究窗口影响检索排序的真实链路问题：`/chat` 过去把 Playbook 标题、输出要求和会话约束拼接进 RAG 的 `query`。这会让检索器同时处理用户问题和控制文本，导致同一个问题仅因切换“论文精读/实验对比”窗口就改变候选召回，而且控制文字可能污染关键词、向量和 GraphRAG 路由。

现在用户原始问题作为 RAG query 进入检索与生成；工作流角色通过已校验的 `workflow_id` 结构化传入 `interaction_context`，由回答提示、引用命名空间、输出章节契约和输入门控执行。这样窗口角色仍然真实生效，但不再以拼接控制语句的方式改变检索语义。

## 外部实践对照

- [Haystack pipeline base](https://github.com/deepset-ai/haystack/blob/main/haystack/core/pipeline/base.py)：管线输入与管线元数据分开管理；组件输入不应把路由控制信息伪装成业务文本。
- [Haystack MetadataRouter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/routers/metadata_router.py)：根据结构化 metadata 路由文档，而不是把路由规则拼到文档内容中。
- [LlamaIndex workflow memory documentation](https://github.com/run-llama/llama_index/blob/main/docs/src/content/docs/framework/module_guides/deploying/agents/memory.mdx)：工作流 Context 用于运行时状态，用户输入通过显式参数传递；两者不混为一段自然语言输入。

本项目不引入 Haystack 或 LlamaIndex 运行时，只把“用户 query 与工作流控制状态分离”的边界落到现有 FastAPI/RAG 链路。

## 实施

- `core/mcp_server.py`
  - `/chat` 不再调用 `build_workflow_query` 拼接 Playbook 文本。
  - 保留 `workflow_id`、项目、选中文档和会话上下文的结构化传递。
  - RAG 的输入 query 恢复为用户原始问题，使检索、查询改写、证据门和答案生成共享同一个真实问题。
- `tests/test_multimodal_workflows.py`
  - 将论文精读、实验对比和会话恢复回归改为断言 RAG 收到原始问题。
  - 同时断言 `workflow_id` 仍存在于结构化交互上下文，避免通过“删掉工作流提示”把角色功能误删。

## 验证

- 先按旧行为运行新回归：工作流测试因 RAG query 含控制契约而失败。
- 修复后定向工作流与 RAG 提示词回归：`108 passed, 1 warning`。
- 全量 `scripts/check.ps1`：后端 `1071 passed, 1 warning`；Python compileall、前端生产构建、7 组 UI 测试 `48 passed`，Tauri `cargo check --locked` 均通过。

## 边界

- 本轮只解决 query/控制契约混用，不宣称已经完成所有工作流检索策略的差异化评测。
- 论文精读、实验对比和会议纪要已有输入门控、证据命名空间及输出章节差异；后续仍需逐项审计它们是否在检索路由和评测指标上有足够可观察的行为差异。
- 不把工作流标题或输出章节当作文档证据，事实仍必须通过现有证据和引用校验。
