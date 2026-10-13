# 记忆类型投影与上下文治理优化

日期：2026-09-13

## 发现

SoulDrive 已经把长期记忆、可复用 Skill、会话、实验台账和 TaskJournal 分开保存，但学习检索结果此前主要暴露 `asset_type` 和自由文本 `subtype`。因此，未知或历史遗留 subtype 仍可能经过普通检索进入上下文；调用方也不能稳定区分“用户策略”“项目语义事实”和“程序性 Skill”。这与研究窗口需要明确记忆边界的要求不一致。

## 公开实践对照

- [Microsoft Memora](https://github.com/microsoft/Memora) 明确区分 factual、episodic、procedural 等记忆类型，并用结构化抽象帮助去重和检索。本地沿用“类型显式化”的思想，但不新增自动长期记忆提取器。
- [zmem](https://github.com/zerkerlabs/zmem) 将 typed memory 与 authority/policy gate 分开：能检索不等于有权影响当前动作。本地复用这个边界，把类型投影和上下文准入分成两步。
- [projectmem](https://github.com/riponcm/projectmem) 把 issue、attempt、fix、decision、note 保留为追加事件，再生成确定性摘要。本地已有会话、实验台账和 TaskJournal 作为事件事实源，本轮不复制一套新的事件日志。

## 实现

1. 新增 `core/memory_roles.py`，将现有资产确定性投影为：
   - `user_preference -> policy`；
   - `project_fact / workflow_fact -> semantic`；
   - `skill -> procedural`；
   - 其余记忆 subtype -> `unknown`。
2. `LearningStore` 的记忆、历史版本、Skill 和检索结果都返回 `memory_family`。该字段由数据库资产类型和 subtype 重新计算，不信任加密 payload 中可能过期的同名字段。
3. `gate_memory_context()` 在内容相关性、敏感信息和指令注入检查之前增加类型治理：未知 family 不进入 prompt；调用方伪造或篡改 `memory_family` 时返回 `memory_family_mismatch`。已分类的 policy/semantic 记忆保持原有项目、工作流、时间和冲突门。
4. `memory_health()` 对活动记忆和历史版本报告 `unclassified_memory_family`，只输出 ID、subtype 和范围，不回显正文，也不自动迁移存量数据。

## 失败优先与验收

- 新回归第一次运行在收集阶段真实失败：`core.memory_roles` 尚不存在，pytest 报 `ModuleNotFoundError`。
- 实现后记忆角色、读取门、运行时和 LearningStore 专项为 `94 passed`。
- 完整 `scripts/check.ps1` 通过：后端 `1234 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- `episodic` 尚未被伪装成已完成的长期记忆类型：会话、实验台账和 TaskJournal 仍是事件事实源，当前只通过来源追踪参与已确认记忆，尚未新增统一的事件检索投影。
- 未知存量记忆仍可在管理查询中被发现，便于 Doctor/人工审核；它们只被禁止直接注入上下文，不会被静默删除或自动改型。
- 本轮没有引入 Memora 的自动抽取、zmem 的 Merkle receipt、projectmem 的新事件文件，也没有改变向量/GraphRAG 索引；这轮证据证明的是类型与准入边界，不是长期记忆召回率或答案准确率提升。
