# SoulDrive 竞赛提交收尾快照

生成时间：2026-08-31T03:13:36.549954+00:00

## 一句话定位

用户指着屏幕中的实验结果说“为什么比上个月差”，SoulDrive 在当前项目内对齐语音、授权选区、实验版本和历史偏好，在端侧给出可验证差异、原因证据等级和最小验证实验，并生成带引用的汇报草稿；歧义时先澄清，未经确认不执行外部动作。

## 当前状态

- MVP 演示可用：是
- 竞赛完整验收：否
- Git HEAD：`0e216835775a93ed263e57911b6d6aa31d813acc`
- 工作区有未提交改动：是
- 未提交路径数量：548
- 未提交 diff 哈希：`569f250cccb351133b3f8e91efdf89656f9da9c6edc142a687b53a74454ee3d9`

## 可在材料中陈述

- 端侧 Windows PC 本地运行，不默认依赖云端推理。
- 支持语音、授权屏幕/UIA/OCR、项目上下文和 RAG 证据的组合链路。
- 项目内优先检索并支持全局文档回退，已有跨项目私有来源隔离回归。
- 实验指标 CSV/XLSX、配置、日志和文档能带来源定位进入证据链。
- 邮件、日历、删除和外部动作保留确认门，不自动执行不可逆动作。

## 不得宣称

- 不能宣称通过外部未公开评测或获得官方成绩。
- 不能把 targeted tests、烟测或合成集称为整体答案准确率。
- 不能宣称 GraphRAG 生产业务验收已经完成。
- 不能宣称 VLM 慢路径完整回答 P95 已稳定小于 5 秒。
- 不能宣称已有 30 条以上独立真实多模态业务 episode。

## 仍需人工收尾

- 用 docs/demo_runbook_v0.1.md 录制一条真实演示。
- 演示前确认工作区已解锁、索引已完成、模型已预热。
- 提交材料中保留 forbidden_claims 和 blocking_items，不写成已完成项。

## 已跑验证

- 后端关键组合测试：136 passed, 1 warning
- closeout 报告测试：2 passed
- 前端媒体事件测试：8 passed
- 前端项目/文档测试：3 passed
- Python compileall：通过
- 前端 npm run build：通过，仅有既有 chunk size warning
- Tauri cargo check --locked：通过

## 路线图逐项核查

| 阶段 | 名称 | 状态 | 未完成或需人工项 |
| --- | --- | --- | --- |
| S0 | 冻结定位与评测契约 | partial | PPT 首页是否已更新需要人工检查最终参赛 PPT 文件。；sealed-test 最终冻结和演示材料冻结仍需提交前人工确认。 |
| S1 | 真实数据与完整端到端基线 | blocked | 不足 30 条独立真实业务 episode。；缺少跨说话人、分辨率、项目、指代方式的完整冻结集。；缺少完整 ASR/OCR/VLM/对齐/检索/首 token/完整回答逐样本 P50/P95。 |
| S2 | 实验账本与严格比较 | partial | 确认卡 UI 对日期、单位、数据集版本和 split 的完整人工流程仍需演示机复核。；复杂实验原因分析仍不能宣称因果结论，只能给证据等级和验证计划。 |
| S3 | SoulAlign-T 与 GUI 目标验证 | partial | VLM 慢路径完整回答 P95 未稳定小于 5 秒。；图标目标相对 ScreenSpot 约 1.9% 基线的显著提升仍未证明。 |
| S4 | 可验证个性化 | partial | 真实 30 episode 上的成对画像收益、撤销残留率和人工事实复核仍未完成。 |
| S5 | 证据式生成与项目 Playbook | partial | 长论文和标准的真实层级文档树路由仍需在生产加密工作区重建后验收。；产物级 E/V/A/M 引用语义仍需人工或 NLI 评测。 |
| S6 | 竞赛冻结与回归 | partial | 最终真实演示视频需要人工录制。；正式提交包的代码、模型、配置、数据和随机种子需要在提交前最后冻结。；Tauri cargo check 和真实端到端演示仍需在最终演示机上确认。 |

