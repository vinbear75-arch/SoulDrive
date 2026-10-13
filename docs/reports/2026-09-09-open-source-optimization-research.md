# SoulDrive 外部开源实践调研与后续优化证据

日期：2026-09-09

本记录只说明外部项目中与当前缺口直接相关的可迁移做法，不表示 SoulDrive 引入这些项目或其依赖。实现仍遵循本机单用户、项目边界和人工审核门。

## 外部实践

### 文件摄取状态与失败可见性

[Open WebUI 的文件 API 文档](https://github.com/open-webui/docs/blob/main/docs/reference/api-endpoints.md)将文件处理建模为异步过程，并公开 `pending`、`completed`、`failed` 状态及错误字段；其[文件处理实现](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/files.py)也会在处理异常时持久化失败状态和错误信息。

SoulDrive 已经在索引器运行时保留逐文档观测和失败分类，因此本轮采用最小映射：文档库接口补充 `index_status`、`index_failure` 和受限的 `index_observation`，前端显示解析/读取/运行/入库阶段及失败原因。没有新增异步队列、目录同步器或第三方框架。

### 项目/租户元数据过滤

[Qdrant 多租户与分区实践](https://qdrant.tech/documentation/tutorials/multiple-partitions/)以及[过滤检索文档](https://qdrant.tech/documentation/search/filtering/)强调将项目/租户元数据作为 payload，并在检索时使用精确过滤。SoulDrive 当前已在父文档、子块和 PPT 证据收集路径中执行 `project_id`/版本过滤，因此本轮不引入 Qdrant；它只作为现有项目边界实现的外部交叉核验。

### 大模型/运行时资源外置

[Ollama 的模型目录配置](https://github.com/ollama/ollama/blob/main/envconfig/config.go)、[Jan 的本地模型目录说明](https://github.com/janhq/jan/blob/main/docs/src/pages/docs/desktop/cli.mdx)和 [ComfyUI Portable 的模型目录约定](https://github.com/heinvoo/comfyanonymous__ComfyUI)都将大型模型/检查点与程序代码分离管理。结合 [NSIS 大安装包限制说明](https://nsis.sourceforge.io/Failure_when_compiling_a_big_installer)和 [Tauri Windows 安装器文档](https://v2.tauri.app/distribute/windows-installer/)，这支持“sidecar 外置或分体分发”作为后续发行方向。

本轮不直接改安装包布局：这会改变升级、路径发现、完整性校验和用户数据迁移，需要产品选择。当前只保留该方向及现有 NSIS/MSI 失败证据，不把它包装成已经解决。

## 本轮已实施

- 后端从现有 `runtime_state.indexing.observability.documents` 和 `failures` 生成文档级只读诊断。
- 文档状态统一投影为 `indexed`、`failed`、`processing`、`pending`；原有 `indexed` 字段保留，兼容已有调用方。
- 失败信息只返回错误码、分类、阶段和截断后的原因；观测信息只返回耗时、切片数、模态、页数、OCR/转换等有限字段，不暴露原始路径或完整错误堆栈。
- 文档库 UI 在每一行显示状态；失败文档显示阶段和原因，索引中文档显示处理中，待索引文档明确提示需要手动启动索引。
- 新增后端四状态及失败观测回归，并新增前端文档诊断展示断言。

## 验证结果

- `tests/test_mcp_server_projects.py`：`16 passed, 1 warning`。
- `souldrive-ui` 生产构建：通过。
- `souldrive-ui` 全部前端测试：通过；新增文档诊断测试已纳入 `test:document-scope`。
- 本轮改动没有改变 RAG 检索算法、项目过滤规则、模型目录布局或真实外部发送行为。

## 仍未闭合的门

- 3.56 GB sidecar 导致 NSIS 构建失败，使用同一资源树的 MSI 旁路也未生成安装包；需明确外置 sidecar、分体包或裁剪运行时的产品方向。
- 真实 Tauri 窗口/WPS 视觉检查仍需人工执行。
- 三项 PNG 当前仍是摄取/证据绑定诊断，不应写成已修复的视觉能力提升。
