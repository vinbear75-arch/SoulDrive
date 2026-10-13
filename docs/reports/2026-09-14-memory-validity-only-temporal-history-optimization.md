# 记忆仅有效期编辑的时间历史优化

日期：2026-09-14

## 结论

本轮修复了记忆时间线的一个缺口：`update_memory()` 只修改 `valid_from/invalid_at`、正文不变时，原实现不会写入 `memory_versions`。把一条当前事实推迟到未来、或提前结束其有效期后，修改前时间点的查询可能错误地查不到原事实。

现在，有效期发生变化时也会按旧 `record_version` 保留可回看的历史区间；若新边界早于原记录的有效起点，则不写入无效区间。没有显式 `valid_from` 的旧记录在这条“仅有效期编辑”路径中以原 `created_at` 作为历史起点，避免把记忆虚构到创建之前。正文编辑的既有历史规则保持不变。

## 外部实践对齐

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 采用事件的 reference time 与摄取时间分离的双时间思路；本项目沿用已有 `valid_from/invalid_at` 与 `memory_versions`，本轮补齐有效期元数据编辑也要产生历史投影的规则。
- [Mem0 memory operations](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx) 将 expiration date 纳入记忆生命周期并在搜索时隐藏过期项；本项目保持本地时间区间模型，同时保证生命周期修改不会抹掉修改前的可追溯区间。

## 实现边界

1. 本轮只处理本地 SQLite 事实源及其历史版本，不引入图数据库或外部时间服务。
2. `valid_from/invalid_at` 仍是显式时间语义；没有证据时不会由模型推断日期，当前接口的异常区间仍 fail-closed。
3. 这不等于自动时间抽取质量、跨模态长期记忆、真实 Redis/PostgreSQL 联调、跨存储 exactly-once 或真实 Tauri/WPS UI/E2E 已完成。

## 回归证据

- 失败优先：正文不变、将 `valid_from` 推迟到未来后，修复前旧时间点返回空；修复后返回 `record_version=1` 的历史事实。
- 时间/历史定向回归：`5 passed, 1 warning`。
- 记忆质量评测：`7/7 scenarios`、`8/8 queries`、召回 `1.0`、过期/越界泄漏 `0`。
- 完整工程门禁：后端 `1521 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。前端仍有既有单 chunk 大小警告。
