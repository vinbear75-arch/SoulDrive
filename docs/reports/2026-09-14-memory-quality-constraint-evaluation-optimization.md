# 项目约束记忆质量评测补齐记录

日期：2026-09-14  
范围：记忆质量评测集、项目约束 context gate、身份投影和评测契约。

## 1. 本轮发现的真实缺口

`project_constraint` 已经进入运行时记忆审核、项目作用域和 RAG gate，但原有 `scripts/eval_memory_quality.py` 仍只认识 `user_preference`、`project_fact` 和 `workflow_fact`。因此完整评测集不会覆盖新记忆类型，也不能验证“当前查询没有约束关键词时，已确认的项目约束仍然进入 context gate”。

这属于评测闭环缺口，不是再增加一个记忆服务的理由。若不补齐，后续改动可能让约束重新退化为普通关键词召回而不被质量门发现。

## 2. 对照的开源机制

- [Mem0 memory-benchmarks](https://github.com/mem0ai/memory-benchmarks)：采用 Ingest → Search → Evaluate 分层，并单独报告事实召回、时间推理、冲突等能力；本轮将 SoulDrive 的“进入检索结果”和“通过读取 gate 进入上下文”分开检查。
- [Mem0 memory flow](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)：强调写入和读取是两个阶段，并要求以 `user_id`、`agent_id`、`run_id` 等元数据做 scope 过滤；本轮将项目约束的 `project_id` 作为必需范围，并在 context 评测中复用项目过滤。
- [Midas](https://github.com/vornicx/Midas)：强调本地、确定性、可复现的 recall/precision 评测和失败轨迹；本轮保持无 LLM judge、固定数据集、逐查询输出和零敏感正文外泄。

这些项目的 benchmark 不能直接作为 SoulDrive 的业务准确率；本轮只借鉴“分阶段评测、作用域约束、可复现诊断”的机制。

## 3. 已实现的最小闭环

- `SUPPORTED_SUBTYPES` 纳入 `project_constraint`，并要求评测事件必须带 `project_id`。
- 新增 `context` 评测操作：先按项目、工作流和时间范围读取 durable memory，再复用 `gate_memory_context()`，检查最终进入上下文的记忆，而不是只检查关键词搜索结果。
- 评测别名映射同时支持 `memory_id` 和 `asset_id`。这是必要的身份边界：持久化搜索响应使用 `asset_id`，context gate 使用权威 `memory_id`，两者不能靠字段名偶然一致。
- 评测可以声明 `expected_gate_reasons`，因此项目约束场景不仅要召回正文，还要证明是以 `project_constraint` 规则通过 gate，而非碰巧命中关键词。
- 冻结数据集新增 `MEM-CONSTRAINT-001`：查询“请概览本轮材料”不包含约束正文关键词，仍必须返回当前项目的项目约束；其他项目和 workflow 场景保持原有检查。

## 4. 失败优先与结果

先加入场景后，旧评测器按预期失败：`project_constraint` 被报告为 unsupported memory subtype。

扩展 `context` 后首轮又暴露评测器自身的身份映射错误：gate 已返回 `context_gate_reason=project_constraint`，但评测器只读取 `asset_id`，导致假阴性。修复为同时读取 `memory_id`/`asset_id` 后：

- `tests/test_memory_quality_eval.py`：`3 passed`；
- 冻结 `eval/memory_quality_v0.1.jsonl`：`7` 个场景、`8` 个查询，`8/8` 通过；
- 其中项目约束 context 查询返回 `project_constraint` gate reason，项目隔离、workflow 隔离、更新时间和冲突淘汰仍保持通过。

收尾工程门已通过：后端 `1394 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。前端保留既有 chunk-size warning，不作为失败。

## 5. 明确未宣称的内容

- 这是确定性本地评测闭环，不是 LongMemEval/LoCoMo 的业务答案准确率，也没有引入 LLM judge。
- 评测证明的是持久化范围、读取 gate、身份映射和固定场景行为，不证明自动记忆抽取语义准确率、跨模态长期记忆、图记忆或跨存储 exactly-once。
- `context` 评测复用了本地 gate 的确定性逻辑；真实聊天还包含 embedding、RAG 证据、模型生成和 Tauri UI，因此仍需把它们分别验收。
