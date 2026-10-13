# 当前源码 sidecar：隔离构建与运行验收

日期：2026-10-13。轮次：47。当前状态：隔离构建、关键内嵌代码核对、新进程锁定/解锁及完整工程检查通过。不是安装包、真实 Tauri 或模型问答验收。

## 外部机制、本地缺口与验收范围

[PyInstaller 官方 usage](https://pyinstaller.org/en/stable/usage.html)明确区分 dist/work/spec 目录，并说明 `--noconfirm` 会替换输出、`--clean` 会清理缓存；[runtime information](https://pyinstaller.org/en/stable/runtime-information.html)说明冻结程序的资源/可执行路径与源码运行不同。沿用本项目 onedir 方式，不引入新框架或改变分发形态。

B0：既有 sidecar SHA-256 `17278d4e1e1f3aa2a4c8e22cd4f0e1e01d28aabd6d548c71333d2258780a5dce`，其实际内嵌 `answer_quality` 不含当前 `_validate_claim_support/_fact_tokens/_normalize_fact_token`，见前一轮只读审计。当前源码数值检查指纹 `809d14b10776dc5814ad8d61a3d26d6fce345e9ad3fbaba02815f2b78e8378cb`。已有源码完整检查为 1783 项通过，但不代表旧包。

B1/Candidate：以当前 `souldrive` Conda 环境和既有 collect/metadata/hidden-import 清单创建全新候选目录。新 dist/work/spec/cache 均在本轮 `build/release-candidates/` 子目录；不运行固定输出的旧打包入口，不使用 `--noconfirm/--clean`，不调用删除依赖的收尾函数。显式 `--paths` 指向当前项目根，避免隐藏导入来源不明。构建环境关闭模型在线下载和外部存储连接。

在执行前固定验收：

1. 构建前后所有 core Python 文件、sidecar 入口、WPS 渲染脚本及现有打包脚本指纹一致；旧 exe/包不被覆盖。
2. 构建退出成功且实际产生新 exe；记录依赖/工具版本、构建日志、目录规模与文件哈希，不从时间戳推断代码身份。
3. 只读检查新 PYZ 的关键模块与当前代码匹配；不能仅看到函数名就称整个包正确。
4. 使用本轮独立测试工作区、日志目录和空模型目录，关闭可移动监听、Redis/PostgreSQL和在线下载，选择未占用的本机端口。验证新进程启动、等待初始化/解锁、错误口令拒绝、正确虚构口令解锁及锁回；不访问用户真实项目。
5. 子进程需以隐藏窗口启动，绑定自己的父进程/记录 PID，只停止本轮测试进程，不杀用户推理服务。真实 Tauri/安装后行为另验，不把独立 sidecar 当整机 E2E。
6. 有代码修复时先留失败证据并重建新候选，再跑相关回归与完整工程检查。编译/启动失败不得用源码测试全绿掩盖。

B2 的分体安装、依赖裁剪及系统功能变更需要独立选择；本轮不实施。原评测集、30 条种子与独立标签草案不改、不评分或训练。新候选即使通过，NSIS/MSI、Tauri 界面、WPS视觉和完整业务验收仍不自动关闭。

## 执行记录

候选根目录：`build/release-candidates/current-source-20261013-120726/`。该目录新建，旧包未覆盖。

| 阶段 | 真实结果 |
| --- | --- |
| 构建 | PyInstaller **6.20.0**、hooks **2026.6**、Python **3.10.20**，约 369 秒构建完成，退出 0；86 份 core/入口/打包/渲染源码指纹前后相同 |
| 实际候选 | `dist/souldrive-sidecar/souldrive-sidecar.exe`，67,681,737 bytes；完整 onedir 为 9811 文件、3,843,587,599 bytes；保留依赖，未裁剪或宣称缩包 |
| 关键代码核对 | PYZ 共 13358 模块；answer_quality、rag_engine、mcp_server、sidecar_runtime、conversation_store、state_backends、presentation_service、task_journal 共 **8/8** 与当前源码编译代码对象相等，不只是函数名存在 |
| 第一次新进程 | 新工作区等待初始化并保持锁定；无 token 为 401、有 token 但未解锁为 423；未确认无恢复条款时初始化 400 且不产生 keystore，明确确认后初始化成功 |
| 两次口令流程 | 错误虚构口令 403，状态仍锁定；正确口令解锁，资料列表 200；锁回后保持锁定。第二次是同一个已初始化的隔离工作区，重启没有自动解锁 |
| 进程结束 | 两次均通过 `/shutdown` 正常退出 0；随后查询两次测试 PID 均已不存在，不停止其他用户进程 |
| 源码交叉回归 | sidecar runtime、打包规则、MCP security：`35 passed, 1 warning`，3.24 秒，退出 0 |
| 完整工程检查 | 退出 0：后端 `1783 passed, 1 warning`，461.29 秒；前端 8 组 66 项、构建、compileall、锁定 Cargo 及四个既有质量门通过 |

本轮新增可复现入口 `scripts/smoke_packaged_sidecar.py`，必须明确给出 exe 和全新输出目录，已有输出目录会拒绝复用。它只使用标准库，隐藏子进程窗口、移除继承的 SoulDrive 配置及 Python 搜索路径配置、将 PATH 限制到 Windows 系统目录、关闭模型下载/外部存储/可移动监听，且在初始化写入前检查测试工作区身份与随机 token。所有状态来自真实新 exe/HTTP，而非 TestClient 或伪造 handler。测试 workspace 和日志保留在候选目录，只有虚构口令/空资料，不访问真实项目。

额外负例验证入口自身的防覆盖边界：再次指定已有输出目录时退出非零，原 `report.json` 哈希不变；以 `python -O` 关闭断言时在创建输出/启动进程前拒绝，输出目录未产生。分别保留 `smoke-output-reuse-rejected.log` 与 `smoke-optimized-rejected.log`；这两项预期拒绝不是应用回归失败。

第一、二次冷启动到 security/status 的单次观察为 1547.0 ms、1047.0 ms，只是本机两次诊断，不是 P50/P95 或真实模型首字延迟。报告明确 `model_inference_tested=false`、`desktop_or_installer_tested=false`。

首个代码比对命令用 UTF-8 字符串读带 BOM 源码，触发工具侧 SyntaxError；保留 `embedded-code-check.log`。改用 `compile(source_bytes, ...)`，按 Python 原生编码规则处理 BOM 后得到 `embedded-code-check2.log` 的 8/8，不修改源码去迎合工具。比较绑定可执行代码，源码原始字节另由构建前后指纹保证；不宣称所有第三方依赖功能已验证。

| 产物/证据 | SHA-256 |
| --- | --- |
| 新候选 exe | `a34ebb84503832326561870fc55ce6888cc2248130c42ff9d0deb9a354b1ca64` |
| build.log | `7da7d951bc7ee400776bdb1f6971470b1abe8bd806aa081ad61d2b0bce7eee67` |
| embedded-code-check2.log | `4a2c52f3b32f170f719f94c299dac31cda3b87204e3655469ef681e027dbcb55` |
| smoke-new-and-existing/report.json | `1e4d8b73c7b2dcffe74aafcfa344f5a737fc6bc2754784a69f413fbb038d4ba7` |
| scripts/smoke_packaged_sidecar.py | `6b56990a2504bd47d97afbbf17c666348fb48480a8355060116e993a1a4722b5` |

完整原始检查日志 `build/quality-gates/check-round47-20261013-121729.log` 已实际读取核对：1086 行、36878 bytes，SHA-256 `16a496e7591ce9da70f2a891f49f33298bc1b091347911875983e4b7b4963a34`。记忆质量 `7/7` 场景、`8/8` 查询；GraphRAG route `4/4`；窗口角色 `6/6`；episodic runtime `5/5`，四份 JSON 均已重读。保留既有 Starlette/httpx 和 Vite chunk 警告，不升级依赖掩盖。

在完整检查结束后再次解析 build.log 的原始 86 份源码 manifest，并逐文件重新计算哈希：不一致项为空。原 seeds、manifest、四条标签草案仍分别保持 `ce12cad8...55424`、`752f72c5...4612d`、`0052d257...c84a7`，未冻结、评分、训练或调参。

旧 sidecar 与旧 Tauri release exe 指纹均再次核对未变，分别仍为 `17278d4e...a5dce` 与 `e22637a1...79ad9`（完整值见前轮只读报告）。构建后 D 盘约 34.4 GB 可用；不删除旧包释放空间。

## 模型警告与默认主链路的区别

构建有可选 TensorRT/onnx/server/transformers CLI、llama_cpp native 导入及 bitsandbytes 多平台 DLL 警告，均保留在 build.log，不能因 exe 启动成功就忽略。只读 PE 检查表明候选的 ggml-cuda 依赖 `cublas64_13.dll`；多平台 bitsandbytes 的 CUDA 11/12/ROCm 警告不应直接当作该 ggml 依赖结论。

同时回查当前两条 Tauri 启动分支：均显式注入 Ollama 默认聊天 `qwen3.5:9b`、嵌入 `qwen3-embedding:4b`。按 [Ollama 官方只读模型清单接口](https://docs.ollama.com/api/tags)检查本机 `127.0.0.1:11434`，服务可访问、7 个已安装模型、上述两者存在；不列出其他用户模型名称，不发生成/嵌入/拉取/卸载请求。首个清单命令被 Conda 的多行参数限制拒绝，未访问服务；随后 PowerShell 直连成功，证据为 `default-ollama-readonly2.log`，失败日志仍保留。

这证明默认后端值得进入下一轮真实推理/检索诊断，**不证明** Ollama 推理、显存调度、生成质量、用户配置最终生效或 GGUF 旁路已可用。不得据 llama_cpp 收集警告就静默改 CPU、卸载依赖或替换默认模型。

## 仍未完成

新目录候选尚未成为 Tauri 的发行资源或安装包；本轮未覆盖旧资源、未运行 NSIS/MSI、未改系统可选功能。手动清除测试子进程开发环境不等于实际 Tauri release 已正确移除父环境变量。Redis/PostgreSQL 关闭后的本地启动不证明真实外部后端，正常重启不等于崩溃/断电恢复。模型、真实 UI、WPS、安装与完整项目主流程继续单独验收。goal 保持 active。
