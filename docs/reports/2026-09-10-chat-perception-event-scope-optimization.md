# SoulDrive 聊天感知事件项目范围优化记录

日期：2026-09-10

## 本轮结论

`/chat` 现在会在会话项目绑定解析完成后，先核验感知事件及其候选目标的显式 `project_id`。只要显式项目与持久会话（或请求的项目上下文）不一致，就在幂等回放、请求收据认领和 RAG 执行前返回 `409`；事件没有携带项目字段时继续兼容旧客户端，由持久会话/活动上下文作为唯一项目范围来源。

这补上了“项目、文档和会话已经隔离，但客户端事件仍能把另一项目的选区文本带入视觉证据”的链路缺口。校验覆盖事件本身和 `target_candidates[*].project_id`，避免只检查外层事件而漏掉候选目标。

## 外部实践依据

- LangGraph 持久化文档将 `thread_id` 作为短期状态的持久边界，并以显式 namespace 管理长期记忆；恢复状态时不能只相信客户端携带的临时字段：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
- Open WebUI 的文件访问实现会在服务端根据所有权、知识库或共享会话重新计算可访问资源，不把客户端提交的文件关联当作授权：[Open WebUI file access control](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/access_control/files.py)。
- Open WebUI 的安全公告指出，伪造聊天文件关联可能把未授权文件带入上下文，修复原则是服务端在附加文件前再次过滤：[GHSA-vrhc-3fr6-pc3c](https://github.com/open-webui/open-webui/security/advisories/GHSA-vrhc-3fr6-pc3c)。
- Restate 将幂等键绑定到完整调用输入并在服务端保存调用结果；这支持本轮把“事件项目范围必须先校验”放在聊天幂等回放之前：[Restate key concepts](https://docs.restate.dev/foundations/key-concepts)。

本轮只借鉴服务端重新计算授权范围的边界原则，没有引入外部框架，也没有把未携带项目字段的历史客户端强制升级为必须传项目字段。

## 实现范围

1. 在 `core/mcp_server.py` 增加 `_perception_event_scope_response`，比较事件和候选目标的显式项目与当前绑定项目。
2. `/chat` 在会话元数据读取完成后立即执行该校验，位置早于请求消息查找、幂等回放、请求收据认领和模型加载。
3. 只拒绝明确冲突；缺失 `project_id` 的事件仍由持久会话项目或活动上下文约束，保持旧客户端兼容。
4. 不把事件项目字段反向提升为会话项目；无持久项目边界的全局聊天仍保持原有语义。

## 验收证据

- 先添加两个失败回归：旧实现对“事件本身属于另一项目”和“候选目标属于另一项目”都错误返回 `200`。
- 修复后新增回归通过：
  - `test_chat_rejects_perception_event_from_another_project`
  - `test_chat_rejects_perception_candidate_from_another_project`
- 相关会话、请求收据和交互上下文回归：`tests/test_mcp_server_learning.py tests/test_conversation_store.py tests/test_interaction_context.py`，共 `107 passed, 1 warning`。
- Fake RAG 在跨项目事件场景主动抛错；本轮结果证明越界事件没有进入模型加载路径。
- 随后的仓库级 `scripts/check.ps1`：后端 `1060 passed, 1 warning`；Python compileall、前端构建、7 组 UI 测试 `46 passed`、`cargo check --locked` 均通过。

## 边界与后续

- 本轮证明的是事件项目范围门，不证明事件内容本身真实，也不证明 OCR/VLM 的目标识别正确。
- 没有项目绑定的全局会话仍允许显式事件存在；如果未来全局会话也需要严格的事件命名空间，应先定义“全局事件”和“项目事件”的业务契约，再补评测，不在本轮静默改变语义。
- 聊天请求收据已在相邻优化中绑定完整显式上下文摘要；本轮事件范围门与该摘要共同保证越界事件既不能直接进入 RAG，也不能绕过幂等冲突检查。
