# SoulDrive 任务后学习闭环设计

## 目标

本分支为 SoulDrive 增加一个可审核、可评估、可接入 RAG 的长期学习闭环。系统应从用户对话和任务结果中提取候选经验，经用户人工审核后写入长期 `memory` 或 `skill`，并在后续问答中被检索召回、注入回答上下文，从而让系统在重复使用中变得更贴合用户偏好和项目工作流。

第一版必须完成闭环，而不是只做存储列表：

1. 从当前会话生成候选 `memory` / `skill`。
2. 在用户主动点击“记忆”或关闭前端时集中审核候选。
3. 用户勾选、编辑、拒绝后，只有确认项写入长期资产。
4. 长期资产写入独立 learning index。
5. 后续 `/chat` 同时检索文档证据和 learning 资产。
6. 召回的 `memory` / `skill` 注入回答上下文。
7. 用同一套评估集比较实现前后的学习召回、偏好遵循、skill 应用和延迟开销。

## 参考与取舍

Hermes Agent 的相关思路包括 memory、skills、scheduled tasks、跨会话学习和经验沉淀。SoulDrive 不做通用 agent runtime，也不照搬 Hermes 的系统形态；SoulDrive 的实现必须服从当前产品边界：

- 端侧私有知识引擎优先。
- 可移动工作区和加密存储优先。
- 用户审核和审计链优先。
- RAG 证据可靠性优先。

本设计采用 Hermes 的分层思想，但落成 SoulDrive 的四层模型：

1. `Memory` 层：事实、偏好、项目长期状态。
2. `Skill` 层：SOP、模板、排障流程、坑点。
3. `Reflection` 层：从会话生成候选资产，只能进入待审队列。
4. `Scheduler` 层：周期性任务和自改进建议，第一版只预留接口，不实现自动执行。

参考资料：

- `https://github.com/NousResearch/hermes-agent`
- `https://hermes-agent.nousresearch.com/docs/user-guide/features/memory`
- `https://hermes-agent.nousresearch.com/docs/user-guide/features/skills`
- `https://hermes-agent.nousresearch.com/docs/user-guide/features/cron`

## 非目标

第一版不做以下能力：

- 未经人工审核的自动写入。
- 周期性任务自动执行。
- skill 自动改写自身。
- memory 自动删除、压缩、合并。
- 复杂冲突解决和版本树。
- 把 `memory` / `skill` 伪装成文档证据。
- 远程模型或联网依赖。
- 通用多 agent 编排框架。

这些能力进入第二阶段预留设计，不能阻塞第一版闭环。

## 核心原则

### 人工审核是强制门槛

反思器只能生成候选，不能直接写入长期资产。用户必须能看到候选内容、类型、依据、推荐理由和置信度，并能编辑后确认或拒绝。

### 不打断对话

系统不能在每轮问答后弹窗询问是否保存。候选沉淀只在两个时机出现：

- 用户主动点击“记忆”按钮。
- 用户关闭 Tauri 前端时，若当前会话有可审候选，弹出集中审核面板。

### Memory 和 Skill 分层

`memory` 存“是什么”和“用户偏好”：

- 用户偏好。
- 项目事实。
- 工作流事实。
- 长期状态。

`skill` 存“怎么做”：

- SOP。
- 排障套路。
- 模板。
- 常见坑点。
- 可复用脚本说明。

两者不混写。分类不明确的候选不得写入长期资产。

### 当前指令优先

当前用户指令优先于长期 `memory` 和 `skill`。如果长期资产与当前指令冲突，系统必须遵循当前指令，并可在回答中说明存在长期偏好冲突。

### 文档证据优先

对资料事实类问题，文档 RAG 证据优先于 `memory`。`memory` 可提供用户偏好、项目状态和上下文，但不能替代资料证据。`skill` 可影响工作步骤和输出结构，但不能作为事实证据引用。

## 阶段划分

### 阶段 1：本分支完整实现

本分支完成：

- `LearningStore` 数据模型。
- `Reflection` 候选生成和审核队列。
- 前端手动记忆和关闭前审核。
- `memory` / `skill` 长期写入。
- learning retrieval。
- RAG prompt 注入。
- learning eval baseline 和实现后对比。
- 审计事件。

### 阶段 2：只预留接口

本分支只在模型中预留字段和接口，不实现自动任务：

- `scheduler_tasks` 概念。
- skill 版本字段。
- 自改进候选来源字段。
- 资产健康检查字段。
- 评估退化记录字段。

后续分支可基于这些字段实现周期性任务和经验复利系统。

## 数据模型

