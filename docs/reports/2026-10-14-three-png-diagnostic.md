# 三个既有 PNG 案例的证据诊断

## 范围与数据边界

对应原计划 E6 的 `CP-RAG017/018/028`，实际固定 JSONL ID 为 `CP-RAG-017/018/028`。只读原文件、原问题和原预期来源，不修改标签、文件内容或原冻结集，不训练/调参，也不把文件名命中称为事实回答通过。

当前 `eval/fixtures/product_b1_current_v0.1/product_rag_eval_v0.1.jsonl` SHA-256 为 `8a812073d05dba58233c38077da48982be5ca89980b7036cc23f92a48cdd40c7`。2026-08-22 历史 run_manifest 中同路径的指纹是 `d1bd0dae286b4f4eab4517c2ba99fceb42b5991985b82d36a98d71355ce8015f`，并非当前整文件指纹；三个指定问题及预期来源与历史逐题结果相同，但不能据此宣称整集新旧可比。

原始三个 PNG 均存在、均为 8,258 bytes，SHA-256 完全相同：`f24fd48d1e4b47eb07de0a74405bcbda289d93070edda56dd74f039c1c21803e`。当前 manifest 也明确记录这个相同指纹。实际查看图片内容为英文项目标题、target_binding_accuracy 的 0.620/0.790/0.850、conflict_clarification_accuracy 0.880 和 dataset 标签；没有实际选区控件或类别 C 混淆矩阵。

## 逐题结论

| 案例与原预期 | 已观察到哪一层 | 已有证据 | 下一步与限制 |
| --- | --- | --- | --- |
| 017 / `08_selection_ui.png` | 历史摄取/身份覆盖异常；当前图片语义也不包含真实选区操作 | 2026-08-22 摄取行匹配数 0、document_present=false，历史最终 Top-6 无该来源。单独图像 OCR smoke 当时却 3/3 有文本。2026-08-31 的 `_cp_rag_017_exact_diag.json` 中，同一原问题在 dense/keyword/source_title/merged/reranked/final 均命中，最终 1 条为该 PNG。 | “图片无字”不能解释全部历史现象。相同文件哈希的多来源身份是待验证的候选原因，未证明因果；当前源码已按稳定来源区分索引，但本轮未重建并回放完整 30 资产检索。标题命中也不能证明图片中真的有选区。 |
| 018 / `08_metrics_chart.png` | **历史并非最终漏召回**，而是摄取汇总与检索证据不一致 | `product_b1_v0.6_rag_live.jsonl` 的第二条证据正是该 PNG，含 0.620/0.790/0.850；对应摄取汇总却匹配 0。不能继续沿用“3 条全部漏召回”的笼统描述。 | 保留原问题、来源和原结果；分别核查摄取身份映射与最终检索，不为追求“修好”改变预期。历史逐阶段候选未保留，不能补造 dense/keyword/rerank 排名。 |
| 028 / `vision_confusion_matrix.png` | 原始资产与题目要求不符，并伴随历史最终来源缺失/跨项目候选 | 文件内容与前两张相同，没有类别 C；历史 Top-6 没有预期 PNG，且包括主项目指标文件。后来 `product_b1_retrieval_candidate_20260829.jsonl` 虽返回该文件名，也不能支持类别 C 的事实结论。 | 先提交数据问题，而不是换 VLM/图数据库/查询 Agent。若将来需要真正混淆矩阵案例，应另建独立候选、审阅图片内容与标签后决定是否冻结；本轮不替换原冻结案例，不生成 gold。 |

## 本轮真实 OCR 复跑

直接调用现有 `LocalImageOcrParser` 读取三个原文件。未把 manifest 的 `text` 或题目文字加入 OCR，未扩大截图范围，未下载模型。使用已存在的 RapidOCR 3.9.1、ONNX Runtime 1.23.2，实际三项 session 的 provider 都为 `CPUExecutionProvider`；检测/识别使用 PP-OCRv6 small，方向分类使用现有 ch_ppocr_mobile_v2.0。

