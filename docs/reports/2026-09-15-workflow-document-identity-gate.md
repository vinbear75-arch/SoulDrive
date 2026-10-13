# 研究窗口文档身份闸门优化

日期：2026-09-15  
范围：论文精读、实验对比、会议窗口的文档选择验证

## 发现的真实缺口

PPT 和普通文档预览已经开始使用显式对象 ID，但研究窗口的 `_validate_workflow_document_inputs()` 仍把文档库压成 `name -> item` 字典。两个同名对象在 name-only 请求中会被后一个条目覆盖，旧客户端因此可能“验证通过”并让检索层自行猜测对象。

同时，当请求同时携带 `document_names` 与 `document_ids` 时，旧逻辑只验证 ID，不验证显示标签是否对应该对象。标签 A 与对象 B 可以返回 ready，之后 RAG 的名称和 ID 交集过滤才出现空证据，错误位置太晚且不清晰。

## 外部实践对照

- [Microsoft GraphRAG 输入对象 schema](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 将稳定 `id`、标题和文本分开，ID 作为跨 chunk 的来源锚点。
- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 要求 TextUnit 能回指 Document，派生检索不能靠标题猜来源。
- [Haystack 元数据过滤](https://docs.haystack.deepset.ai/docs/metadata-filtering) 把过滤范围放在结构化 metadata 中；本地对应为先按对象 ID/唯一名称验证，再进入工作流检索。

本轮没有扩大 API 到稀疏 ID；兼容旧客户端的 name-only 路径仍存在，但遇到真正同名对象会 fail-closed，要求刷新选择并使用 ID。

## 失败优先验证

新增两个 API 回归：

1. 同项目两个 `同名论文.pdf`，请求只带 `document_names`；修复前返回 ready，修复后返回“同名文档不唯一”。
2. 文档库只有 `object-a -> 实际文档.pdf`，请求带 `object-a + 伪造标签.pdf`；修复前返回 ready，修复后返回“文档 ID 与名称不匹配”。

## 实现

- `core/mcp_server.py::_validate_workflow_document_inputs`
  - 将名称索引改为一对多列表，不再用字典覆盖同名条目。
  - name-only 命中多个不同库对象时返回明确的 ID 选择提示。
  - 同时提交名称与 ID 时要求数量一致，并逐项验证 basename 与对象展示名一致；数量不一致或标签错配都加入工作流缺失输入。
  - 对显式 ID 的已有路径保持对象 ID 优先，项目/索引状态检查不变。
- `souldrive-ui/src/App.tsx`
  - PPT 证据指标的旧兼容路径显示“来源标签”，只有对象身份路径显示“来源对象”，避免前端重新夸大 legacy name-only 证据。

## 验证证据

- 研究窗口/聊天/学习定向：`163 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1647 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python compileall 与锁定 Cargo 构建通过。
- Vite 仍有既有单 bundle 体积警告，当前产物约 `587.48 kB`。

## 边界

- name-only 的唯一名称兼容路径仍不能提供对象级 provenance；只有 ID 路径才能保证同名对象身份。
- 这轮只验证工作流输入边界，不证明模型回答准确率、召回率、真实 Tauri/WPS UI/E2E、跨主机 exactly-once 或真实 Redis/PostgreSQL。
- RAG 层已支持对象 ID过滤；本轮修复的是更早的“工作流 ready 闸门”，避免错误输入先进入 RAG 再暴露。