新增模块建议命名为 `core/learning_store.py` 和 `core/reflection.py`。

存储位置：

```text
<SoulDrive>/data/learning.sqlite
```

该数据库只在工作区挂载并软件解锁后可读写。敏感正文使用工作区密钥加密。SQLite 明文字段只保留必要的低敏索引字段，例如类型、状态、时间、分数和计数。

### reflection_reviews

一次集中审核任务。

字段：

- `review_id TEXT PRIMARY KEY`
- `trigger TEXT NOT NULL`
  - `manual`
  - `close`
- `status TEXT NOT NULL`
  - `pending`
  - `completed`
  - `discarded`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `candidate_count INTEGER NOT NULL`
- `accepted_count INTEGER NOT NULL`
- `rejected_count INTEGER NOT NULL`

### reflection_candidates

候选资产。

字段：

- `candidate_id TEXT PRIMARY KEY`
- `review_id TEXT NOT NULL`
- `asset_type TEXT NOT NULL`
  - `memory`
  - `skill`
- `subtype TEXT NOT NULL`
  - memory: `user_preference` / `project_fact` / `workflow_fact`
  - skill: `sop` / `troubleshooting` / `template` / `pitfall`
- `status TEXT NOT NULL`
  - `pending`
  - `accepted`
  - `rejected`
- `confidence REAL NOT NULL`
- `selected_by_default INTEGER NOT NULL`
- `duplicate_of TEXT`
- `source_message_indexes TEXT NOT NULL`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `payload_nonce TEXT NOT NULL`
- `payload_ciphertext TEXT NOT NULL`

加密 payload：

- `title`
- `content`
- `evidence`
- `rationale`
- `tags`
- `source_excerpt`

### memories

用户确认后的长期记忆。

字段：

- `memory_id TEXT PRIMARY KEY`
- `subtype TEXT NOT NULL`
- `status TEXT NOT NULL`
  - `active`
  - `archived`
- `source_candidate_id TEXT NOT NULL`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `payload_nonce TEXT NOT NULL`
- `payload_ciphertext TEXT NOT NULL`

加密 payload：

- `title`
- `content`
- `evidence`
- `tags`

### skills

用户确认后的长期技能。

字段：

- `skill_id TEXT PRIMARY KEY`
- `category TEXT NOT NULL`
- `status TEXT NOT NULL`
  - `active`
  - `archived`
- `version INTEGER NOT NULL`
- `source_candidate_id TEXT NOT NULL`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `payload_nonce TEXT NOT NULL`
- `payload_ciphertext TEXT NOT NULL`

加密 payload：

- `title`
- `content`
- `steps`
- `applicability`
- `pitfalls`
- `tags`

### learning_chunks

为后续检索准备的轻量索引表。

字段：

- `chunk_id TEXT PRIMARY KEY`
- `asset_id TEXT NOT NULL`
- `asset_type TEXT NOT NULL`
- `subtype TEXT NOT NULL`
- `status TEXT NOT NULL`
- `text_digest TEXT NOT NULL`
- `embedding_json TEXT`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `payload_nonce TEXT NOT NULL`
- `payload_ciphertext TEXT NOT NULL`

说明：

- 第一版优先复用现有 embedding 能力。
- 若 embedding 模型不可用，可降级为关键词检索。
- 不把 learning chunks 混入现有文档向量库，避免证据边界混乱。

### scheduler_tasks 预留

第一版只保留表结构或模型定义，不暴露自动执行入口。

字段预留：

- `task_id`
- `task_type`
- `schedule`
- `status`
- `last_run_at`
- `created_from_asset_id`

## 反思候选生成

输入来自当前前端会话 messages：

```json
[
  {"role": "user", "content": "..."},
  {"role": "ai", "content": "..."}
]
```

生成规则：

- 用户偏好和事实必须来自用户消息。
- skill 可来自完整对话，但必须带来源消息索引。
- 每条候选必须有依据摘录。
- 没有依据的候选直接丢弃。
- 低置信候选可以进入待审，但默认不勾选。
- 与已有资产重复的候选标记 `duplicate_of`，默认不勾选。
- 模型输出必须通过结构化 JSON 校验。

候选生成可以使用本地 LLM，但 LLM 输出只作为草稿。最终写入内容以用户审核提交为准。

## API 设计

所有接口沿用现有 FastAPI sidecar 风格，非公开接口要求 `X-SoulDrive-Token`。

### POST /reflection/draft

创建一次待审反思。

请求：

```json
{
  "trigger": "manual",
  "messages": [
    {"role": "user", "content": "用户不希望每轮问答后都弹窗保存记忆"},
    {"role": "ai", "content": "已记录为设计约束"}
  ]
}
```

