# PPT 来源名称大小写规范化优化

日期：2026-09-12

## 结论

PPT 证据收集现在按 Windows 文件名的大小写不敏感语义匹配索引来源，并把匹配结果规范化回用户选择的来源名称。请求中的大小写变体也只计为一个来源，避免排序阶段异常或大纲阶段因字典键大小写不同而丢失证据。

## 外部实践对照

- [LlamaIndex BaseNode schema](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 将 node ID、source node 和 metadata 分层；展示字段不应替代稳定的来源身份。
- [LlamaIndex node parser](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/interface.py) 保留父文档到节点的来源关系；SoulDrive 本轮保持父块/版本身份不变，只规范显示名。
- [GraphRAG inputs](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 为输入文档建立稳定 ID，并把 filename/title 作为展示字段；[GraphRAG 的大小写问题记录](https://github.com/microsoft/graphrag/issues/2427) 也说明身份 join 不能在不显式设计的情况下混用大小写敏感与不敏感规则。

## 失败优先验证

新增 `tests/test_presentation_service.py::PresentationServiceTests::test_collect_indexed_evidence_normalizes_case_insensitive_source_name`：索引元数据为 `report.pdf`，请求选择 `REPORT.PDF`；修复前在证据排序阶段抛 `ValueError: 'report.pdf' is not in list`，修复后返回的证据来源名称统一为 `REPORT.PDF`。

同时把请求来源去重改为 `casefold()` 键，保留第一个用户展示名称；项目过滤、版本过滤、父文档回退和证据快照语义不变。

## 回归证据

- PPT 服务与 PPT API 专项：`26 passed, 1 warning`。
- 最新完整 `scripts/check.ps1`：后端 `1167 passed, 1 warning`；前端生产构建通过，7 组测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只修复来源展示名的大小写兼容，不把文件名提升为唯一业务身份；项目、资产和版本仍依赖显式元数据。没有冻结业务集时，不据此宣称召回率或答案质量提升。
