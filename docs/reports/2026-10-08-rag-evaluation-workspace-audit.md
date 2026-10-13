# RAG 独立评测审计隔离与回答路径记录

日期：2026-10-08。状态：审计隔离、终态记录、真实回放及第五轮工程门完成。

## 实测发现

本轮真实开发集和三路径 GraphRAG 回放指定了独立评测工作区，索引和文档没有修改用户项目，但 RAG 构造未显式传入工作区审计器，实际事件进入应用总 `runtime/audit_log.jsonl`。因此此前“评测完全隔离”需要修正为“索引/语料隔离，审计尚未隔离”。已有应用日志保留，不删除或重写历史审计链。

真实 GraphRAG 回放 `eval/results/graphrag_acceptance_bge_recheck_20261008.json` 的安全/路由标志全部通过，但全局综合退回逐来源证据列表；局部关系回答也走过校验回退。最新三条 trace 的终态分别是 `rag.completed`、`rag.refused_invalid_answer`、`rag.refused_invalid_answer`。失败原因含缺少事实引用，不能把所有回退简单归因于来源数不足。

这说明现有安全门不等于用户任务完成，也不能据“有引用、未拒答”宣称全局综合成功。本轮只增加终态观测，不更改冻结样本、评分公式或验收阈值；新的人工内容金标准或评分定义仍需用户审核。

## 外部实践与验收

参考 [Microsoft GraphRAG 配置模型](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/config/models/graph_rag_config.py) 明确绑定 reporting 与输出目录的做法，以及 [GraphRAG global search](https://github.com/microsoft/graphrag/blob/main/docs/query/global_search.md) 将跨社区综合与普通定位区分。本项目只复用已有 `AuditLogger.for_workspace()`，不安装框架。

- 两个独立评测入口显式绑定指定工作区的审计器。
- 同一临时测试中，共享审计日志与状态文件字节保持不变；评测工作区内新增事件且审计链通过校验。
- 回答路径由本轮 trace 的真实终态事件决定，区分模型回答、校验回退、质量回退和直接摘录；没有事件时保留未知，不根据回答词汇猜测模型执行情况。
- 原安全门的结果和退出语义不变；回答路径只是诊断记录，不新增或放宽质量评分。

## B0

生产修改前，两个评测入口的审计隔离回归 **2 failed，0.26 秒**。共享审计日志确实新增了事件，指定评测目录未建立审计链。测试替换模型/检索构造，使用真实审计文件与状态落盘，且所有测试数据都在临时目录内。

另外，终态记录的 6 个反例在新增逻辑前均失败（**6 failed，6 deselected，0.23 秒**）：四类终态、其他 trace 不可串入、检索成功不可冒充生成成功、同 trace 取最新终态。

## 候选

两个入口显式传入 `AuditLogger.for_workspace()`。GraphRAG 逐条结果新增 `answer_execution`，从实际本轮审计终态记录路径、尝试次数和校验原因；既有安全评分及退出规则未改。审计隔离、模型身份、评测资产与既有 GraphRAG 组合在终态逻辑加入前 **67 passed，1.36 秒**。

## 候选完整验证

候选审计、模型身份、GraphRAG 终态和评测资产组合 **73 passed，1.08 秒**。

真实本地重复回放保存为 `eval/results/graphrag_acceptance_isolated_audit_20261008.json`。三个问题的审计事件确实写入指定评测工作区，逐条终态均为 `validation_fallback`，尝试次数 2、原因为 `answer missing evidence citation`。安全/图路由门仍通过，但内容综合没有达标。前次普通问题能够生成、本次也回退，进一步说明小样本生成路径不稳定，不能选择性只展示一次成功。

第五轮 `scripts/check.ps1` 退出 0：后端 **1695 passed，1 warning，448.96 秒**；冻结记忆 7/7（8/8 查询）、GraphRAG 路由 4/4、研究角色 6/6；前端生产构建和 UI **66 passed**，`compileall` 与锁定 Cargo 检查通过。新建的回答纠正反馈反例在该轮收集之后，未计入这 1695 项。

## 限制

本轮不把审计隔离当成 RAG 内容质量提升，也不证明全局共同结论已能稳定生成。历史默认审计中的评测事件不迁移、不删除，以免破坏既有哈希链；新回放与历史回放的归档位置已经明确区分。
