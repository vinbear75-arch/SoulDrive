# 业务种子审核包与误用保护

日期：2026-10-08。状态：用户已批准生成 30 条待审核种子，并确认 30 个场景全部保留；未批准来源许可、gold、指标、划分、冻结、调参、训练或质量结论。结构预检、定向回归及第十轮完整工程检查通过。

## 本次实际产物

`eval/fixtures/business_seed_review_v0.1/` 包含 30 条 `pending_review`、`gold=null` 种子、来源 manifest 和可阅读的审核索引。任务分布为跨文档综合 10、论文评审 8、实验对比 6、记忆冲突 6。

文献问题的主题锚点来自原有四份开发文档的元数据；原始 PDF SHA256 与既有 manifest 一致。未读取 sealed 标签，未修改既有任何冻结样本，未复制或下载新论文。后续已只读核对首面/作者/DOI 等来源标识，详见 `2026-10-08-business-seed-source-review.md`；这不批准许可或答案。逐题正文支持、页码、资料类型及使用许可尚待人工审核，不凭章节标题补 gold。

实验和记忆来自已有仓库合成 fixture，变体明确标注。没有写入真实项目、知识库、记忆、日程或邮件。`review-*` 身份不代表已存在的项目/运行；实验 `%` 和 `0.81` 的业务解释待审核，不自动换成 81%。当前样本来自已暴露开发材料，不能用于独立 held-out 成绩。

## 外部实践与最小落地

[Ragas](https://docs.ragas.io/en/stable/getstarted/rag_testset_generation/) 提供多样化 RAG 任务生成思路；[LongMemEval](https://github.com/xiaowu0162/LongMemEval) 明确包含知识更新、时间推理和跨会话等能力。这里仅借鉴任务覆盖，不复制它们的数据、运行其基准或沿用其成绩。

沿用仓库旧多模态评测已使用的 JSON Schema 检查方式，本机 `jsonschema=4.26.0` 已存在；没有安装依赖或引入训练框架。新增只读预检验证 30 条数量、10/8/6/6 分布、schema、唯一 ID、来源绑定、7 个来源文件哈希及全部禁止评分/训练/调参/冻结/外发的策略。

## B0：草案字段改写会绕过边界

最初 draft 使用 `prompt`，原 RAG loader 会因缺少 `question` 拒绝。但反例把同一未审核行补成 `question`、`type`、`expected_sources` 后，loader 忽略 `review_status` 与 schema，接受它为 `EvalRecord`。

生产 loader 修改前反例 **1 failed，10 deselected，0.25 秒**（`DID NOT RAISE ValueError`）。未调用模型或执行评分，反例只读临时 JSONL。

现明确拒绝 `pending_review` 或本 review schema，返回“not approved as evaluation gold”；原冻结集字段、标签、指标、检索与生成逻辑不改。单测用临时合成来源验证哈希/策略，避免把真实 PDF 作为每次工程检查的环境依赖。没有删除或改动原始材料。

## 已验证与失败记录

- 实际只读预检退出 0：30 条、7 个来源哈希匹配，`scoring_performed=false`、`ready_for_freeze=false`。
- 初版防误用 10 项通过；加入字段改写保护与可移植测试后，审核包/评测资产/模型身份/审计隔离/安全检索组合 **97 passed，36.54 秒**。
- 第九轮完整工程检查不是成功：**1 failed、1707 passed、1 warning，463.76 秒**。唯一失败在未修改的检索测试 `test_project_fallback_when_project_candidates_are_not_relevant` 的临时目录清理：`chroma.sqlite3` 出现 Windows `WinError 32`，后续清理转成 `WinError 267`。工程脚本正确退出 1，后续质量门/前端/Cargo 未继续，不能借先前成绩补作该轮通过。
- 安全检索全组在上述 97 项定向重跑中通过；未修改检索测试、未跳过清理、未删真实索引、未强制终止其他进程。当前同步 Chroma 客户端确有 public `close()`，KB 和测试也已调用；尚不能断言是谁持有句柄或彻底修复了偶发锁。
- 第十轮无真实模型生成并行的完整工程检查退出 0：后端 **1709 passed、1 warning，424.79 秒**；编译、冻结记忆 7/7 场景与 8/8 查询、GraphRAG 路由 4/4、研究窗口角色 6/6、前端构建、8 组 UI 脚本 66 项及锁定 Cargo 检查通过。这是独立完整终态，不覆盖第九轮失败，也不证明偶发文件锁的根因已修复。

第十轮 loader SHA256 `b6c7e527ca49891ca3d648791dda03c2a1849a22752476cf31ad9c64d7d644dd`；种子 SHA256 `ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424`；记录用户场景范围确认后的 manifest SHA256 `752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d`。确认只改变 manifest/README 的审核状态说明，30 条输入、gold=null 和全部禁止使用策略不变。其后 loader 仅提取共用的模型身份读取函数，未改变数据接收与误用保护；最新代码/回归另记于 `2026-10-08-graphrag-runtime-model-identity.md`。

## 仍需人工审核

来源使用许可、逐题正文/页码定位、任务贴合度、完整运行输入与单位、gold/标签全集、指标与 baseline、模板隔离和数据划分。未审核前不执行模型评分、提示词优化、训练或自动扩集。此处结构通过不等于项目所有模块或 GraphRAG 内容质量达标。
