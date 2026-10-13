# 记忆并列分数排序优化记录

日期：2026-09-10  
范围：`LearningStore.search` 的记忆 `top_k` 排序

## 外部实现对照

- [Open WebUI memory utilities](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/memory.py) 的记忆结果排序把路径/相关性作为主要顺序，并以 `updated_at` 降序处理同层结果；这与“新修改优先”的记忆管理语义一致。
- [Open WebUI memory router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py) 保留 `created_at`、`updated_at` 元数据，并先做相关性阈值过滤；时间元数据是结果解释和排序的组成部分。
- [Letta archival memory search](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py) 将相似度作为主排序契约，同时返回记忆 ID、时间戳和内容，便于调用方解释和继续裁决。

本项目当前没有为长期记忆额外加载语义模型；因此本轮只修复已有关键词分数相同场景下的确定性次序，不把词法排序冒充语义召回提升。

## 失败回归与根因

新增 `LearningStoreTests.test_memory_search_prefers_newer_memory_on_score_tie`：创建两条相同关键词得分的记忆，再更新较早记忆使其成为最新版本，并将 `top_k_memory` 限为 1。修复前返回未更新的旧记忆，说明旧记录会先消耗有限的候选窗口。

根因是 `LearningStore.search` 使用 `(-score, updated_at, chunk_id)` 排序，第二项为升序；分数相同时旧时间戳优先。

## 最小修复

排序现在是：

1. 相关性分数降序；
2. `updated_at` 降序；
3. `chunk_id` 升序作为稳定兜底。

这不会改变项目范围、时间有效性、冲突裁决或 `memory_context` 门控，只改变候选窗口内的同分 tie-break。

## 验证

- 修复前新增回归失败，实际返回了另一条较旧记忆。
- 修复后 `tests/test_learning_store.py`：`51 passed`。
- 本轮仓库全量 `scripts/check.ps1`：后端 `1074 passed, 1 warning`，前端生产构建与 7 组 UI 测试 `48 passed`，Tauri `cargo check --locked` 通过，退出码为 0。

## 边界

该修复不证明同义词/跨语言语义召回，也不证明回答正确率；若要引入向量记忆或 reranker，必须先冻结记忆检索评测集并测量召回、无关记忆拒答、延迟和本机显存成本。
