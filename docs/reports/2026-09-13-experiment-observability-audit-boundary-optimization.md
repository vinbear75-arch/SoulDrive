# 实验账本结果与审计观测边界优化

日期：2026-09-13

## 对照实践

- [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking) 将 experiment 作为 run 的容器，并在每个 run 中保存参数、指标、时间和 artifacts；结果查询围绕稳定 run ID，而不是一次请求是否成功写完所有旁路日志。
- [MLflow REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html) 将 run metadata、metrics、params、tags 和 artifacts 分开建模；本地 SoulDrive 继续由 ProjectCatalog 维护实验事实、版本和证据引用，不引入远程 tracking server。
- [MLflow 开源仓库](https://github.com/mlflow/mlflow) 同时强调评测结果和 lineage；本轮只修正本地结果提交与审计副作用的边界，不把审计事件冒充实验事实或质量指标。

## 失败优先发现

重复实验汇总、指标导入和运行对比在业务函数返回结构已经形成后直接写审计。审计 sink 故障会把：

- 尚未确认的指标计划误报成失败；
- 已确认并写入实验账本的指标导入误报成失败；
- 已完成的重复汇总或对比结果误报成失败。

指标确认尤其容易造成重试误解：本地 run 已经存在，用户看到 500 后再次提交可能得到冲突或重复导入，而不是清晰的“已完成但观测告警”。

## 本轮修改

- `repeats/summary` 的汇总结果保留，审计故障只附加 warning。
- `experiments/metrics` 的 `needs_confirmation` 与 `imported` 状态保留，`source_version_id` 和实验结果不丢失。
- `experiments/compare` 的 `comparable` 等结果保留，审计故障不改变研究窗口、项目或时间语义。
- 不改变确认门、run ID、项目范围、指标定义/单位/方向、数据集版本、执行环境和证据版本约束。

## 验收证据

- 新增同一项目/同一指标源/同一 run 的审计故障回归，覆盖计划导入、确认导入、重复汇总和同 run 对比。
- 项目接口与实验账本回归 `42 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1321 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是 MLflow 接入，也不是新增远程 tracking server、artifact store 或协作能力。
- 这不证明实验指标正确、重复运行统计有效、原因分析成立或 RAG 答案质量提升。
- 这不是事务性审计 outbox、自动补偿、跨存储 exactly-once；实验账本事实仍以本地 SQLite/ProjectCatalog 为准。
