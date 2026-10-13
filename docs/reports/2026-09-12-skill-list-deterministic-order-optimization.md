# 可复用技能列表确定性排序优化

日期：2026-09-12

## 结论

`LearningStore.list_skills()` 现在按 `updated_at DESC, skill_id ASC` 返回活动技能。同一更新时间的技能不再依赖 SQLite 插入/返回顺序，学习候选校验、技能管理展示和重启后的读取顺序可复现。

## 外部实践对照

- [Graphiti 的 episode/fact 实现](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py) 将有效时间、摄取时间和实体 UUID 分开处理；其近期关于 [episode 列表排序与 UUID tie-break 的讨论](https://github.com/getzep/graphiti/issues/1724)也明确指出，浏览结果需要可见时间字段和稳定身份末级排序。
- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 用显式 block/archival memory ID 表示记忆对象，避免把列表位置当作身份。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将持久记忆作为可版本化、可重新发现的文件对象；重启或重新读取不应依赖一次进程内的顺序。

这些项目只提供“时间字段 + 稳定身份”的设计参考；SoulDrive 没有因此引入图数据库、Git-backed memory 或新依赖。

## 失败优先验证

新增 `tests/test_learning_store.py::LearningStoreTests::test_list_skills_uses_stable_skill_id_tie_breaker`：逆序写入 `skill-b`、`skill-a`，二者 `updated_at=100.0` 相同。

- 修复前返回 `skill-b, skill-a`，证明结果依赖写入顺序。
- 修复后稳定返回 `skill-a, skill-b`；活动状态、加密载荷绑定、技能执行记录和项目边界没有改变。

## 回归证据

- 学习存储、记忆运行时、记忆门专项：`84 passed`。
- 本轮修改后的全量 `scripts/check.ps1`：后端 `1157 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明技能列表的确定性顺序，不证明技能语义质量、候选选择质量、模型生成质量、分页协议或跨进程 exactly-once；技能内容仍由现有加密存储、项目范围和人工审核链路负责。