响应：

```json
{
  "review_id": "rev_...",
  "status": "pending",
  "candidates": [
    {
      "candidate_id": "cand_...",
      "asset_type": "memory",
      "subtype": "user_preference",
      "title": "关闭前集中审核记忆",
      "content": "用户希望在关闭程序前集中审核候选记忆，而不是每轮问答后弹窗。",
      "evidence": ["用户明确要求不要每次提问后询问是否保存记忆。"],
      "rationale": "这是长期交互偏好，应作为 memory 召回。",
      "confidence": 0.92,
      "selected_by_default": true
    }
  ]
}
```

失败：

- `423`：工作区未挂载或未解锁。
- `503`：本地模型不可用，且无法降级生成候选。
- `400`：messages 为空或格式不合法。

### GET /reflection/reviews/pending

返回未完成审核。

### GET /reflection/reviews/{review_id}

返回审核详情。

### POST /reflection/reviews/{review_id}/decide

提交用户决策。

请求：

```json
{
  "decisions": [
    {
      "candidate_id": "cand_...",
      "action": "accept",
      "edited_title": "关闭前集中审核记忆",
      "edited_content": "用户希望在关闭程序前集中审核候选记忆，而不是每轮问答后弹窗。"
    },
    {
      "candidate_id": "cand_...",
      "action": "reject"
    }
  ]
}
```

响应：

```json
{
  "review_id": "rev_...",
  "status": "completed",
  "accepted_count": 1,
  "rejected_count": 1
}
```

行为：

- `accept` 写入 `memories` 或 `skills`。
- `accept` 同步写入 `learning_chunks`。
- `reject` 不写长期资产。
- 用户编辑内容优先于候选原文。
- 决策写入审计日志。

### GET /learning/memories

返回已确认 memory 列表。

### GET /learning/skills

返回已确认 skill 列表。

### POST /learning/search

供测试和调试使用，RAG 内部也可复用同一检索函数。

请求：

```json
{
  "query": "以后什么时候询问我是否保存记忆？",
  "top_k_memory": 3,
  "top_k_skill": 2
}
```

响应：

```json
{
  "memories": [
    {
      "memory_id": "mem_...",
      "subtype": "user_preference",
      "title": "关闭前集中审核记忆",
      "content": "用户希望在关闭程序前集中审核候选记忆，而不是每轮问答后弹窗。",
      "score": 0.87
    }
  ],
  "skills": []
}
```

## 前端交互

### 手动记忆

主界面增加“记忆”按钮。

流程：

1. 用户点击“记忆”。
2. 前端将当前 `messages` 发给 `/reflection/draft`。
3. 若无候选，提示“本轮没有可沉淀内容”。
4. 若有候选，打开审核面板。
5. 用户可勾选、编辑、拒绝。
6. 点击确认后提交 `/reflection/reviews/{review_id}/decide`。
7. 写入成功后显示结果。

### 关闭前审核

需要拦截 Tauri 窗口关闭事件。

流程：

1. 用户关闭窗口。
2. 若当前会话没有消息，直接关闭。
3. 若有消息，调用 `/reflection/draft`，`trigger = close`。
4. 若无候选，直接关闭。
5. 若有候选，弹出审核面板。
6. 用户可选择：
   - `确认记忆并关闭`
   - `不记忆并关闭`
   - `取消关闭`
7. 写入完成后提示“记忆已完成，将自动关闭程序”，然后主动关闭窗口。

说明：

- 关闭前审核不能在后台静默写入。
- 写入失败时允许重试、取消关闭或不记忆关闭。
- 真正关闭后沿用现有 Rust `Drop` 逻辑关闭 sidecar。

## RAG 接入

现有 `/chat` 流程在文档检索后增加 learning 检索：

```text
query
  -> document retrieval
  -> learning retrieval
  -> build answer messages
  -> local generation
  -> citation validation
```

learning 检索不改变文档证据的编号和引用校验。

新增 prompt 区域：

```text
【已确认长期记忆】
- 用户偏好：...
- 项目事实：...

【已确认可复用技能】
- SOP：...
- 排障流程：...
```

系统提示新增约束：

- 长期记忆和技能来自用户审核确认。
- 当前用户指令优先于长期记忆和技能。
- 文档证据优先于长期记忆中的事实性内容。
- 技能只用于指导工作方法、步骤和输出结构。
- 不允许把 memory/skill 当作文档证据引用。
- 如果长期资产与当前问题无关，不要强行使用。

### 检索策略

第一版检索采用保守策略：

