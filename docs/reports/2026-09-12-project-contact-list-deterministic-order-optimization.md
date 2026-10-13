# 项目与联系人发现列表确定性优化

日期：2026-09-12

## 结论

项目目录和联系人列表现在都在展示名称/创建时间之后使用稳定唯一 ID 作为末级排序键：项目使用 `project_id`，联系人使用 `contact_id`。联系人解析在匹配分数和名称相同的候选之间也使用 `contact_id` 稳定排序。

## 外部实践对照

- [Temporal visibility schema](https://github.com/temporalio/temporal/blob/main/schema/sqlite/v3/visibility/schema.sql) 在时间字段之后保留 `run_id`，避免可见性列表的并列记录漂移。
- [LangGraph checkpoint history](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx) 用 thread/checkpoint 身份承载恢复与历史顺序，而不是依赖数据库偶然返回顺序。
- [Open WebUI pagination issue](https://github.com/open-webui/open-webui/issues/20846) 记录了仅按非唯一 `updated_at` 排序导致分页重复/漏项的实际问题，并建议追加唯一 ID。

本地只补充查询排序键，没有改变项目、联系人范围过滤或联系人匹配评分。

## 失败优先验证

- `tests/test_mcp_server_projects.py::test_project_catalog_uses_project_id_as_final_tiebreaker`：同创建时间、同名称的 `project-b/project-a` 逆序写入，修复前返回顺序依赖插入，修复后按 ID 升序。
- `tests/test_project_catalog.py::test_contact_lists_use_contact_id_as_final_tiebreaker`：同名称联系人逆序写入，列表和同分解析候选均按 `contact-a/contact-b` 稳定返回。

## 回归证据

- 项目目录与项目 Catalog 组合：`41 passed, 1 warning`。
- 最新完整 `scripts/check.ps1`：后端 `1169 passed, 1 warning`；前端生产构建通过，7 组测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明发现/解析列表顺序可重复，不证明联系人语义解析正确率、项目推荐质量、分页游标或跨存储 exactly-once。
