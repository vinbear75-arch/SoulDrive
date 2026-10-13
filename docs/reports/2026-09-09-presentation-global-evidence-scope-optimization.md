# 项目 PPT 全局证据范围优化记录

日期：2026-09-09  
范围：`PresentationService.collect_indexed_evidence` 的项目/全局/外部项目边界

## 发现

现有代码已经有显式跨项目负向防线：元数据绑定到 `project-two` 的材料不能进入 `project-one` 的 PPT。但同一范围判断把缺少 `project_id` 的文档也拒绝了。文档库将这类文档标记为“全局知识库”，普通项目 RAG 也允许项目读取全局知识，因此项目 PPT 与其余知识链路不一致。

## 外部实践对齐

多租户检索的核心不是按文件名猜测，而是把所有结果绑定到可靠的 payload/metadata 范围并在查询时强制过滤；Qdrant 的多租户指导明确要求每次查询带租户过滤，并强调过滤只是应用层隔离的一部分。[Qdrant multitenancy guidance](https://github.com/qdrant/skills/blob/main/skills/qdrant-multitenancy/SKILL.md)

SoulDrive 的本机单用户语义多了一个合法共享范围：`project_id` 为空代表全局文档。于是本轮采用三值判断：

- 空绑定：允许作为全局共享证据；
- 当前项目绑定：允许；
- 明确其他项目绑定：拒绝。

当调用方同时提供 `source_version_ids` 时，仍要求版本 ID 命中，不能用“全局”绕过版本边界。

## 实现与验证

- `core/presentation_service.py`：`_evidence_matches_scope` 仅在元数据存在且明确属于其他项目时拒绝；保留版本范围校验。
- `tests/test_presentation_service.py`：新增全局材料可被项目 PPT 使用的回归；原有外部项目拒绝回归继续保留。
- 改动前新增回归失败：全局材料返回空证据；实现后通过。
- `tests/test_presentation_service.py tests/test_mcp_server_presentations.py`：`22 passed, 1 warning`。

## 边界

- 这不是跨项目共享开关，也不允许一个显式 foreign `project_id` 被忽略。
- 如果未来索引器把本应项目绑定的材料错误地写成无 `project_id`，它会按全局材料处理；因此索引元数据写入和项目目录绑定仍是独立的安全防线，不能只依赖展示层。
- 本轮只覆盖 PPT 证据收集；真实 WPS、打包启动和成品视觉质量仍不由该专项测试证明。
