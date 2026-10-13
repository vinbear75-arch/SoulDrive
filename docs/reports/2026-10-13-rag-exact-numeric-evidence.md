# RAG 数值证据：精确等值与前导小数

日期：2026-10-13。轮次：46。当前状态：反例、交叉与完整工程检查通过，完整输出已核对；不是开放式质量或真实交付完成。

## 外部依据与实际链路

[Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)要求把回答拆成主张并核对上下文支持性；本轮只借鉴这个边界，不运行 Ragas 评分。[Python 3.10 Decimal 文档](https://docs.python.org/3.10/library/decimal.html)说明从字符串构造可保留输入精度，但算术仍受 context 精度和指数范围影响，不能只把 float 改名就认定精确。

已追踪 `validate_answer_citations -> _validate_claim_support -> _fact_tokens -> _normalize_fact_token`，由 RAG 的既有引用门和一次纠错重试使用；文本/视觉/语音引用共享此检查。当前使用 `float` 与 `.12g`，会把不同值合并；数字正则还漏掉前导小数的符号。本轮只修改这一共享根因，不改生成提示词、阈值、检索配置或实验统计方法。

只读复现输出：

| 固定输入对 | 当前归一化结果 | 缺口 |
| --- | --- | --- |
| 1000000000001 / 1000000000002 | 同为 1e+12 | 有效位丢失 |
| 0.10000000000001 / 0.10000000000002 | 同为 0.1 | 小数有效位丢失 |
| 1e-400 / 2e-400 | 同为 0 | float 下溢 |
| 1e400 / 2e400 | 同为 inf | float 上溢 |
| -.5% / +.5% | 同为 0.05 | 符号与小数点丢失 |
| -.5% / -0.5% | 0.05 / -0.005 | 等价值被误拒绝 |

## 在实现前固定的验收条件

1. 相邻大整数、高精度小数、上下溢数量、不同符号及单位换算后的不同值不得互相支持。
2. 前导小数、科学计数法、百分比、时间/存储单位和尾零等价应继续被识别；不引入近似容差或改指标口径。
3. 保留字符串型事实 token/缺失项输出；不能把 Decimal 直接放入 JSON，也不能按指数展开海量零。
4. 数值处理不修改调用方全局 decimal context；极端不可表示字面量保留原样，不能合并成零/无穷，也不能让生成流程抛错。
5. 实际 RAG 流须证明：错误首答不外发、仅执行既有一次重试、正确次答通过并留原有审计原因。
6. 定向与交叉回归通过后跑完整 `scripts/check.ps1`，保存完整输出与代码指纹。小型工程门不当作开放式模型准确率提升或真实桌面/E2E 完成。

对照：B0 为当前脏工作区的已记录文件指纹与新固定反例；B1/Candidate 使用标准库的精确十进制及足够的局部算术精度，不新增依赖。B2 的通用数学蕴含/自由 Agent 框架不适用于本确定性缺陷，不为凑阶段部署。没有真实 LLM、GPU 推理、训练随机种子或新业务标签。

B0 指纹：`core/answer_quality.py` = `b5cbc60d37ae53164c05a124f01e6042625099d1cc10eb09840f2f5296ea7d32`；原测试 `tests/test_answer_quality.py` = `929f0bc017a9f611753b03c6187f7453e57609d4c426e66ed0f2687346e1e221`；`tests/test_rag_prompting.py` = `9c8cb8c4770eb74fc36f259172339228bd9cf44321266963a68d0bb17dc5c066`。保留既有修改，不 reset/clean。

原 30 条 seeds/manifest 和四条独立标签草案保持原指纹：`ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424` / `752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d` / `0052d2576abd896bc358dca130f7ee69f6ed9cc1ac383eb14218f21908ec84a7`。它们不进入本轮评分、训练或调参。

## 执行记录

| 阶段 | 结果与日志 |
| --- | --- |
| B0 新反例 | `3 failed, 1 passed, 178 deselected`，0.36 秒，退出 1；`round46-numeric-b0-20261013.log` 保存当前实现误放行错误大整数、误拒绝前导小数等价及实际 RAG 首答错误 |
| 首版 Candidate | `4 passed, 178 deselected`，0.10 秒；`round46-numeric-candidate1-20261013.log` |
| 首版补查暴露缺陷 | 极端指数构造位于 localcontext 外，污染调用方 InvalidOperation flag；`1 failed, 52 deselected`，0.22 秒，退出 1；`round46-context-candidate1-20261013.log` |
| 最终 Candidate | 将构造也移入局部 context，强制 Inexact 触发保守回退，并处理禁用异常 trap 时的非有限构造；`4 passed, 178 deselected`，0.12 秒，退出 0；`round46-numeric-candidate2-20261013.log` |
| 交叉回归 | answer quality、RAG prompting、GraphRAG acceptance、eval assets、窗口角色与记忆质量：`260 passed`，5.30 秒，退出 0；`round46-numeric-cross-20261013.log` |
| 完整工程检查 | 退出 0；后端 `1783 passed, 1 warning`，452.71 秒；日志 `build/quality-gates/check-round46-20261013-115237.log` |

上述短日志均位于 `build/quality-gates/`。B0 的 unittest 循环在首个失败处停止，不把未执行的后续子例称为已复现；初始只读输出及最终全通过的循环分别提供其他边界证据。交叉回归后再加入“缺失数值 token 保持短字符串”的断言并重跑最终四项；完整检查包含该断言。

最终实现仅改共享数字正则与归一化：从原始字符串构造 Decimal；局部 precision 取两个有限十进制系数的位数之和，足够保证乘法不丢位；使用运行时允许的指数边界，不依赖调用方设置，且不按指数展开零。`.normalize()` 只在足够精度的局部 context 内使用；超出实现能力或 inexact 时返回原字面量，不压成零/无穷。缺失项仍为字符串，已验证 `json.dumps(..., allow_nan=False)`；没有改变向量/图检索、LLM 参数、阈值、提示词或实验账本统计。

当前固定反例覆盖大整数、小数超过 28 位、正负前导小数、常规与极端指数、百分比、时间、存储换算、正负零等价，以及调用方 strict/disabled traps 的状态隔离。实际 RAG 流用固定知识库和固定两次模型返回验证四组错误首答被拦截，恰有一次原有审计重试；这不是真实模型准确率分数。

完整日志已实际读取核对：1086 行、36871 bytes，SHA-256 `0516f43fcfe8b4879ae8579f79cb63c8cdd06d42e974e79f4201cc354c2511d0`；包含后端结果、前端 8 组共 66 项通过、Vite build 和最终 Cargo 完成。compileall、现有记忆 `7/7` 场景/`8/8` 查询（Recall@K=1.0、陈旧/禁止泄漏率=0）、GraphRAG route `4/4`、窗口角色 `6/6`、episodic runtime `5/5` 全部通过；四份结果 JSON 已重读。现存 Starlette/httpx 弃用和 Vite chunk 体积警告未隐藏。

运行条件：Windows 本机，`souldrive` Conda，Python 3.10.20、pytest 7.4.4、UTF-8，实际 shell PowerShell 7.6.5；GPU 实测 RTX 5060 Laptop、8151 MiB、驱动 616.92。本轮固定反例不使用真实模型/GPU推理，不引入随机训练、容差或新指标。复现为 `conda run --no-capture-output -n souldrive python -m pytest -q tests/test_answer_quality.py tests/test_rag_prompting.py -k "exact_numeric or callers_decimal_context"`，再按项目入口运行完整检查。

| 最终文件 | SHA-256 |
| --- | --- |
| core/answer_quality.py | `809d14b10776dc5814ad8d61a3d26d6fce345e9ad3fbaba02815f2b78e8378cb` |
| tests/test_answer_quality.py | `fe152f213b7627e63e9e01de89c5fbaf2dc38e95458a8b0a18b346299831677e` |
| tests/test_rag_prompting.py | `6a62a65259844f5d19be8741cbf24d43452500c2e9b27f99eb11c94688813357` |

上述三份指纹在完整检查进行中记录、结束后复核保持一致；原种子、manifest 和独立标签草案的指纹也保持上述原值。只读发行审计另见 `2026-10-13-release-payload-readonly-audit.md`：现存 sidecar 内嵌代码不含当前数字/语义引用检查，不能将本轮源码回归成绩用于旧包。

## 保留边界

不是通用自然语言蕴含、因果或数学表达式检查；逗号分组、复杂区间、日期、推导值和单位语义仍需另外明确。只使用明确数字与现有单位换算，不改实验账本的数值类型或统计方法。真实 Tauri、WPS视觉、安装包及业务标签审核仍未闭合，goal 保持 active。
