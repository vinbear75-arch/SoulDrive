# 评测运行清单与实验 lineage 优化

日期：2026-09-13

## 发现

数据集指纹已经能证明评分标签一致，但一次现场 RAG 评测还需要知道使用了哪个代码状态、Python/包/GPU 环境、Top-K、重排参数、模型名和指标摘要。此前这些信息只零散存在终端输出或旧的专项 manifest 中，普通 `scripts/eval_rag.py` 运行没有自动留下可复核的 run lineage。

## 公开实践对照

- [OpenAI Evals 运行说明](https://github.com/openai/evals/blob/main/docs/run-evals.md) 为每次运行保存 `run_id`、本地 JSONL record path，并支持中断后按 progress 继续；本地沿用已有 `souldrive_run_manifest_v0.1`，不引入新的服务端 recorder。
- [OpenAI Evals 构建评测](https://github.com/openai/evals/blob/main/docs/build-eval.md) 用 eval name/split/version 区分可比较版本；本地把 baseline level、dataset fingerprint 和 repository snapshot 放进同一清单。
- [Phoenix 实验流程](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments) 要求固定数据集、任务、评估器和运行配置进行可比实验；本地至少把当前脚本能确定的输入、检索参数、模型和环境写出，并明确索引/模型外部状态仍需单独核对。

## 实现

1. `scripts/eval_rag.py` 新增 `--save-run-manifest`、`--run-id` 和 `--baseline-level`，复用既有 `souldrive_run_manifest_v0.1` schema，而不是新建第二套格式。
2. 清单保存数据集文件 SHA-256、dataset fingerprint、Git HEAD/工作区状态、Python/包/GPU、评测模式、Top-K、检索开关、模型名和不含逐样本行的指标摘要。
3. 未绑定指纹的旧结果仍能读取，但 manifest 的 `dataset_fingerprint_status` 为 `legacy_unbound`，并在 limitations 中明确不能作为严格版本对比证据。
4. 仓库状态读取失败不会阻断评测清单写入，而会以 `unknown` 和 limitation 显式记录；这保持本机离线评测的可恢复性，不伪造 Git 证据。

## 失败优先与验收

- 新增 manifest 回归第一次运行真实失败：`scripts.eval_rag` 尚不存在 `save_run_manifest`。
- 实现后 manifest 专项通过；评测资产/工作流/RAG 提示组合 `173 passed, 1 warning`。
- `save_run_manifest()` 单元回归确认清单包含 `run_id`、CANDIDATE、数据集指纹、重排参数、模型名和输出路径；JSON 使用 UTF-8 写入。
- 完整 `scripts/check.ps1` 已通过：后端 `1222 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 清单记录的是评测程序可观察到的 lineage，不会自动证明向量/关键词/图索引内容、embedding 权重、聊天模型文件、外部 Ollama 服务或硬件状态与另一轮完全相同。
- 目前没有把 manifest 做成跨进程任务队列，也没有宣称可恢复执行或 exactly-once；它只是一次评测运行的可追溯元数据。
- 旧结果不会被自动重算或伪造迁移；要建立严格 B0/B1/Candidate 对比，仍需带当前 dataset fingerprint 和现场 run manifest 的新运行。
