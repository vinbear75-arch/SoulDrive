# PPT 证据项目边界优化

日期：2026-09-12

## 结论

收紧 `collect_indexed_evidence()` 的兼容入口：未提供 `project_id` 时，只允许没有项目绑定的全局材料；带有明确 `project_id` 的项目材料必须在请求中提供匹配项目。这样，两个项目中的同名文件不会再因旧的“仅按文件名生成 PPT”入口被合并到一份有效 PPT 中。

## 外部实践对照

- [Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py) 在按文件名解析前先计算用户可访问的知识库与文件集合；文件名不是访问控制边界。
- [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/) 将业务范围放入 payload filter，在相似度/关键词搜索前限制候选集合；[Qdrant multitenancy](https://qdrant.tech/documentation/tutorials/multiple-partitions/) 进一步要求租户字段参与每次查询。
- [Microsoft GraphRAG input schema](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 为文档建立稳定 ID；文件展示名不能替代数据身份。

SoulDrive 没有引入新的权限框架或向量数据库，只把现有 `project_id` 元数据提升为无项目兼容入口的硬边界；真正的全局材料仍可被项目显式引用。

## 失败优先验证

新增 `tests/test_presentation_service.py::PresentationServiceTests::test_global_presentation_does_not_merge_project_bound_same_filename_evidence`：父文档索引中逆序写入两个项目的同名 `同名报告.pdf`，修复前无项目请求返回两个项目的内部内容，证明 filename-only 入口仍存在串项目风险；修复后返回空集。

保留并通过既有边界回归：

- 显式项目只能读取同项目或全局材料；外部项目材料被拒绝。
- 指定 `source_version_ids` 时只读取所选版本。
- 仅有全局材料时，项目 PPT 仍可使用。
- 向量索引缺失时，父文档索引回退仍保留原有行为。

## 回归证据

- PPT/证据收集专项：`23 passed`。
- 最新完整 `scripts/check.ps1`：后端 `1165 passed, 1 warning`；前端生产构建通过，7 组测试全部通过（合计 `48 passed`）；Tauri `cargo check --locked` 通过。

## 边界

本轮证明的是证据收集入口的项目隔离，不证明权限系统、跨存储事务或外部 exactly-once。无项目上下文下引用项目材料现在会被视为未索引，调用方必须补充项目范围；这属于有意的安全失败。全局材料的“无 `project_id`”约定仍依赖摄取链路正确写入元数据，后续仍应保留项目边界负向评测。
