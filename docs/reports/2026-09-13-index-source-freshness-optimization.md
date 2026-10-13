# 文档库来源新鲜度提示优化

日期：2026-09-13

## 发现

索引器本身已经用文件 hash 跳过未变化的普通文档，但文档库接口只依据“历史上是否出现过索引记录”返回 `indexed`。用户在索引完成后直接修改 `papers` 下的普通文件时，UI 仍显示“已索引”，旧内容还可能被加入当前上下文；系统没有把“需要重新索引”与“没有索引”区分开。

## 公开实践对照

- [Open WebUI Knowledge Base Sync](https://github.com/open-webui/docs/blob/main/docs/ecosystem/knowledge-base-sync/index.md) 以路径、文件名和 SHA-256 checksum 形成只读 manifest diff，只对新增/修改/删除文件执行后续处理，未变化文件不重复上传或嵌入。
- SoulDrive 当前只采用其中与本机单用户模式直接对应的部分：读取现有索引元数据中的 `document_hash`，对可直接编辑的普通工作区文件做只读当前 hash 比对；不引入后台 daemon、远程 connector 或自动删除。

## 实现

1. `core/mcp_server.py` 增加索引来源 hash 汇总和普通文件当前 hash 读取，不写入索引、不修改项目目录。
2. 文档库返回 `index_present`、`source_changed` 与 `index_status=changed`；内容 hash 不一致时 `indexed=false`，防止旧证据继续进入当前上下文；hash 一致时保持 `indexed`。
3. `App.tsx` 与 CSS 将该状态显示为“原文已变化”，并提示用户重新启动索引。安全文档是内容寻址、不可原地编辑的对象，旧/非标准索引 hash 不作为源变化证据，仍保留其原有状态。

## 失败优先与验收

- 旧实现先因缺少 `_indexed_source_hashes` 而无法建立反例；实现后完整回归暴露了安全文档测试使用人为 `hash-1` 的兼容边界，修正为仅对普通可编辑来源做 hash 新鲜度判断。
- 文档库与索引接口组合 `35 passed, 1 warning`；来源变化反例覆盖 hash 漂移、hash 一致、既有失败/处理中状态和加密文档计数。
- 完整 `scripts/check.ps1` 通过：后端 `1250 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 当前是文档库读状态提示，不是 Open WebUI 式完整 manifest diff；不会自动显示删除文件清单，也不会后台自动重建索引。
- 普通源 hash 只表示字节变化，不证明解析结果、切片质量、GraphRAG freshness 或答案质量；重新索引后仍需现有范围/证据校验。
- 安全文档通过内容寻址形成新对象，不支持原地编辑；需要新版本时应重新导入，不能仅凭旧派生 metadata hash 推断变化。
