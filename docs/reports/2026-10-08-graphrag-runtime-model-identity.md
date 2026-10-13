# GraphRAG 验收记录绑定实际运行模型

日期：2026-10-08。状态：B0 四个错误记录反例已复现；定向回归、真实运行身份只读检查及第十一轮完整工程检查通过。

## 真实缺口与外部实践

`scripts/eval_graphrag_acceptance.py` 的报告在关闭客户端之后从环境读取 `SOULDRIVE_OLLAMA_MODEL`、`SOULDRIVE_OLLAMA_EMBEDDING_MODEL`，缺省 embedding 只写 `workspace default`。但 RAG 可实际选用 compatible 客户端或 GGUF；KB 也可能实际解析本地 sentence-transformers 模型。环境中遗留的 Ollama 名称不能作为运行身份，未知后端也不应被补成默认模型。

借鉴 [MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/) 将运行元数据、模型与产物关联的机制；本项目仅修正现有 JSON 记录，不安装 MLflow 或增加服务。具体“读取已加载客户端与已解析 KB 名称”的方案是对当前代码缺口的本地映射。

## B0 与最小改动

修改前，main 入口反例 **4 failed、12 deselected，0.25 秒**：实际 Ollama、compatible、GGUF 与未知客户端均被错误记成配置中的另一 Ollama 模型。测试也安排 `close()` 清空 `rag.llm`，防止在释放后才取身份。

普通 RAG 评测已有身份读取逻辑，提取为两个评测入口共用的 `loaded_chat_model_name()`：读取客户端 `model`/`model_name`，GGUF 只保存实际路径 basename；没有身份则返回 null。GraphRAG 在关闭资源前记录实际客户端名和 KB 的 `embedding_model_name`，按已解析本地路径或 Ollama embedding 对象记录 provider；无法识别则 null，不从遗留环境补名字。

没有修改模型选择、检索、提示词、回答验证器、冻结标签、指标、任何接受门或回答重试预算。模型名不证明权重哈希、Ollama tag 不可变性或推理参数一致；后续高要求可复现运行仍需要这些证据。

## 已验证

- 初次组合检查误写不存在的测试路径，pytest 退出 1、没有运行测试；不能作为通过证据。核对实际文件名后重新运行。
- 补上 Ollama embedding 与未知后端边界；模型身份、GraphRAG 验收、审计隔离、种子保护、评测资产组合 **90 passed，1.86 秒**。测试不调用远端模型，不读取真实文档答案。
- 本机现有独立评测工作区的身份检查退出 0：`mode=runtime_identity_only`，客户端为 `qwen3.5:9b`，KB 为 `bge-small-zh-v1.5` / sentence-transformers；`queries_executed=0`、`answers_scored=0`。复用既有模型和工作区，没有下载模型、访问云端、读取待审核题目的答案或执行新质量实验。
- 第十一轮完整 `scripts/check.ps1` 退出 0：后端 **1715 passed、1 warning，426.89 秒**；编译、冻结记忆 7/7 场景与 8/8 查询、GraphRAG 路由 4/4、研究窗口角色 6/6、前端构建/8 组 66 项脚本与锁定 Cargo 检查通过。这是本轮独立终态，不借用第十轮成绩；不能把第九轮临时文件锁失败改记为通过或根因已修复。

当前 `eval_rag.py` SHA256 `364f27a893b4e36fe47219c8d311210580c2b833fa7a61e00122e65eb380d86a`；`eval_graphrag_acceptance.py` SHA256 `f90e600c36458c33f26da48d6799ffb0865f5b635b275f418f293941dd46dc1e`；测试 SHA256 `358ce73da9aae8a13a92dcd67ae31dcc1088281685ead548de2111a5e890e610`。

本轮不基于许可尚未确认的文献再跑新答案质量实验。此前固定 GraphRAG 回放的全局/局部仍发生引用校验回退，身份记录修正不是内容准确率提升，也不把 30 条 pending_review 种子转成 gold。
