# 本地索引 manifest 只读变更预览优化

日期：2026-09-13

## 发现

来源新鲜度状态已经能标出普通文件变化，但用户仍需要逐项查看文档列表，无法一次知道本次索引会处理哪些新增、修改和删除来源，也无法区分“索引存在但 hash 未验证”的历史记录。

## 公开实践对照

- [Open WebUI Knowledge Base Sync](https://github.com/open-webui/docs/blob/main/docs/features/workspace/knowledge.mdx) 的 `/sync/diff` 接收本地 manifest，返回 added/modified/deleted/unmodified，并明确该 diff 本身是只读操作；后续上传和 cleanup 是分开的步骤。
- SoulDrive 只采用本机可验证的只读部分：从现有本地索引与当前工作区文件生成摘要，不引入远程同步、自动删除或后台 daemon。

## 实现

1. 增加 `GET /index/preview`，返回 `souldrive_index_manifest_diff_v0.1`，包含 `added`、`modified`、`deleted`、`unchanged`、`unverified`、`blocked` 及计数；明确 `read_only=true`。
2. 文档库增加“检查变更”入口和只读摘要，展示最多 6 个变更名称；修改/新增/删除不会自动触发索引。
3. `index_freshness` 区分 `current`、`changed`、`unverified`、`unindexed`；旧安全内容寻址对象不因非标准历史 hash 被误报为变化。

## 失败优先与验收

- 旧实现先因没有 `_index_preview_payload` 而失败；实现后覆盖新增、修改、删除、未变化、未验证和只读 HTTP 入口。
- 索引/项目接口定向 `17 passed, 1 warning`；文档库前端静态测试 `17 passed`，TypeScript 检查通过。
- 完整 `scripts/check.ps1` 通过：后端 `1252 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `50 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机索引的只读变更预览，不是同步执行器；删除来源不会自动清理派生索引，必须由现有索引流程处理并观察结果。
- `unverified` 表示缺少可重验证的当前/历史 hash，不等于内容一定变化；安全内容寻址对象需重新导入形成新版本。
- 预览分类不证明解析、切片、embedding、GraphRAG 或回答质量；真实 Tauri/UI/E2E 和安装包仍未由 `scripts/check.ps1` 覆盖。
