# 记忆读取 ID 级可追溯优化报告

日期：2026-09-10

## 目标

让一次 RAG 请求实际注入了哪些长期记忆可以从 gate 和审计事件中定位，同时避免把记忆正文写入审计日志。此前 gate 只记录 accepted/rejected 数量，虽然每个 memory item 自带 `source_trace`，但聚合日志无法直接知道选择了哪些 ID。

## 外部实践对照

- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 的 archival search result 显式返回记忆 `id`、时间戳、内容和 tags；结果身份是读取可追溯性的基础字段。
- [Letta archival memory functions](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py) 返回插入记忆的 ID，并建议使用自包含内容和描述性标签，而不是依赖不可定位的片段。
- [Graphiti episode attribution](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 将提取事实映射回 episode index，使事实选择可以回溯到来源 episode，而不必把整段原始内容写入运行日志。

## 最小实现

- `core/memory_runtime.py::gate_memory_context` 的 gate 结果新增 `accepted_memory_ids`，只包含最终进入 capsule 的 memory/asset ID。
- `core/mcp_server.py` 的 `learning.injected_into_rag` 审计事件新增 `memory_ids`，与 gate 使用同一最终集合，避免中间候选和最终注入结果混淆。
- 不复制标题、正文、source excerpt 或敏感记忆内容到聚合审计字段；逐条 memory 仍保留已有 `source_trace`、项目范围和时间元数据。

## 测试证据

- 先写回归测试：旧 gate 没有 `accepted_memory_ids`，测试失败。
- `tests/test_memory_gate.py`：`7 passed`。
- 记忆门、学习 API 和 RAG 提示词专项：`143 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1066 passed, 1 warning`；Python compileall 通过；前端生产构建通过；7 组前端测试 `48 passed`；Tauri `cargo check --locked` 通过。
- Vite 单 chunk 约 `576.12 kB` 的提示和 Starlette/httpx 弃用提示仍是环境警告，不是本轮失败。

## 边界

- 这是 ID 级运行审计，不等于自动证明记忆内容正确；内容真实性仍依赖 source trace、人工审核、时间范围和读取冲突门。
- 审计日志不记录记忆正文，调试需要通过本地 memory ID 再走现有项目范围校验读取。
- 真实 Tauri/WPS、安装包、模型/视觉服务和冻结留出集答案质量仍未由本轮自动检查闭合。
