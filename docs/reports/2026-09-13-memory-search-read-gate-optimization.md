# 记忆搜索入口读取门优化

日期：2026-09-13

## 发现

`/learning/search` 之前直接返回 `LearningStore.search()` 的记忆结果。聊天运行时会继续调用 `gate_memory_context()`，但搜索入口本身可能被 UI、诊断脚本或未来工具调用方当作可用上下文来源，因此形成“检索结果绕过准入门”的边界：未知 subtype、敏感内容或指令型存量记忆可能先被返回，再由调用方自行决定是否使用。

## 公开实践对照

- [zmem](https://github.com/zerkerlabs/zmem) 明确区分 memory search 与 memory inject，并记录 admitted/withheld 的 policy decision；本地采用同一最小原则，搜索入口也先过本地读取门。
- [LangMem memory tools](https://langchain-ai.github.io/langmem/reference/tools/) 支持 namespace/filter，但“检索到”仍需要结合运行时 namespace；本地沿用已实现的项目、时间和 workflow 过滤，不扩大跨项目范围。

## 实现

1. `/learning/search` 在 `LearningStore.search()` 后复用 `gate_memory_context()`；`top_k_memory` 作为门控的 item 上限，保持请求级预算一致。
2. 返回 `memory_gate` 审计摘要，包含最终 admitted/rejected ID 与原因，不复制额外正文；原有 `skills` 返回结构保持不变。
3. 未分类 family、family 与 subtype 不一致、敏感内容和不可信指令不再从该入口直接返回为记忆上下文；管理端 `/learning/memories` 仍可查看存量元数据并由 Doctor 提示人工处理。

## 失败优先与验收

- 新回归先复现旧行为：fake LearningStore 返回 `legacy_note` 后，旧 endpoint 原样返回该记忆；回归断言失败。
- 实现后学习 API、LearningStore、读取门和记忆角色组合为 `170 passed, 1 warning`。
- 含该修复的完整工程门通过：后端 `1237 passed, 1 warning`；前端生产构建和 7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 读取门只治理 memory，不改变 Skill 计划/执行权限；Skill 仍由已审核状态、匹配和既有执行边界控制。
- 这不是 Merkle receipt、跨进程 exactly-once 或事实真实性证明；它只保证本地搜索入口不会绕过已有的类型、敏感内容、指令注入、相关性和预算门。