## 阻塞项

- 正式加密工作区生产索引重建和人工验收仍需在演示机上完成。
- 真实多模态独立 episode 数量仍不足路线图 S1 的 30 条门槛。
- 完整 VLM+RAG 首 token/完整回答 P95 仍缺真实冻结集实测。
- 外部未公开评测协议未在当前工作区发布。

## 证据文件

| 项 | 路径 | 状态 |
| --- | --- | --- |
| 执行路线图 | `docs/plans/2026-08-29-souldrive-competition-value-roadmap.md` | OK |
| 项目主文档定位 | `README.md` | OK |
| 3 分钟演示 runbook | `docs/demo_runbook_v0.1.md` | OK |
| 限制与问题清单 | `docs/known_issues_v0.1.md` | OK |
| 产品 B1 语料与 baseline | `docs/product_b1_corpus_and_baseline.md` | OK |
| 竞赛分项 scorecard | `eval/results/competition_scorecard_v0.1.json` | OK |
| 旧版 release gate 快照 | `eval/results/competition_release_gate_current.json` | OK |
| RAG 冻结 baseline | `eval/results/rag_rebuild_baseline_v0.1.json` | OK |
| 项目作用域产品 RAG 结果 | `eval/results/product_b1_scoped_current_20260830.jsonl` | OK |
| 全局文档跨项目最终回答回归 | `eval/results/rag_global_scope_answer_recheck_20260830.json` | OK |
| XLSX 表格摄取烟测 | `eval/results/rag_xlsx_table_ingestion_smoke_recheck_20260830.json` | OK |
| 结构化来源定位回归 | `eval/results/rag_structured_source_locator_recheck_20260830.json` | OK |
| 真实多模态阶段账本回归 | `eval/results/real_mmi_stage_ledger_recheck_20260830.json` | OK |
| 多模态事件契约回归 | `eval/results/multimodal_structured_event_contract_recheck_20260830.json` | OK |

## 配置与数据哈希

| 项 | 路径 | 状态 | SHA-256 |
| --- | --- | --- | --- |
| SoulAlign-lite 配置 | `configs/soulalign_lite_v0.1.json` | OK | `591857064edfeb0ec0ee46643978ef793468ce4b5578fef0ecafa03c9d9bab46` |
| Python 依赖 | `requirements.txt` | OK | `da3caa1a66773fa5bee30b034674c3ff84b4a4541ba287afe1cc3989845db22e` |
| Topic5 GPU 依赖 | `requirements-topic5-gpu.txt` | OK | `05679e9e9ea0c1c9e6d1eb6b285f7054852c180e6e90970b6184fbf8fdda1eb1` |
| 前端依赖声明 | `souldrive-ui/package.json` | OK | `c0dcb81c4ea0a86bffabf1ebfc4a6caa3e51a674740b7583cc5a1b2da6fdb481` |
| Tauri/Rust 依赖声明 | `souldrive-ui/src-tauri/Cargo.toml` | OK | `ed111c6d9fbf164802cedcab12c04990879f8106ec56b4f4610130c1a201ae60` |
| 产品 B1 当前语料 manifest | `eval/fixtures/product_b1_current_v0.1/product_benchmark_manifest_v0.1.json` | OK | `791b4f996a445776553980bd2d9b30cf9e80437bb2213bf45829275b159e574e` |
| 产品 B1 RAG 评测集 | `eval/fixtures/product_b1_current_v0.1/product_rag_eval_v0.1.jsonl` | OK | `8a812073d05dba58233c38077da48982be5ca89980b7036cc23f92a48cdd40c7` |
| 多模态媒体集 manifest | `eval/fixtures/souldrive_pc_mmi_media_480_v0.2/manifest.json` | OK | `2fd7cf29922cfecf21c6356dd4fbe304e45ed173f611143cd4225ca55e702643` |
| 多模态媒体 episode | `eval/fixtures/souldrive_pc_mmi_media_480_v0.2/episodes.jsonl` | OK | `8d54a8bdded9b858d7a663bb09d22dc736eaa68df6bddcac14f6c10839cbe99c` |
