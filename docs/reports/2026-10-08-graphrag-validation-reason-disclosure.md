# GraphRAG 回退原因与实际校验结果对齐

日期：2026-10-08。状态：真实回放发现误报，B0 回归已复现；候选定向、相同问题回放及第八轮工程检查通过；审核包加入后的最新第十轮完整工程检查通过。

## 真实缺口

`graphrag_generation_probe_candidate2_20261008.json` 的全局终态为 `validation_fallback`，实际原因为 `answer claim missing evidence citation`。但 `global_graph_source_fallback_answer()` 对所有全局校验失败一律说“自动综合未通过来源多样性校验”。缺少逐条引用、引用不存在的证据或语义不支持都可能被误报成来源多样性问题，误导用户排查和补材料。

沿用 [Haystack JsonSchemaValidator](https://docs.haystack.deepset.ai/docs/jsonschemavalidator) 将实际校验错误与反馈对应的机制；本项目只对齐已有程序校验结果和本地提示，不添加框架，不改变验证器。

## B0 与验收

生产修改前，新增真实生成链路回归 **1 failed，110 deselected，0.33 秒**：审计是 `answer missing evidence citation`，用户文本却说来源多样性失败。回归还覆盖未知证据引用与真正来源数不足；保留两次回答预算和多来源摘录。

验收边界：

- 只有实际 `answer citation source diversity insufficient` 才显示来源多样性失败。
- 两种缺引用失败显示逐条引用校验失败，其他失败只显示证据校验失败，不猜测原因。
- 摘录和引用 ID、来源计数、图路由、检索、提示词、评分公式、重试上限和拒答门均不改变。
- 实际校验原因继续保留在审计和 `answer_execution` 中；未知原因不原样回显为用户文本。
- 与工程回归和固定问题回放单独验收，不借用之前成绩，不把更准确的错误提示宣称为内容生成质量提升。

## 候选与结果

已等待第七轮和 16 条开发集完整终态后再修改生产代码：把原验证器原因传给现有回退函数，使用三个已知原因对应的固定中文标签；其他原因只说证据校验失败，不回显原始异常或草稿。来源摘录、引用、验证器和评分公式未改。

RAG/回答质量/GraphRAG/评测资产组合 **225 passed，3.36 秒**，覆盖缺引用、未知证据引用与真正来源不足的提示分支。生产代码 SHA256 `bda0cada29dbcce6985d634fac2ea9ff6b5c234ac67133d844186011f4b7dd00`；提示词测试 SHA256 `ceff97bcdb1af0200f879ddc1a192985bb6005a73f02222e0867cb9b0f13e285`。

`eval/results/graphrag_generation_probe_reason_disclosure_20261008.json` 完整退出 0：安全/路由门通过，实际仍是普通问题 `model_generated`、全局和局部 `validation_fallback`。全局实际原因 `answer claim missing evidence citation`，输出包含“自动综合未通过逐条结论的引用校验”，不再误说来源多样性不足。全局回退引用 4 份来源，来源数门继续通过；综合任务仍未完成。

第八轮 `scripts/check.ps1` 完整退出 0，前端与 Cargo 末端成功。该次保存的 stdout 被长度截断，未保留精确后端计数/耗时，不用预计项数补齐；第八轮不覆盖后来新增的审核包结构测试。第九轮因临时 Chroma 文件锁退出 1；第十轮独立完整检查退出 0，后端 **1709 passed、1 warning，424.79 秒**，编译、三个冻结质量门、前端构建/66 项 UI 脚本和锁定 Cargo 检查通过。失败与未确认的锁根因详见 `2026-10-08-business-seed-review-preflight.md`；重跑成功不把第九轮改记为通过。

这是错误提示透明性修正，不是答案准确率提升、GraphRAG 内容验收达标或真实 UI/E2E。