三个文件各产生 1 个 chunk，识别文字相同，含图片中实际的四行。第二次带实际 provider 的记录耗时分别为 2534.675 / 638.403 / 641.55 ms；第一条包含懒加载，不能当成稳定 P95，也不与历史时间做效果比较。第一轮记录也保留：2656.678 / 660.149 / 645.298 ms。

原始结果：`build/quality-gates/png-diagnosis-round51-ocr.json`，两次日志为 `png-diagnosis-round51-ocr.log` 与 `png-diagnosis-round51-ocr-providers.log`。原文件与当前 JSONL 前后指纹一致。OCR 输出自身不携带业务项目字段，项目/版本绑定须由目录与索引层补充，不能从这一解析单测直接推出项目隔离通过。

## 当前版本的有限分层回放

随后在全新 `build/quality-gates/png-replay-round51/workspace` 注册原目录版本，并逐个校验 SHA-256 后复制原 30 份资产。没有运行会重建资产的 corpus builder，也没有将 manifest 的 `text` 注入解析或检索。三个原问题、预期来源和 Top-K=6 不变；关闭使用反馈写回，避免评测改变下一题的排序状态。以 BASIC 摄取模式隔离图抽取，GraphRAG 未执行，不用本结果宣布图链路质量通过。该副本没有用户密钥，明文派生索引也不代表加密分支验收。

实际 30/30 摄取成功、0 跳过、0 文件失败、30 chunks；总摄取耗时 50,692.747 ms。嵌入为已有本地 Ollama `qwen3-embedding:4b`，实际重排为 `Qwen3-Reranker-0.6B` / CUDA，没有生成回答或下载新模型。对现有生产检索函数仅加旁路记录，保留每次 dense/keyword/source-title/merge/hydrate 的候选身份与项目条件，以及重排后的父块扩展输入，未改变候选或排名。

| 原问题 | 当前最终来源 | 嵌入 / dense / keyword / 重排 / 总计 ms | 结论边界 |
| --- | --- | --- | --- |
| CP-RAG-017 | `08_selection_ui.png` | 66 / 22 / 4 / 2082 / 2254 | 当前来源召回正常；图片本身不包含真实选区操作，不推出视觉定位能力。 |
| CP-RAG-018 | `08_metrics_chart.png` | 70 / 16 / 2 / 550 / 702 | 当前来源与实际 OCR 数值可追溯；不把命中视为完整回答评分。 |
| CP-RAG-028 | `vision_confusion_matrix.png` | 69 / 6 / 2 / 151 / 263 | 当前来源召回正常，但图片没有类别 C，事实缺口仍是资产/题目不匹配，需另行审核候选资料。 |

每题最终只有上述一个来源，项目 ID 与该题一致。三项 PNG 在索引中拥有不同的来源身份；当前不能复现历史“相同哈希导致丢失”的猜测，也不能倒推历史失败的因果。原始逐阶段记录和摄取观察在 `build/quality-gates/png-replay-round51/report.json`，SHA-256 `c2e8dfe8334051104d39d34de2fcceb706f1c7546ebe21d2077860162c830dc1`；运行日志 `build/quality-gates/png-replay-round51.log`，进程退出 0。

主机 GPU 总显存每秒采样，共 49 次，首值 425 MiB、最高 6988 MiB。这包含其他主机进程，不是单任务归因，也不是连续硬件峰值。没有同时生成对话/视觉答案，不能推断三模型并发容量或稳定 P95。30 个原始资产、manifest、JSONL 前后指纹全部一致。

## 最终诊断边界

历史检索/生成分项耗时和模型名称均为 null，继续保留未测；当前有限回放另记实际检索/重排耗时，不回填历史空值。当前生成耗时仍为 null，不填零。历史 017 的逐阶段记录与后来文件名命中仅作为归档诊断，不是同条件效果比较。

已定位的确定事实是：三个原始资产字节相同、028 要求的事实不在图片中、018 历史最终证据已命中，以及历史摄取/最终检索记录存在身份不一致；当前分层回放中三个来源均正常召回。身份去重导致历史失败仍只是候选原因。原计划 E6 的有终点诊断已形成三项定位、证据、下一步和未测边界，不要求人为把三题改成答案通过。此处仅关闭 E6 诊断，不宣布整项目验收完成；真实视觉/新标注质量继续遵守人工审核门。
