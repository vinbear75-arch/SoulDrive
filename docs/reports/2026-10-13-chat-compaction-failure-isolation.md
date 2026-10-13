# 聊天答案已持久化后的 compaction 故障隔离

日期：2026-10-13。范围：Windows 本机、既有加密 ConversationStore/ProductionSessionStore；不是新 durable 框架。

## 外部机制与本地缺口

[Temporal 的工作流回放说明](https://docs.temporal.io/workflows)将已记录的执行历史作为恢复依据，回放复用已记录结果；[DBOS 的恢复说明](https://docs.dbos.dev/production/workflow-recovery)则明确 pending workflow 恢复依赖运行时管理。这两者提供“业务提交、派生计算与恢复承诺要分别说明”的参考边界，不证明 SoulDrive 已具备其运行时。

本地 `/chat` 在 assistant 记录与 completed request receipt 已写入后执行 `compact_session()`。旧路径把它放在覆盖整个生成过程的异常处理内；摘要派生失败后会把已成功的回答重新当成生成失败，再尝试写错误 assistant，从而出现虚假的“模型服务不可用”文本以及相同消息 ID 的冲突。

## B0 -> B1/Candidate

固定输入：新增 `test_chat_compaction_failure_does_not_reclassify_persisted_answer`，使用临时真实加密会话存储、关闭 Redis/PostgreSQL 投影、固定一次 RAG 回答，并令 compaction 抛出 `OSError`。同一 request 再在关闭/重开的存储上重放。

| 对照 | 改动因素 | 结果 |
| --- | --- | --- |
| B0 旧行为重建 | 仅撤去本次 compaction 局部异常隔离；不 reset 工作区 | `1 failed, 153 deselected, 1 warning`；正常回答后被追加服务错误，并触发消息身份冲突 |
| B1 / Candidate | 仅隔离派生摘要异常；保留已保存答案与 completed receipt | 反例通过；答案仅生成一次，归档保持 user/assistant 两条，同键重放原答案 |
| 交叉回归 | 聊天/学习、会话存储、outbox 故障隔离、RAG prompting | `380 passed, 1 warning`，285.00 秒 |

这是确定性故障合同回归，不是模型性能实验；无必要引入 B2 框架。此前误用 base Python 导致的缺 `redis` 导入失败不计为有效 B0；上述结果均来自 `souldrive` Conda 环境。

## 实现与观测

- `compact_session()` 单独捕获异常，写 best-effort `chat.compaction_failed`；不把 completed receipt 降级，不追加失败 assistant。
- `chat.completed` 增加 `compaction_status`，区分 `not_attempted/not_needed/completed/failed`。
- 反例检查首次完成事件为 `failed`，同键回放事件为 `not_attempted`，没有 `chat.failed`；RAG 只被调用一次。
- 未增加摘要重试队列，也未改请求幂等输入、正文、scope 或压缩阈值。

## 完整回归与复现范围

先后两次 `scripts/check.ps1` 退出 0：后端分别 `1778 passed, 1 warning`（499.27 秒）及 `1779 passed, 1 warning`（453.09 秒）；后者包含新增产物入口测试。记忆 `7/7` 场景、`8/8` 查询，GraphRAG route `4/4`，窗口角色 `6/6`，episodic runtime `5/5`；前端生产构建、66 项脚本、compileall 和 `cargo check --locked` 均通过。

这两次结果由工具回传确认，但 transcript 文件漏收原生命令输出，不能把文件本身当完整原始日志。第三次完整输出归档复跑退出 0：后端 `1779 passed, 1 warning`、459.61 秒，前端 66 项及四个小型质量门、编译/构建检查均通过。已核对 1086 行日志与代码指纹，最终记录见 `2026-10-13-artifact-smoke-confirmed-workflow.md`。

固定代码指纹：`core/mcp_server.py` SHA-256 `331064d6ef1018ade6a64add3f3891180863985ac40b074b191754e5dde4fac5`；`tests/test_mcp_server_learning.py` SHA-256 `1e082bea61beca7a3bbe60ce5587b57a22b749e7c8b32d2c764101abe7963508`。公共环境与其余指纹见上述总回归记录。

## 未证明

未证明摘要失败后的自动重试/崩溃恢复、跨存储原子性、真实 Redis/PostgreSQL、断电、跨主机 exactly-once、流连接中断交付、模型回答正确率或真实 Tauri UI/E2E。该修复只保证“派生摘要失败不推翻已经持久化的本次聊天成功”。
