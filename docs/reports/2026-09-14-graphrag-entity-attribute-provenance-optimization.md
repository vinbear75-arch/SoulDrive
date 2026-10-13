# SoulDrive GraphRAG 实体属性来源保真优化

日期：2026-09-14

## 本轮结论

本轮修复了图 RAG 中“同名实体的最后一次描述覆盖前一次描述”的来源保真缺口。实体名称仍作为共享图节点，但实体类型和描述不再只有一个无来源的聚合值：新抽取结果会按 `chunk_id` 保存属性来源，项目或 global-only GraphRAG 索引只选择当前范围内有来源的属性。

这解决的是项目范围内的实体属性串写，不等同于实体消歧、实体合并质量或答案准确率提升。

## 外部开源实践对照

- [Microsoft GraphRAG Entity 数据模型](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/data_model/entity.py) 将实体描述与 `text_unit_ids` 一起建模，允许把实体属性回溯到文本单元。
- [Microsoft GraphRAG 增量实体合并](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/index/update/entities.py) 在同名实体合并时聚合描述和文本单元来源，而不是只保留最后写入值。
- [Graphiti MCP 的 group/episode 作用域与来源查询](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 将 group 边界和 episode 产生的图元素作为可查询的来源上下文。

结合 SoulDrive 只面向本机单用户、已有 chunk provenance 和加密图存储的边界，本轮采用来源映射而没有引入 Neo4j、Graphiti 或新的远端服务。

## 失败优先证据

修复前新增用例先失败：

- LocalGraphDB 写入同名实体的项目 A/B 两条描述后，快照没有 `source_attributes`。
- SecureGraphStore 同样没有按 chunk 保存属性来源。
- GraphRAG 项目 A 作用域只能看到“最后写入的类型/描述”，无法选出项目 A 的属性。

## 最小实现

### 存储层

- LocalGraphDB 新增 `entity_sources(entity_name, chunk_id, type, description)` 派生表；写入、更新和删除 chunk 时保持与实体 provenance 同步。
- SecureGraphStore 在加密实体 payload 内增加 `source_attributes`；旧 payload 没有该字段时仍可读取。
- 图快照暴露 `source_attributes`，每条记录都包含 `chunk_id`、`type` 和 `description`。

### GraphRAG 作用域层

- 项目或 global-only 快照优先按当前允许的 chunk 选择属性来源，并按稳定顺序合并同范围内的不同描述。
- 属性来源不完整时清空类型和描述；旧数据没有来源映射时继续沿用 fail-closed 行为。
- 社区 payload 新增有界 `entity_attributes`，结构化社区报告和可选 LLM report generator 均只能看到已经作用域裁剪后的实体属性；不再出现“存储有属性但 GraphRAG 输出丢失属性”的旁路。
- 无范围的普通图快照不改变既有行为，关系结构和 chunk 召回接口保持兼容。

## 验证证据

- 失败优先：LocalGraphDB 2 个新用例、SecureGraphStore 2 个新用例、GraphRAG 1 个新用例均先复现失败，修复后通过。
- 图存储、加密图存储、GraphRAG、图抽取器：`63 passed`。
- 索引作用域、索引 worker、GraphRAG 路由和接受度：`28 passed`。
- Python `compileall`：通过。
- `git diff --check`：通过；输出中的 LF/CRLF 提示为工作区既有换行提示，不是 diff 错误。
- 本轮新增社区报告字段后的仓库级 `scripts/check.ps1` 已通过：后端 `1419 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `61 passed`；Python `compileall` 和锁定 Cargo 通过。既有 Vite bundle 大小提示与 Starlette/httpx 弃用警告仍存在。

## 数据迁移与边界

- 旧 LocalGraphDB 自动创建来源表；旧 SecureGraphStore 加密 payload 没有来源字段时按兼容路径读取，不会凭空推断历史描述属于哪个项目。
- 只有新抽取或重新抽取的实体才具备逐 chunk 属性来源；历史实体需要在正常索引重建/图抽取流程中补齐来源。
- 同名实体仍是共享名称节点，项目过滤依赖 chunk provenance；本轮没有宣称完成实体消歧、别名归一化、跨语言实体解析或关系事实时间有效期。
- 本轮没有增加 Agentic RAG，也没有改变 GraphRAG 的召回路由；应在冻结评测集上另行比较 text-only、当前 GraphRAG 和带属性来源的候选方案，不能从定向单元测试推导答案准确率提升。
- `scripts/check.ps1` 仍不证明真实 Tauri UI、安装包、跨机器迁移或线上部署行为。
