# PPT 混合来源对象身份边界优化

日期：2026-09-15  
范围：PPT 前端来源选择、对象 ID 传递、普通文件兼容路径

## 发现的真实缺口

PPT 来源选择同时支持两类记录：安全对象带 `document_id`，旧的普通文件可能只有展示名。此前 `generatePresentation()` 只有在所有记录都带 ID 时才发送 `source_document_ids`；混合选择会把 ID 列表整体置空，再按文件名提交。

这种请求在文件名唯一时可能“成功”，但安全对象已经失去精确身份；在同名时又可能直到后端才被拒绝。它会让界面看起来可以生成，实际却把对象级 provenance 降级为展示名级 provenance。

## 外部实践对照

- [Microsoft GraphRAG 输入对象契约](https://github.com/microsoft/graphrag/blob/main/docs/index/inputs.md) 将稳定 `id`、`title` 和文本分开；ID 是下游 provenance 的锚点，标题不是 ID 的替代品。
- [Microsoft GraphRAG 默认数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 让 text unit 回指原始 Document，以便查询结果能够回到原始来源。
- [Haystack Document 元数据过滤](https://docs.haystack.deepset.ai/docs/metadata-filtering) 将范围/身份放在结构化 metadata 中，而不是依赖显示名称猜测。

本轮没有引入稀疏 ID 新协议。对于当前单用户小项目，后端兼容旧客户端而前端直接拒绝混合选择，边界更窄、回归成本更低，也不会让空字符串占位符成为新的身份语义。

## 失败优先验证

新增 UI 静态失败优先回归，要求提交函数同时具备“至少一个有 ID”和“至少一个无 ID”的检测，以及明确的中文提示。

- 修复前：`test:document-scope` 为 `29 passed, 1 failed`，混合身份保护不存在。
- 修复后：混合选择在发起 HTTP 请求前返回，并保留界面提示；`test:document-scope` 为 `30 passed`。

## 实现

- `souldrive-ui/src/App.tsx`
  - `generatePresentation()` 在生成 `request_id` 和发起请求前，检查 `hasIdentifiedPresentationSource` 与 `hasLegacyPresentationSource`。
  - 两者同时存在时 fail-closed，提示“安全对象与普通文件不能混合生成 PPT”，不发送降级的文件名请求。
  - 全部来源有 ID 时继续发送标签与 ID 的一一对应列表；全部来源无 ID 时保留兼容路径。
- `souldrive-ui/scripts/document-scope.test.mjs`
  - 固定对象身份和旧文件名路径不能静默混用。

## 验证证据

- UI 文档范围回归：`30 passed`。
- 完整 `scripts/check.ps1`：后端 `1645 passed, 1 warning`；前端生产构建通过；UI 脚本合计 `66 passed`；Python compileall 与锁定 Cargo 构建通过。
- Vite 仍有既有单 bundle 体积警告，当前产物约 `587.34 kB`。
- 这轮沿用上一轮刚重跑的冻结评测；该 UI 边界不改 RAG、记忆或 GraphRAG 计算路径。冻结结果仍为记忆 `7/7`、GraphRAG 路由 `4/4`、研究窗口角色 `6/6`、episodic `5/5`。

## 边界

- 这是提交前的确定性边界，不是实际 Tauri 点击流或 WPS 行为证明；真实 UI/E2E 仍需单独执行。
- 普通文件仍允许走文件名兼容路径，但没有对象级 ID 时不能宣称对象级 provenance。
- 如果未来确需混选，下一步应先设计显式的稀疏对象引用契约（每个显示标签都要有可验证的身份或明确的 legacy 标记），再补 API、工具 schema、服务端和迁移回归；当前不把它偷偷扩大成新协议。
