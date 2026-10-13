# SoulDrive 全局 PPT 证据项目边界与来源溯源守卫

日期：2026-09-15

## 结论

本轮修复了一个可复现的跨项目证据旁路：当旧父文档索引缺少 `project_id`，但项目目录已经证明某个文件名属于项目二时，全局 PPT 入口仍可能仅凭文件名读取该父索引内容，生成一份状态合法但含有项目二正文的 PPT。

现在 `collect_indexed_evidence()` 即使在 `project_id` 为空的全局入口，也会查询项目目录的文件绑定。若选中文件已被绑定到项目而索引元数据没有明确项目身份，证据被拒绝；只有没有项目绑定的真正全局资料仍可被全局 PPT 使用。项目入口原有的同项目匹配、版本匹配、父子元数据冲突和外国项目拒绝逻辑保持不变。

## 外部实践对照

- [Microsoft GraphRAG dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/) 将 TextUnit 与知识项关联，保留从派生结构回到原始文本的 provenance；本轮把 SoulDrive 的 `project_id`、`asset_version_id`、`document_id` 等身份视为证据准入条件，而不是展示字段。
- [Microsoft GraphRAG repository](https://github.com/microsoft/graphrag) 将索引视为从原始语料派生的结构化数据管线；本轮因此以项目目录/资产版本为权威边界，旧父索引不能反向声明自己属于全局。

没有引入新的图数据库或自动迁移旧索引；缺失身份时选择拒绝并要求重新索引，避免用文件名猜测归属。

## 失败优先验证

新增 `test_global_presentation_rejects_unscoped_parent_for_catalog_bound_project_file`：建立项目二的资产版本 `p2-secret.pdf`，写入缺失 `project_id` 的旧父索引，再从全局入口按文件名收集证据。修复前该测试确实返回项目二正文；修复后返回空证据集。

## 实现与验证

- `core/presentation_service.py`：全局入口也读取项目目录文件绑定；对已绑定文件，缺少明确项目身份的旧元数据 fail-closed。
- `tests/test_presentation_service.py`：新增全局旧索引负向回归，并保留项目隔离、同名文件、父子 provenance 冲突和来源版本过滤回归。
- PPT/证据服务定向回归：`35 passed`；与学习/工作流相关的交叉回归为 `53 passed, 112 deselected, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1630 passed, 1 warning`；前端生产构建通过，8 组 UI 脚本合计 `62 passed`；Python compileall 与锁定 Cargo 检查通过。Vite 保留既有单 bundle 体积警告（约 `585.53 kB`）。
- 冻结记忆质量、GraphRAG 路由、episodic workflow routing 与研究窗口角色评测分别为 `7/7`、`4/4`、`5/5`、`6/6`；真实本地 GraphRAG acceptance 的关键路由、索引、引用和来源多样性验收项全部为 `true`。全局查询仍是受控多来源回退，不能据此宣称模型跨文档综合质量已达标。原始结果：[graphrag_acceptance_after_presentation_scope_guard_20260915.json](../../eval/results/graphrag_acceptance_after_presentation_scope_guard_20260915.json)。

## 边界

- 这是证据收集边界修复，不等于已自动修复所有历史索引；旧索引需要重新索引或补齐可信来源身份。
- 仅凭文件名无法证明同名文件是同一资产；全局入口宁可拒绝项目目录已绑定但元数据缺失的文件。
- 不证明 PPT 内容事实准确率、WPS 真 UI/E2E、跨主机 exactly-once、真实 Redis/PostgreSQL 或大规模 provenance 完整率。
