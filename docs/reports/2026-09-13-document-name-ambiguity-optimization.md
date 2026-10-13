# 文档同名歧义与删除安全优化

日期：2026-09-13

## 发现

加密文档存储按内容摘要去重，因此同一内容重复导入不会重复写入；但不同内容可以保留相同显示名。预览入口已经在发现同名文档时拒绝猜测，删除入口却仍按名称读取第一份并继续清理索引，存在删除错误本体、留下另一份文档或误清理索引的风险。

## 公开实践对照

- [Paperless-ngx FAQ](https://docs.paperless-ngx.com/faq/) 将“完全相同内容的精确重复”和“内容相似”分开处理，并允许用户复核重复文档；本轮借鉴的是精确身份与内容相似不可混淆的边界。
- [Paperless-ngx configuration](https://docs.paperless-ngx.com/configuration/) 说明哈希重复检查属于明确的内容级策略，而不是文件名猜测；SoulDrive 保留内容摘要去重，同时不把显示名当作唯一对象身份。
- [Open WebUI Knowledge Base Sync](https://docs.openwebui.com/ecosystem/knowledge-base-sync/) 的增量同步以内容哈希和 manifest 识别对象；这与 SoulDrive 已有的内容寻址文档存储方向一致，但本轮没有把名称路径扩展成新的索引身份。

## 实现

1. `SecureDocumentStore` 新增按显示名列出对象的只读入口；读取和删除遇到多个同名对象时统一抛出歧义错误，删除不会先删第一条再发现问题。
2. `POST /documents/delete` 在索引清理前检查同名对象数量；多候选直接返回 `409`，不会调用索引删除，也不会删除任何文档。
3. 删除成功路径保存已确认的 `object_id`，后续按对象 ID 删除，避免在索引清理和本体删除之间再次按名称选择不同对象。
4. 原有预览的同名拒答行为保持不变；单一对象删除、加密文件清理和索引清理回归保持通过。

## 失败优先与验收

- 旧实现反例：写入两个同名不同内容的文档后，`read_document_named()` 和 `delete_document()` 会直接选第一份；API 删除也可能在名称级索引清理后删除错误对象。
- 修复后覆盖存储层读取/删除、API 授权删除、索引清理未调用和两份文档均保留等断言；文档存储与文档 API 定向 `16 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1269 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机同名对象的 fail-closed 删除保护，不是文档语义去重、模糊相似度识别或自动合并。
- 当前 API 仍以显示名作为用户入口；遇到同名候选必须先由更高层提供文档 ID 或由用户清理重复项。没有新增隐式“取最新/取第一份”规则。
- 该证据不覆盖真实 Tauri/UI/E2E、并发删除压力、索引与文档跨存储原子事务或 RAG 召回质量。
