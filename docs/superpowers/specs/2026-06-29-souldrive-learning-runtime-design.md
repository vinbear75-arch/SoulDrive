# SoulDrive Learning Runtime 设计

## 目标

本分支不再把学习闭环定义为“候选列表 + 存储 + prompt 注入”。目标是补成一个端侧可审计的 Learning Runtime：从任务后对话中提出 memory/skill 候选，经过用户审核后写入加密工作区；后续回答时能构建常驻 memory capsule、按查询检索相关 memory、匹配可复用 skill、生成受控执行计划并记录运行结果；任务结束后再根据运行日志提出可审核的改进候选。

这套能力参考 Hermes Agent 的思想，但按 SoulDrive 的产品边界重写：Hermes 的 memory 是会注入 prompt 的小型长期事实快照，skills 是按需加载的流程资产，skill 写入可以开启 approval gate，cron 任务也可以绑定技能执行。SoulDrive 必须更保守：长期资产正文加密，未审核内容不得写入长期 memory/skill，文档 RAG 证据仍然优先于 memory 的事实性内容。

## 非目标

- 不做未经用户审核的长期资产写入。
- 不让 skill 直接执行任意系统命令、文件写入或网络访问。
- 不把 memory/skill 伪装成文档证据引用。
- 不在本分支做无人值守 cron 自动执行；但要预留 skill-backed scheduled task 的数据边界。

## 核心链路

1. 对话结束或用户点击“记忆”时，反思器从当前 transcript 生成候选。
2. LearningValidator 绑定证据、过滤幻觉、标记重复/冲突/敏感内容，低置信候选默认不勾选。
3. 用户在前端审核、编辑、勾选、拒绝。
4. accept 后写入加密 memories 或 skills，同时写入 learning_chunks。
5. `/chat` 先构建 learning runtime context：
   - MemoryCapsule：少量高价值、常驻型用户偏好/项目事实。
   - RetrievedMemory：当前查询相关的 memory。
   - MatchedSkill：当前任务相关的结构化 skill。
   - SkillExecution：当 skill 匹配度足够时生成的步骤化执行计划。
6. RAG prompt 明确区分“文档证据”和“学习资产”。事实性回答仍以文档证据为准；偏好和流程类回答可使用 memory/skill。
7. skill 被使用时记录 SkillRunLog。运行结果或用户纠正可以生成新的 skill_update 候选，仍走人工审核。

## 数据模型补充

现有 `reflection_reviews`、`reflection_candidates`、`memories`、`skills`、`learning_chunks` 继续保留。

新增 `skill_runs`：

- `run_id TEXT PRIMARY KEY`
- `skill_id TEXT NOT NULL`
- `status TEXT NOT NULL`: `planned` / `completed` / `failed`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- `payload_nonce TEXT NOT NULL`
- `payload_ciphertext TEXT NOT NULL`

加密 payload：

- `query`
- `matched_score`
- `plan`
- `inputs`
- `outcome`
- `failure_reason`

skill payload 标准化字段：

- `title`
- `content`
- `intent`
- `triggers`
- `required_inputs`
- `applicability`
- `steps`
- `tool_permissions`
- `output_contract`
- `success_criteria`
- `failure_modes`
- `eval_cases`
- `evidence`
- `tags`

## 验证与防幻觉

LearningValidator 在写入 review 前执行：

- memory 的用户偏好、用户事实、项目事实必须来自 user 消息。
- 每条候选必须带 `source_message_indexes`，且 evidence 必须能在对应消息中找到或被对应消息内容覆盖。
- 无 evidence、无 title、无 content、类型不合法的候选直接丢弃。
- 与已有 asset 文本高度相似时标记 `duplicate_of`，默认不勾选。
- 与已有 active memory/skill 冲突时标记 `conflicts_with`，默认不勾选。
- 含 prompt injection、凭证、密钥、要求泄露系统提示等敏感模式时拒绝候选。
- skill 必须能标准化为步骤、触发条件和输出契约；否则只能作为低置信候选进入审核。

## Skill Runtime

Skill 不再只是被注入 prompt 的文本。它进入运行时后有三个对象：

- `SkillRegistry`：从 LearningStore 读取 active skills，返回结构化 skill。
- `SkillMatcher`：根据 query、intent、triggers、steps、tags 匹配候选技能，输出 score 和 reason。
- `SkillExecutor`：把 matched skill 转成受控执行计划。当前分支先执行“guided execution”：生成步骤、输入要求、输出契约、成功标准，不执行外部工具。后续如果增加工具权限，必须由 `tool_permissions` 白名单和用户确认共同控制。

## API 补充

- `POST /learning/skills/match`：返回当前 query 匹配的技能及理由。
- `POST /learning/skills/{skill_id}/runs`：创建一次 guided skill run，返回 `run_id` 和 plan。
- `GET /learning/skills/runs`：查看技能运行日志摘要。

`/chat` 内部也会使用同一 matcher/executor。当匹配到高置信 skill 时，向 RAG 注入 `skill_execution`，并记录 `planned` run。这个日志不是长期知识写入，不绕过人工审核。

## 评估指标

合并前必须能跑同一套评估，至少覆盖：

- `learning_recall_at_k`：memory/skill 是否被召回。
- `memory_capsule_hit_rate`：常驻 memory capsule 是否包含高价值偏好。
- `skill_match_precision`：相似任务是否匹配正确 skill，非相似任务是否不乱匹配。
- `skill_execution_plan_rate`：可执行任务是否生成结构化计划。
- `preference_following_rate`：回答是否遵守已确认偏好。
- `latency_overhead_ms`：learning runtime 相对 baseline 的额外耗时。
- `unreviewed_write_count`：必须为 0。

## 合并门槛

- 新增长期 memory/skill 必须经过人工审核。
- Skill 至少具备结构化 schema、匹配、guided execution、run log，不再只是 prompt 注入。
- RAG prompt 中 learning context 与 document evidence 边界清楚。
- focused backend tests、frontend learning test、build、cargo check 通过。
- 若 full suite 有已知环境失败，必须明确说明原因，不把它当作通过。
- 不自动 commit、merge、rebase、cherry-pick 或 push。
