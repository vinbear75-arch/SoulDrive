# 产物入口：大纲确认、完成结果回放与 Skill 显式激活

日期：2026-10-13。状态：定向与三次完整工程检查通过；最终完整输出已归档核对。goal 仍 active，不是全项目完成。

## 缺口与最小实现

继续核查真实可执行入口，而不只检查库函数。`scripts/smoke_artifact_flows.py` 仍假设 `PresentationService.create_task()` 一次运行就得到 `output_path`，而现有服务已是先产生待确认大纲的合同，实际 CLI 因 `KeyError: output_path` 退出 1。另一旧假设把 Skill 审核当成激活；现有入口已要求两阶段操作。

沿用既有服务，不新增产物框架或绕过确认。按 [Temporal 已记录结果回放](https://docs.temporal.io/workflows)与 [DBOS 恢复边界](https://docs.dbos.dev/production/workflow-recovery)复查任务身份和恢复承诺，新增以下冒烟步骤：

1. 建立并运行 plan，读取持久化 `outline_review` 结果，确认此时没有 PPTX。
2. 以实际 `plan_task_id + plan_digest` 确认，运行 generation task，验证真实 OpenXML 文件。
3. 重建服务对象，重复确认得到同一 generation task；再次运行完成任务，比较返回结果与文件哈希均不变。
4. 明确固定本次冒烟为 OpenXML renderer，并在上下文退出后恢复父环境设置；不启动 WPS/COM。
5. 临时合成 Skill 经审核后仍不可发现为 active，显式 activation 后才 active；不执行脚本。邮件只用 mock，未连接真实 SMTP。

## 失败优先与候选过程

| 阶段 | 证据 |
| --- | --- |
| B0 CLI | `KeyError: output_path`，退出 1；plan 尚未确认，不应读取生成结果 |
| B0 新测试 | `tests/test_artifact_smoke.py`：`1 failed`，复现同一断点 |
| 第一候选 | PPT 与邮件通过，但 Skill 仍错把审核结果当 approved/active，CLI 退出 1；保留 `build/quality-gates/artifact-smoke-candidate-20261013.json` 失败报告 |
| 最终候选 | 正确走审核 -> 显式激活；冒烟/PPT服务/PPT接口/TaskJournal/Skill installer 组合 `106 passed, 1 warning`，9.58 秒 |
| 最终 CLI | `artifact-smoke-candidate2-20261013.json`，退出 0，PPT/email/Skill 三分支均 pass |

新增测试故意将父环境 renderer 设为 `com`，并令 socket 连接与原生渲染子进程调用抛错；完整运行仍成功且父环境恢复。测试不允许意外触发真实邮件或本机原生渲染。

实际生成 4 页、22 个 ZIP entry、11730 bytes 的有效 PPTX：`build/quality-gates/artifact-smoke-candidate2-20261013.pptx`；SHA-256 `e2d07bf9864af1c76bbb286c00909c51840fab108db76174b55490e197b78c31`。2/2 证据页被绑定，但该合成单来源 fixture 的身份仍是 `display_name_only`，不能写成精确版本来源或复杂多文档质量证明。

## 完整工程回归

| 检查 | 工具回传结果 | 证据边界 |
| --- | --- | --- |
| 第一次完整检查 | 退出 0；后端 `1778 passed, 1 warning`，499.27 秒 | 启动早于新增冒烟测试，因此不包含该条新测试 |
| 第二次完整检查 | 退出 0；后端 `1779 passed, 1 warning`，453.09 秒 | 包含新增冒烟测试和本轮三个生产修复 |
| 第三次完整输出归档复跑 | 退出 0；后端 `1779 passed, 1 warning`，459.61 秒 | 包含新增冒烟测试；1086 行输出已实际读取核对，前端 8 组 66 项、最终 Cargo 完成均在文件内 |

前两次 `Start-Transcript` 日志只保留了部分头尾信息，原生命令输出未完整落盘；`check-20261013-110054.log` 与 `check-20261013-111526.log` 不是完整原始测试日志。结果由执行工具输出确认，特意再跑带输出捕获的一次以补足可审计文件，不隐藏这个日志缺口。

最终完整日志：`build/quality-gates/check-captured-20261013-112701.log`，1086 行、36870 bytes，SHA-256 `847d6efed1985a9cb16ba71c5246716916ff34cca1a413f9154603c46f706307`。完整复跑结束后再次核对下表 8 份代码/测试指纹，均与启动前一致；四个质量门 JSON 结果也重新读取核对，没有用未结束输出代替最终结果。

第二、三次完整检查还验证：

- compileall、前端 TypeScript/Vite 生产构建、8 组共 66 项前端脚本、`cargo check --locked` 均通过。
- 原有冻结记忆 `7/7` 场景、`8/8` 查询、Recall@K=1.0、陈旧/禁止泄漏率=0.0；GraphRAG route `4/4`，研究窗口角色 `6/6`，episodic runtime `5/5`。
- 这些是既有小型确定性门；不消费本轮四条标签草案，不代表 LongMemEval 或开放式答案质量得分。
- 现存 Starlette/httpx 弃用警告与 Vite chunk 体积警告仍存在，未为消除警告扩大变更。

## 运行快照与复现

HEAD：`e9aa88c722c1a6ef61b25c191048caf1c0423da1`，工作区已有大量未提交改动，未 reset/clean、未提交。相同 Windows 本机，使用 `souldrive` Conda 环境、UTF-8；Python `3.10.20`、pytest `7.4.4`、FastAPI `0.136.3`、Starlette `1.3.1`，本次 shell 实测 PowerShell `7.6.5`，没有把它写成已在 5.1 验证。GPU 实测 RTX 5060 Laptop、8151 MiB、驱动 `616.92`；不因项目文档的旧配置而假称本次仍为 610.62。这些固定输入回归不调用真实大模型/GPU推理，无训练随机种子或模型权重变更。

复现入口：`conda run --no-capture-output -n souldrive python -m pytest -q tests/test_artifact_smoke.py tests/test_presentation_service.py tests/test_mcp_server_presentations.py tests/test_task_journal.py tests/test_project_skill_installer.py`；另以新的、不覆盖已有文件的 `--output` 路径运行 `scripts/smoke_artifact_flows.py`。完整工程入口仍是 `scripts/check.ps1`，各步骤返回非零时阻断。

| 文件 | SHA-256 |
| --- | --- |
| core/mcp_server.py | `331064d6ef1018ade6a64add3f3891180863985ac40b074b191754e5dde4fac5` |
| core/rag_engine.py | `4355141bb056e09bd4f3c267a0d4bd48cb81cee53ac6f732686c7eb7e8ff9dfc` |
| core/answer_quality.py | `b5cbc60d37ae53164c05a124f01e6042625099d1cc10eb09840f2f5296ea7d32` |
| tests/test_rag_prompting.py | `9c8cb8c4770eb74fc36f259172339228bd9cf44321266963a68d0bb17dc5c066` |
| tests/test_mcp_server_learning.py | `1e082bea61beca7a3bbe60ce5587b57a22b749e7c8b32d2c764101abe7963508` |
| tests/test_answer_quality.py | `929f0bc017a9f611753b03c6187f7453e57609d4c426e66ed0f2687346e1e221` |
| scripts/smoke_artifact_flows.py | `7f03a9b99c4f9a31bca568b1177e9f79460dc84ff22429fcfd2ef10a0b642fe2` |
| tests/test_artifact_smoke.py | `166ca1a3efe6eed0a4fba076e60c104d224ee07de0385e2b51ccaac41ed86c5f` |

## 未闭合的交付门

真实 Tauri 开发/发布解锁隔离、桌面确认交互、取消/崩溃恢复、WPS/COM视觉效果、安装包和真实单项目端到端流程仍须独立验收。服务对象重建不是 OS 进程崩溃/断电恢复，OpenXML 结构有效不是幻灯片视觉合格，mock 邮件不是真实投递。同样不证明跨存储事务或跨主机 exactly-once。

本轮相关修复另见 `2026-10-13-chat-compaction-failure-isolation.md`、`2026-10-13-rag-signed-numeric-evidence.md` 与第四十二轮 query variants 报告的 2026-10-13 修正。四条业务标签草案仅在 `eval/review_drafts/`，原 30 条仍待审核、gold 全空，不冻结、评分、训练或调参。
