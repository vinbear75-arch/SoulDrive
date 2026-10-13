# 运行时记忆继承研究窗口 provenance

## 外部机制与本地缺口

[Graphiti 的 MCP 说明](https://github.com/getzep/graphiti/blob/17ed4b707320f092b5e79bdbd9d45130c7f84259/mcp_server/src/graphiti_mcp_server.py)把 episode 按 `group_id` 分区，并区分事件发生时间与写入时间；[LongMemEval](https://arxiv.org/abs/2410.10813)把多会话推理、时间推理、知识更新和 abstention 作为独立长期记忆能力。SoulDrive 不引入图数据库或外部记忆服务，只把这两个边界落实到本机事实源：自动记忆必须保留产生它的 session/workflow provenance，且 provenance 不得被客户端任意覆盖。

普通反思入口已经把 `source_session_id`、`source_workflow_id` 写入候选、归档和派生同步；此前 `/learning/memories/runtime` 的自动接受路径只校验项目并传递 `session_id` 给投影，canonical memory 本身没有来源会话和研究窗口。这样论文、实验或会议窗口产生的用户偏好虽然可能保存成功，却无法从记忆事实层解释其来源。

## B0 反例

新增失败优先测试：创建绑定 `paper_review` 的项目会话，经运行时记忆接口保存用户偏好，再从 LearningStore 读取带 metadata 的 active memory，要求同时存在 `source_session_id`、`source_workflow_id` 及对应 `source_trace`。旧实现实际缺少这些字段，结果为 `1 failed, 151 deselected, 1 warning`。

## 最小修复

- 从已存在的 session manifest 读取 `workflow_id`，不新增客户端可伪造的 runtime `workflow_id` 字段；项目仍以 session 与请求的现有校验为准。
- 将绑定 session/workflow 注入 runtime memory candidate，复用现有 `_candidate_payload`、canonical memory、加密 archive 和 memory outbox 的 provenance 传播路径。
- 将 workflow 身份纳入 reflection review digest，使同一 session 的窗口身份参与原 review 回放；保留旧 receipt 的 `message + project_id` 输入摘要格式，避免升级后旧完成回执无谓变成 409。
- `user_preference` 的事实 scope 仍是 global、`project_id=null`；`source_workflow_id` 只表示来源，不把全局用户偏好错误限制在某个研究窗口。
- 没有修改冻结评测集、gold、业务种子、训练或调参。

## 验收证据

- B0：`1 failed, 151 deselected, 1 warning`。
- 修复后运行时记忆/回执回归：`11 passed, 1 warning`。
- 运行时记忆与反思来源/项目边界交叉回归：`14 passed, 1 warning`；LearningStore canonical scope：`2 passed`。
- 第二次独立完整 `scripts/check.ps1`：退出码 `0`；后端 **1771 passed、1 warning，445.04 秒**；记忆质量 **7/7 场景、8/8 查询、Recall@K=1.0、陈旧/禁止泄漏率=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；episodic runtime **5/5**；前端生产构建成功、UI **66 passed**；`compileall` 与锁定 Cargo 检查通过。

## 仍未证明

- 这是本机 SQLite/加密归档及其派生 outbox 的 provenance 闭合，不是跨主机图记忆、真实 Redis/PostgreSQL、断电恢复或 exactly-once 证明。
- `source_workflow_id` 是来源审计字段，不等于 workflow-specific memory visibility；真正的 `workflow_fact` 仍由既有 workflow namespace 过滤。全局用户偏好跨窗口可见是当前设计，而不是 LongMemEval 分数或长期记忆质量结论。
- 当前测试证明字段传播和回放身份，不证明开放式跨会话答案质量、自动知识失效、GraphRAG 内容收益或真实 UI/E2E。
