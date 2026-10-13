# SoulDrive 记忆写入边界复核优化

日期：2026-09-09

## 结论

记忆安全门现在不仅约束模型/反思候选，也约束“人工审核时编辑后的内容”和“已保存记忆的后续编辑”。此前这两个写入入口会绕过候选阶段的敏感内容检查，存在把 API key、密码或其他特权内容重新写入长期记忆的回归路径。

本轮没有引入新的记忆框架，也没有删除已有记忆；只把同一条敏感内容拒绝规则下沉到 `LearningStore` 的两个事实写入边界。

## 外部实践对照

- [Open WebUI memory 文档](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/memory.mdx)把记忆分为用户/上下文两类，提供明确的人工编辑、替换和删除入口，并强调记忆质量依赖模型，不能把自动写入当作无条件可信事实。
- [Open WebUI memory 工具实现](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/memory.py)在内容进入存储/向量化前做内容清洗和非空校验；这说明入口安全不能只依赖提示词或上游调用方。
- [Graphiti 的时间事实模型](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)将事实有效性和来源追溯放在持久化模型中。SoulDrive 沿用“事实层有明确约束”的原则，但当前只做本机加密记忆，不引入 Graphiti 的图数据库与自动冲突推理。

## 复现的真实缺口

1. 一个原本安全的反思候选在审核请求中通过 `edited_content` 改成包含 `API key` 的内容，旧实现仍会创建活动记忆。
2. 一个已经存在的安全记忆通过 `update_memory` 改成包含敏感内容，旧实现仍会更新加密事实层和检索 chunk。

这两条路径都绕过了 `validate_learning_candidates` 的早期写入门；只修候选生成器不能保证 API/存储边界安全。

## 当前改动

- `core/learning_store.py`
  - `update_memory` 在开启事务前重新检查标题和正文的敏感/特权内容。
  - `_insert_memory_asset` 在新记忆进入事实表前再次检查，覆盖审核编辑、运行时记忆和其他内部调用方。
  - 空标题/正文仍由原有非空约束拒绝；事务失败会回滚审核状态，不留下半条记忆。
- `tests/test_learning_store.py`
  - 新增审核编辑绕过和后续更新绕过的回归：敏感内容被拒绝、审核仍可用安全决定重试、已有安全记忆保持不变。

## 验证

- 先写失败回归：旧实现对审核编辑没有抛出错误，测试失败。
- 修复后定向检查：`tests/test_learning_store.py -k "memory_write_safety_is_rechecked_after_review_edit_and_update or accepting_memory_candidate_creates_memory_and_chunk or decide_review_is_idempotent"`：`3 passed`。
- 本轮之前已完成的仓库级检查：后端 `1016 passed, 1 warning`，前端构建与 7 组测试 `44 passed`，Tauri `cargo check --locked` 通过；后续审核草稿项目绑定和自动记忆候选范围修正后的完整门已再次通过，后端 `1019 passed, 1 warning`，前端构建与 `44 passed`、Tauri `cargo check --locked` 均保持通过。

## 未完成边界

- 该门只拒绝当前敏感模式命中的内容，不是完整的 DLP/PII 分类器；误报和未知语言模式仍需专门评测。
- 语义冲突仍采用“候选冲突标记 + 人工审核 + 读取阶段有限择优”，没有自动事实槽位合并或无人工确认的 supersession。
- 本轮没有改变全局/项目范围、版本历史和 request receipt 语义，也没有把记忆当作文档事实证据。
