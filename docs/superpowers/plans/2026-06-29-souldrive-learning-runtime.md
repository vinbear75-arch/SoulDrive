# SoulDrive Learning Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前学习闭环补成可审核、可检索、可匹配、可受控执行、可评估的端侧 Learning Runtime。

**Architecture:** 保留已有 `LearningStore`、`Reflection`、RAG 注入和前端审核。新增 validator、memory runtime、skill runtime、skill run log 和 runtime eval，API 与 `/chat` 共用同一套能力，避免“页面能看但回答不用”的鸡肋实现。

**Tech Stack:** Python `unittest`、FastAPI/Pydantic、SQLite、AESGCM、React/Tauri、Node static tests。

**Commit Policy:** 不提交 commit。项目指令要求除非用户明确要求，否则禁止自动 commit。

---

## Task 1: LearningValidator

**Files:**

- Create: `core/learning_validator.py`
- Modify: `core/mcp_server.py`
- Test: `tests/test_learning_validator.py`, `tests/test_reflection.py`

- [ ] 写失败测试：模型候选 evidence 不在 source 消息中时被拒绝。
- [ ] 写失败测试：assistant-only 用户偏好 memory 被拒绝。
- [ ] 写失败测试：与已有 memory 重复时设置 `duplicate_of` 且 `selected_by_default=False`。
- [ ] 写失败测试：敏感/prompt-injection 候选被拒绝。
- [ ] 实现 `validate_learning_candidates(candidates, messages, existing_assets)`。
- [ ] 在 `/reflection/draft` 写入 review 前调用 validator。
- [ ] 运行 `conda run -n souldrive python -m unittest tests.test_learning_validator tests.test_reflection tests.test_mcp_server_learning -v`。

## Task 2: Structured Skill Payload

**Files:**

- Create: `core/skill_runtime.py`
- Modify: `core/learning_store.py`
- Test: `tests/test_skill_runtime.py`, `tests/test_learning_store.py`

- [ ] 写失败测试：accept skill 后 payload 含 `intent/triggers/steps/output_contract/success_criteria`。
- [ ] 写失败测试：自由文本 skill 可被标准化为至少一个步骤。
- [ ] 实现 `normalize_skill_payload(payload)`。
- [ ] 在 `LearningStore._candidate_payload()` 和 `_candidate_final_payload()` 中保留并标准化 skill schema。
- [ ] 让 `LearningStore.search()` 返回 skill 结构字段。
- [ ] 运行 `conda run -n souldrive python -m unittest tests.test_skill_runtime tests.test_learning_store -v`。

## Task 3: Memory Capsule

**Files:**

- Create: `core/memory_runtime.py`
- Modify: `core/mcp_server.py`, `core/rag_engine.py`
- Test: `tests/test_memory_runtime.py`, `tests/test_rag_prompting.py`

- [ ] 写失败测试：高价值 user_preference/project_fact 会进入 token-bounded capsule。
- [ ] 写失败测试：query-specific retrieved memory 与 capsule 分区输出。
- [ ] 实现 `build_memory_capsule(memories, max_chars)`。
- [ ] `/chat` 的 learning context 同时包含 `memory_capsule` 和 `memories`。
- [ ] `build_learning_context()` 分区渲染 capsule/retrieved memory，不放入文档证据区。
- [ ] 运行 `conda run -n souldrive python -m unittest tests.test_memory_runtime tests.test_rag_prompting -v`。

## Task 4: Skill Matcher and Guided Executor

**Files:**

- Modify: `core/skill_runtime.py`, `core/learning_store.py`, `core/mcp_server.py`, `core/rag_engine.py`
- Test: `tests/test_skill_runtime.py`, `tests/test_mcp_server_learning.py`, `tests/test_rag_prompting.py`

- [ ] 写失败测试：相似 query 能匹配正确 skill，普通事实查询不匹配。
- [ ] 写失败测试：`create_skill_execution_plan()` 返回 steps、required_inputs、output_contract、success_criteria。
- [ ] 写失败测试：`/learning/skills/match` 返回匹配分数与理由。
- [ ] 写失败测试：`/learning/skills/{skill_id}/runs` 创建 encrypted run log。
- [ ] `/chat` 高置信匹配时注入 `skill_execution`，并记录 `planned` run。
- [ ] 运行 focused API/RAG 测试。

## Task 5: Runtime Eval

**Files:**

- Modify: `scripts/eval_learning_loop.py`, `eval/learning_loop_v0.1.jsonl`
- Test: `tests/test_learning_eval.py`

- [ ] 写失败测试：seeded runtime 模式能加载 approved assets 并计算 recall/match/plan 指标。
- [ ] 实现 `run_seeded_runtime(records)`，不依赖真实 LLM。
- [ ] 输出 baseline 与 runtime 对比摘要。
- [ ] 运行 `conda run -n souldrive python -m unittest tests.test_learning_eval -v` 和 eval 命令。

## Task 6: Verification

- [ ] focused backend: `conda run -n souldrive python -m unittest tests.test_learning_store tests.test_learning_validator tests.test_memory_runtime tests.test_skill_runtime tests.test_reflection tests.test_mcp_server_learning tests.test_rag_prompting tests.test_learning_eval -v`
- [ ] full backend: `conda run -n souldrive python -m unittest discover -s tests -v`
- [ ] frontend: `cd souldrive-ui; npm run test:learning; npm run build`
- [ ] tauri: `cd souldrive-ui/src-tauri; cargo check`
- [ ] diff/status review: `git diff --stat`; `git status --short`

## Plan Self-Review

- 没有把 skill 简化成仅 prompt 注入；Task 2/4 覆盖 schema、match、execution、log。
- 没有绕过人工审核；Task 1/2 只影响候选和 accept 后资产。
- 没有把 learning asset 混进文档证据；Task 3/4 在 RAG prompt 测试中验证边界。
- 没有引入自动无人值守 cron；只保留 future boundary。
