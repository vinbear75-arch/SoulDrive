# SoulDrive 记忆直接编辑重复守卫优化

日期：2026-09-15

## 结论

本轮修复了记忆写入的一个具体旁路：审核接受路径已经会对同作用域、同类型和同规范化正文做精确去重，但 `LearningStore.update_memory()` 直接编辑活动记忆时没有执行同一检查。用户可以把第二条活动记忆编辑成第一条的完全相同内容，导致 `memory_health()` 报告重复活动记忆。

现在编辑提交前会复用现有精确匹配逻辑，检查作用域、项目、工作流、类型和时间有效区间。若命中另一条活动记忆，编辑以 `ValueError` 拒绝；编辑自身不会误报，已有记录不自动删除、归档或合并。

## 外部实践对照

- [Mem0 How It Works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将写入前的相关记忆查找、事实去重和检索范围作为记忆层的独立边界。
- [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 保留精确文本/hash 去重作为低成本确定性防线；相似事实不应仅凭一个未经评测的阈值静默合并。
- [Graphiti](https://github.com/getzep/graphiti) 将事实的时间有效性与历史来源作为可查询状态；本轮沿用 SoulDrive 已有的 `valid_from`/`invalid_at` 和来源模型，没有引入新的图数据库。

本轮只把已有的确定性去重契约补到“直接编辑”入口，不引入语义相似度合并器或后台自动 consolidator，避免把不同表述但可能有不同证据的事实误合并。

## 失败优先验证

新增 `LearningStoreTests.test_memory_update_rejects_exact_duplicate_of_another_active_memory`：先创建同一项目内两条不同 ID 的活动 `project_fact`，再把第二条编辑为第一条的标题/正文。修复前测试失败于“没有抛出重复错误”，证明这是可复现的写入缺口而不是理论风险。

## 实现与验证

- `core/learning_store.py`：在更新记忆的有效区间校验后构造编辑后的匹配探针，命中另一条活动记忆时 fail-closed；同一 `memory_id` 命中则继续执行原有版本历史、加密 chunk、同步 outbox 和幂等回执流程。
- `tests/test_learning_store.py`：覆盖拒绝重复编辑、保持两条原记录不变和健康检查不再新增 duplicate issue。
- 记忆/会话组合回归：`389 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1626 passed, 1 warning`；前端生产构建通过，UI 脚本 `62 passed`；Python compileall 与锁定 Cargo 检查通过。

## 边界

- 这是同作用域下的精确重复保护，不是语义相似记忆合并，也不证明记忆抽取、矛盾识别或答案事实准确率提升。
- 旧数据库中已经存在的重复活动记忆仍由 `memory_health()` 报告，不自动选择胜者；需要用户确认后单独归档或编辑。
- 不证明 SQLite 与派生向量/归档同步的跨存储 exactly-once、真实 Redis/PostgreSQL 联调、断电恢复或真实 Tauri UI/E2E。
