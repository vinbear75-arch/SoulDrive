# 实验对比：运行身份门

日期：2026-10-10。本轮对应最终执行大纲 E3/E7 的实验比较链路，只修两运行身份输入校验，不改变指标、时间语义、统计方法、研究协议或冻结评测数据。

## 外部机制与本地缺口

[MLflow Tracking 官方文档](https://mlflow.org/docs/latest/ml/tracking/)将 run 表示为一次代码执行，记录其参数、指标和产物；模型检查点则有独立模型身份，可以在同一 run 内比较。本轮借鉴的是比较对象身份必须明确，不声称 MLflow 自身禁止所有同 run 比较。

SoulDrive 当前接口是选择 `baseline_run_id` 与 `current_run_id` 的两运行比较，不是检查点比较。共享 `compare_experiment_runs` 已校验项目、数据集、脚本和指标完整匹配，但同一个运行被选两次时仍给出 comparable、零差值和 M 证据。独立比较接口、研究准备检查和聊天都经过此共享函数。两个既有导入/审计接口测试也把同一个 run 断言为 comparable；需要修正该矛盾，同时保留导入确认、审计失败警告与不同真实运行可比的正向验证。

## 改生产代码前锁定的条件

- B0：当前 HEAD `e9aa88c722c1a6ef61b25c191048caf1c0423da1`，保留既有工作树改动。`core/experiment_ledger.py` SHA256 为 `c597cd64db5096d4a083e21a80b2c582ff88ef7984a143f7fef7906a70e4b960`；`core/mcp_server.py` 为 `77a742239f67a5c5428bcfa3c15d66334f637eeae5be1afb29c3d264ad80a684`；接口测试改动前 `tests/test_mcp_server_projects.py` 为 `a687b4c19dcb231509bbb7c4acb5e1b10b5b65f966a23a5f9a810648a423f0a0`。
- 数据：新增 `tests/test_experiment_comparison_identity.py` 只用临时工作区、真实 ProjectCatalog、虚构项目与两次合成运行；指标为 synthetic_score/points/v1，两次均为 1.0。固定时间和 ID，无模型、无 GPU、无随机抽样、无待审核语料。
- 负向：同一次真实运行不能成为两运行比较，不产生变化或 M 证据，理由指向选择不同运行；真实比较 API、研究计划与聊天都继承该门，聊天必须在加载模型/检索前阻断。
- 正向：不同运行即使数值相同仍 comparable，零差值为 unchanged，引用保留两个不同运行 ID。先检查请求项目范围，不能用身份错误掩盖既有 cross_project 判定。
- 既有回归：导入确认、审计失败警告、重复统计、测量版本、项目隔离及不同配置的正常比较仍通过。种子 manifest 引用的 `tests/test_experiment_ledger.py` 不改；不通过更新源哈希绕开审核。
- 运行：Windows 本机，现有 souldrive Conda Python；CPU AMD Ryzen 9 8940HX（16 核/32 线程）。先跑新增反例与两条矛盾接口测试取得 B0，再最小修复、扩大定向回归，最后运行原聚合检查。所有耗时只是测试运行时间，不是用户链路延迟收益。

## 运行记录

改生产代码前的 B0 定向回归为 **6 failed、2 passed、36 deselected、1 warning，1.53 秒**；失败均是同一运行仍被返回 `comparable`，没有把命令失败或旧断言误记为业务结果。候选只在共享 `compare_experiment_runs` 的项目范围检查之后增加 run ID 身份门；同一运行返回 `not_comparable`、明确中文原因、空变化和 `change_computation=blocked`，不生成 M 证据。

候选定向回归为 **8 passed、1 warning，1.04 秒**。扩大后的实验账本、测量版本、项目接口、研究工作流和交互上下文回归为 **122 passed、1 warning，25.68 秒**。其中保留了项目越界优先级、不同真实运行的正常差异比较、相同数值的 `unchanged` 比较，以及审计失败警告。

候选 SHA256：`core/experiment_ledger.py` 为 `80b032dd47ab4fe3560f2e058d1b69cade64150f09c56758109b827e0eb5d49f`；新增 `tests/test_experiment_comparison_identity.py` 为 `fc16a5ee6d8786b7877224a54d13f2fb3df6651769efa10a1c80862be0c474f4`；修正 `tests/test_mcp_server_projects.py` 为 `d5429e651050c097ab1b36165609ec0e80ba32440ecff7512fba4d4c8056ef72`。`tests/test_experiment_ledger.py` 未修改，其 manifest 指纹保持不变。

原聚合检查 `scripts/check.ps1` 已结束并退出 0：后端 **1724 passed、1 warning，484.88 秒**；Python 编译检查、记忆/GraphRAG/研究角色三项冻结规则门、前端构建、8 组前端脚本 **66 项**和锁定 Cargo 检查均通过。结果文件复核为记忆 7/7 场景、8/8 查询，图路由 4/4，研究角色 6/6。新增身份回归使后端总数相对上一轮 1718 增加 6 项。

完整检查的 transcript `build/quality-gates/experiment-comparison-identity-full-check-20261010.log` 只保留宿主与起止元数据，完整计数来自同一执行会话的终态输出；这不是完整 stdout 日志。冻结 manifest 引用的 7 个输入文件逐项重算哈希均匹配，`tests/test_experiment_ledger.py` 未被修改。前端构建既有大分块提示仍存在，不是本轮优化对象。

本轮工程闭环已完成，但这只证明运行身份门和现有自动化合同，不证明三类研究窗口的开放式答案质量、模型收益、真实 Tauri/WPS/发行包或跨主机 exactly-once。下一轮仍需从新的外部机制与当前代码证据重新定位缺口。
