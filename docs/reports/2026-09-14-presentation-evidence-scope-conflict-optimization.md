# PPT 证据范围冲突与旧索引旁路优化

日期：2026-09-14

## 发现

PPT 证据采集同时读取向量子块、父文档索引和项目目录。旧实现存在两个风险：

1. 子块元数据属于项目二、父文档元数据误标为项目一时，简单的 `{child, parent}` 合并会让父记录覆盖真实项目身份；
2. 历史父索引缺少 `project_id` 时，按文件名选择会把“目录已证明属于其他项目”的材料降级成全局材料。

这两种输入都可能让最终 PPT 文件合法生成，但来源边界错误，属于交付前必须拒绝的证据问题。

## 公开实践对照

- [Microsoft GraphRAG BYOG](https://github.com/microsoft/graphrag/blob/main/docs/index/byog.md)：外部/既有数据接入仍需保留可验证的文档与索引身份，不能只用展示名称重建来源。
- [Microsoft GraphRAG 社区回指讨论](https://github.com/microsoft/graphrag/discussions/931)：派生摘要需要回指原始文档集合，来源身份不清时不能把摘要当作可靠证据。
- [LlamaIndex schema metadata](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py)：节点与文档身份元数据是检索和回指契约的一部分。

本轮只复用“来源身份冲突即拒绝、目录身份优先于文件名显示”的原则，没有引入新的图数据库或 Agent 框架。

## 实现

- 合并子块/父文档元数据时，对 `source_filename`、`project_id`、`asset_id`、`asset_version_id`、`document_id`、`document_hash` 和 `catalog_file_ref` 做一致性校验；任一非空身份字段冲突则丢弃该父记录，不让父记录覆盖子块身份。
- 项目范围采集前读取本地 `ProjectCatalog` 的资产版本绑定；若目录证明某文件名已绑定项目，而父索引没有明确且匹配的 `project_id`，该记录不再按 global 证据放行。
- 该门同时作用于向量父记录和 parent-document fallback；因此向量重建暂时落后时不会绕过项目范围门。

## 验收

- 新增失败优先反例：子块项目二/父记录项目一冲突，以及目录绑定项目二但父索引无项目字段；两者均不能进入项目一证据集。
- PPT Service 与 MCP API 回归：`38 passed, 1 warning`。
- 本轮最终完整工程门禁已通过：后端 `1501 passed, 1 warning`；前端生产构建及 8 组脚本测试通过；`compileall` 与 `cargo check --locked` 通过。`git diff --check` 通过（仅有 Windows 换行转换提示）。

## 边界

- 这是本机项目目录、向量父记录和 parent index 之间的确定性身份门，不是身份认证或多用户权限系统。
- 目录存在项目绑定但索引缺少身份字段时选择拒绝，要求重新索引；不会猜测该文件属于哪个项目。
- 仍未证明 Redis/PostgreSQL 实例级联调、跨进程 exactly-once、PPT 内容事实准确率或真实 Tauri/WPS UI/E2E。