- `top_k_memory = 3`
- `top_k_skill = 2`
- memory 和 skill 分开排序。
- 有 embedding 时使用 embedding 相似度。
- embedding 不可用时使用关键词匹配降级。
- 低于阈值的 learning 结果不注入 prompt。

### 回答策略

回答生成时：

- 对用户偏好问题，优先使用相关 memory。
- 对执行方法类问题，优先使用相关 skill。
- 对资料事实问题，仍以文档证据为主。
- 若没有文档证据但有相关 memory/skill，可回答“根据已确认长期记忆/技能”，但不能输出文档引用。

## 评估设计

新增评估集：

```text
eval/learning_loop_v0.1.jsonl
```

样例：

```json
{
  "id": "MEM-01",
  "setup_assets": [
    {
      "asset_type": "memory",
      "subtype": "user_preference",
      "content": "用户偏好：关闭程序前集中审核候选记忆，不希望每轮问答都弹窗。"
    }
  ],
  "query": "以后你应该什么时候询问我是否保存记忆？",
  "expected_keywords": ["关闭", "集中审核", "不在每轮问答后"],
  "forbidden_keywords": ["每次提问后", "立即弹窗"],
  "metric": "preference_following"
}
```

### 指标

1. `learning_recall_at_k`
   - 正确 memory/skill 是否被召回。

2. `preference_following_rate`
   - 回答是否遵守已确认用户偏好。

3. `skill_application_rate`
   - 相似任务是否使用已确认 SOP 或流程。

4. `latency_overhead_ms`
   - 接入 learning retrieval 后相对 baseline 的额外耗时。

### 对比方法

实现前先跑 baseline：

- 不注入 learning retrieval。
- 指标应反映当前系统无法使用长期 memory/skill。

实现后跑同一评估集：

- `learning_recall_at_k` 应明显提升。
- `preference_following_rate` 应明显提升。
- `skill_application_rate` 应明显提升。
- `latency_overhead_ms` 应保持可接受。

评估脚本建议命名：

```text
scripts/eval_learning_loop.py
```

## 审计与安全

新增审计事件：

- `reflection.draft_created`
- `reflection.draft_failed`
- `reflection.review_completed`
- `learning.memory_created`
- `learning.skill_created`
- `learning.search_performed`
- `learning.injected_into_rag`

审计 payload 必须避免泄露完整正文，可记录：

- 类型。
- 数量。
- 哈希摘要。
- 置信度。
- trace_id。
- 是否注入 RAG。

安全边界：

- 工作区未解锁时不能 draft、decide、search。
- 反思候选和长期资产正文加密。
- learning 资产不能被工具接口绕过审核写入。
- 关闭前审核失败不能导致静默写入。
- 当前指令覆盖长期资产。

## 测试要求

后端测试：

1. 候选生成结果通过 schema 校验。
2. 无依据候选不会进入 pending。
3. pending 不会直接进入长期表。
4. accept 后写入对应 memory 或 skill。
5. reject 不写长期资产。
6. 用户编辑内容优先。
7. 未解锁工作区拒绝 draft、decide、search。
8. learning search 能召回相关 memory。
9. learning search 能召回相关 skill。
10. RAG prompt 包含相关 learning context。
11. RAG prompt 不把 learning context 放入文档证据块。
12. 审核和注入动作写入 audit。

前端测试：

1. 手动点击“记忆”打开审核面板。
2. 没有候选时不写入。
3. 勾选并确认后提交 decide。
4. 编辑后提交编辑内容。
5. 关闭窗口时有候选则拦截关闭。
6. 确认记忆后主动关闭。
7. 取消关闭时窗口保持打开。

评估测试：

1. baseline 能运行并输出 learning 指标。
2. 实现后同一评估集能对比指标。
3. 延迟指标输出毫秒值。

## 实施顺序建议

1. 先做 `LearningStore` 和加密读写测试。
2. 再做审核队列和 accept/reject 流程。
3. 再做 learning search。
4. 再接入 RAG prompt。
5. 再做 learning eval baseline 和实现后对比。
6. 最后做前端手动记忆和关闭前审核。

这个顺序能先证明核心数据和检索链路，再补交互。

## 成功标准

本分支完成时应满足：

- 用户可以在前端审核并确认候选 memory/skill。
- 已确认资产持久化在工作区内。
- 后续问答能召回并使用相关 memory/skill。
- 回答不会把 memory/skill 当作文档证据引用。
- learning eval 指标能显示实现前后差异。
- 所有新增后端测试通过。
- 前端构建通过。
- 不自动提交、不合并、不推送 main。
