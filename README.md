# SoulDrive

比赛主线目标：用户指着屏幕中的实验结果说“为什么比上个月差”，SoulDrive 在当前项目内对齐语音、授权选区、实验版本和历史偏好，在端侧给出可验证差异、原因证据等级和最小验证实验，并生成带引用的汇报草稿；歧义时先澄清，未经确认不执行外部动作。

SoulDrive 是一个面向私有资产的端侧知识引擎，核心目标是把本地知识文档的解析、索引、检索增强生成、证据链和知识图谱打磨稳定。当前演示与测试仍以论文作为默认切入口，但系统能力不局限于论文场景。

## 核心模块

- `core/`：本地 sidecar、工作区管理、文档解析、索引、检索、RAG、审计日志、模型运行配置。
- `souldrive-ui/`：React + Tauri 桌面界面，用于展示知识文档、运行态、问答、证据链和导图。
- `tests/`：后端核心逻辑的 `unittest` 测试。
- `config/enterprise-policy.json`：本地运行策略，保留 API 限流、CORS、lite/license 等基础约束。

## 本地业务验证

2026-10-13 阶段交付与空间清理记录见 [阶段闭环报告](docs/reports/2026-10-13-stage-closure-and-cleanup.md)。打包服务的实验比较主线已通过合成工程材料验证；真实桌面交互和开放式业务质量仍是单独的验收项。

2026-10-14 的 [实际桌面与产物记录](docs/reports/2026-10-14-answer-fallback-transparency.md) 保留了两次失败大纲、修复和十页 WPS 逐页/编辑验证。普通回答回退会明确说明；模型 PPT 结论不能绑定所列原文时改用明确标注的摘录，旧提纲也不能绕过确认/恢复校验。这是保守的来源绑定，不是自由改写或因果推理达标。

2026-10-15 的 [研究材料准备流程](docs/reports/2026-10-14-research-context-coverage.md) 修复配置漏入上下文及变化表格误去重。未确认的指标比较先列出表头登记缺口、原文记录和验证草案，引导进入已有指标确认/运行比较流程，不猜测相同数据集或计算未确认增幅；实际桌面切换/重启可找回六条来源。自由研究综合、原因判断和长期记忆业务质量仍未通过，本轮源码尚未重新打包，不把准备检查或摘录产物当作全部业务完成。

默认启动不再自动解锁工作区或内置开发口令；已有工作区/密钥保留，使用正常界面解锁。可选 Redis/Postgres 需自行明确配置。清理编译缓存后，下一次开发启动会重新编译；模型和原始资料留在本机，不随 GitHub 源码上传。

发布前请在同一 Windows 本机环境验证文档摄取、检索问答、证据引用、项目隔离、端侧多模态感知与人工确认链路：

```powershell
conda run -n souldrive python -m pytest -q
npm.cmd --prefix souldrive-ui run build
cargo check --locked --manifest-path souldrive-ui/src-tauri/Cargo.toml
```

## 参赛文档

- [项目总览](docs/competition_project_overview.md)
- [运行与模型](docs/competition_runtime_and_model.md)
- [实验与证据](docs/competition_experiment_and_evidence.md)
- [参赛文稿总稿](docs/competition_submission_narrative.md)
- [参赛 PPT 大纲](docs/competition_ppt_outline.md)
- [提交清单](docs/competition_submission_checklist.md)

## 重装系统后从零恢复

本节是恢复 SoulDrive 的主入口。恢复基线就是“本 README 所在的 Git 提交”。GitHub 只保存源码、测试、配置和文档；模型、知识库、索引、运行状态、密钥、依赖目录与构建产物都不会上传。

### 1. 安全边界

如果旧系统疑似感染恶意程序，只从 GitHub 重新克隆源码。不要从旧系统复制以下内容：

- `models/`、`runtime/`、`souldrive_db/`、`data/`、`index/`、`audit/`。
- `.venv/`、`venv/`、`node_modules/`、`build/`、`dist/`、Tauri `target/`。
- `souldrive-ui/src-tauri/sidecars/` 中打包出的 `.exe`、`.dll` 和其他二进制文件。
- `.env`、`license.json`、证书、私钥、旧工作区的 `keystore.json`。

重装后应在干净系统重新扫描克隆目录，并撤销旧电脑使用过的 GitHub 凭据。源码审计可以降低风险，但不能证明受感染系统上的文件绝对无污染。

### 2. 环境基线

当前验证基线为 Windows、Python 3.10、Node.js 24/npm 11、Rust/Cargo 1.95 和 Git 2.53。Python 应使用名为 `souldrive` 的 Conda 环境；源码和公开文档不得写入本机解释器绝对路径。

安装以下工具：

- Git。
- Anaconda 或 Miniconda。
- Node.js（包含 npm）。
- Rust stable（包含 Cargo）。
- Tauri 在 Windows 上需要的 Microsoft C++ Build Tools 和 WebView2 Runtime。

### 3. 克隆源码并恢复 Python

```powershell
git clone https://github.com/vinbear75-arch/SoulDrive.git
cd SoulDrive
git status --short --branch

conda create -n souldrive python=3.10 -y
conda activate souldrive
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
```

`git status` 应显示干净工作区。当前恢复基线为 CPU-only。

### 4. 恢复前端与 Rust 依赖

PowerShell 的执行策略可能阻止 `npm.ps1`，因此统一使用 `npm.cmd`：

```powershell
cd souldrive-ui
npm.cmd ci
cd src-tauri
cargo fetch --locked
cd ..\..
```

`package-lock.json` 和 `Cargo.lock` 是依赖恢复基线，不要在首次恢复时删除或重新生成。

### 5. 从可信来源恢复模型

模型目录已被 `.gitignore` 排除。使用固定 Hugging Face revision 下载，避免模型仓库更新导致结果漂移：

```powershell
conda activate souldrive
python -m pip install "huggingface_hub==1.20.1"

hf download Qwen/Qwen2.5-3B-Instruct-GGUF qwen2.5-3b-instruct-q4_k_m.gguf `
  --revision cc1e68eea5f05f88f41a6de1fc73110178f23715 `
  --local-dir models

hf download BAAI/bge-small-zh-v1.5 `
  --revision 7999e1d3359715c523056ef9478215996d62a620 `
  --local-dir models\bge-small-zh-v1.5

hf download Qwen/Qwen3-Reranker-0.6B `
  --revision e61197ed45024b0ed8a2d74b80b4d909f1255473 `
  --local-dir models\Qwen3-Reranker-0.6B
```

模型用途：

- `qwen2.5-3b-instruct-q4_k_m.gguf`：必需，本地聊天生成模型；来源为 [Qwen 官方 GGUF 仓库](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF)。
- `bge-small-zh-v1.5/`：必需，本地中文嵌入模型；来源为 [BAAI 官方模型仓库](https://huggingface.co/BAAI/bge-small-zh-v1.5)。
- `Qwen3-Reranker-0.6B/`：推荐，本地中文/多语言轻量重排模型；来源为 [Qwen 官方模型仓库](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)。缺失时系统关闭重排，但基础检索仍可工作。

校验关键权重：

```powershell
Get-FileHash -Algorithm SHA256 `
  models\qwen2.5-3b-instruct-q4_k_m.gguf, `
  models\bge-small-zh-v1.5\model.safetensors, `
  models\Qwen3-Reranker-0.6B\model.safetensors
```

预期 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `qwen2.5-3b-instruct-q4_k_m.gguf` | `626B4A6678B86442240E33DF819E00132D3BA7DDDFE1CDC4FBB18E0A9615C62D` |
| `bge-small-zh-v1.5/model.safetensors` | `354763B9B1357BC9C44F62C6BE2276321081ED2567773608C0D0785B61D5A026` |
| `Qwen3-Reranker-0.6B/model.safetensors` | `27CD75A405B9C1B46B59ABFD88AAA209E6FED2A1972CDE9B70E7659537C5E65B` |

如果哈希不一致，应删除对应模型并从固定 revision 重新下载，不要继续加载。

### 6. 验证源码能够重新构建

```powershell
conda activate souldrive
python -m unittest discover -s tests -v
python -m compileall -q core scripts tests

cd souldrive-ui
npm.cmd run build
cd src-tauri
cargo check --locked
cd ..\..
```

也可以在项目根目录执行聚合检查：

```powershell
.\scripts\check.ps1
```

### 7. 启动桌面端

桌面端默认使用系统应用数据目录中的 `workspace/` 作为 PC 本地加密工作区，不需要插入可移动存储设备。首次启动会创建工作区，之后需要在界面中初始化或输入口令解锁。运行：

开发阶段直接在项目根目录双击 `启动SoulDrive.bat`。它启动 Vite、Tauri 和本地 sidecar 的完整开发链路；保持弹出的终端窗口运行，关闭终端即可停止 SoulDrive。

发布版工作区未完成口令初始化或尚未解锁时，涉及文档、索引和问答的 API 会保持锁定，这是预期行为。

### 8. 重新生成便携程序

只有在干净系统完成测试后，才重新生成 sidecar 和安装包：

```powershell
conda activate souldrive
.\scripts\package-sidecar.ps1
cd souldrive-ui
npm.cmd run tauri build
```

不要把生成出的 sidecar、安装包或 `target/` 提交到 Git。

### 9. 交给 Codex 恢复

重装后在 Codex 中打开克隆后的仓库，并发送：

```text
请先完整阅读 AGENTS.md 和 README.md，按 README 的“重装系统后从零恢复”章节检查环境、依赖、模型哈希和测试。不要复用旧系统的任何二进制、缓存、虚拟环境、node_modules、target、sidecar、索引或运行数据。循环修复直到后端测试、前端构建和 cargo check --locked 全部通过，再帮助我启动开发版桌面端。
```

恢复不依赖旧 Codex 对话；仓库文件和 README 就是交接材料。

## 本地开发

后端建议使用名为 `souldrive` 的 Conda Python 环境：

```powershell
conda activate souldrive
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

完整检查：

```powershell
.\scripts\check.ps1
```

安全与依赖审计：

```powershell
.\scripts\audit.ps1
```

前端构建：

```powershell
cd souldrive-ui
npm.cmd ci
npm.cmd run build
```

Tauri/Rust 检查：

```powershell
cd souldrive-ui\src-tauri
cargo check --locked
```

桌面端会通过 Tauri 启动本地 sidecar，并把随机 API 端口和 API token 传给前端；前端不应写死默认端口作为桌面端主路径。
非公开本地 API 默认要求 `X-SoulDrive-Token`。仅本地调试时可设置 `SOULDRIVE_ALLOW_UNAUTHENTICATED_API=1` 临时放开。
`test.http` 中的 `{{souldrive_token}}` 需要替换为当前 sidecar 运行时 token，或在 IDE HTTP Client 环境变量里配置。

## PC 默认形态与便携扩展

桌面端默认设置 `SOULDRIVE_WORKSPACE_MODE=local`，并把 `SOULDRIVE_WORKSPACE` 指向系统应用数据目录中的 `workspace/`。这是参赛演示和日常使用的主路径：数据、索引、审计与密钥材料均留在 PC 本地加密工作区内。

U 盘是可选的便携扩展。需要演示即插即用时，可将 `SOULDRIVE_WORKSPACE_MODE` 设置为 `removable`；后端会启用可移动盘监听并接管盘符根目录下的 `SoulDrive/` 工作区。不要把固定盘符写入业务代码，盘符变化由可移动盘探测逻辑处理。

推荐的 U 盘目录：

```text
<USB_DRIVE>:\
  SoulDrive.exe
  models\
  sidecars\
    souldrive-sidecar\
      souldrive-sidecar.exe
  SoulDrive\
    data\documents\
    data\papers\
    index\
    audit\
    models\
    runtime\
    config\workspace.json
```

打包 Python sidecar：

```powershell
.\scripts\package-sidecar.ps1
```

该脚本会把 Docling、RapidOCR、ONNX Runtime、SentenceTransformers、llama.cpp Python 绑定等离线解析和推理依赖收集进 `souldrive-ui\src-tauri\sidecars\`。完成后再构建 Tauri 桌面端。

## 工作区约定

SoulDrive 支持两种显式工作区模式：

- PC 默认模式：系统应用数据目录中的 `workspace/`，由桌面端显式传入 `SOULDRIVE_WORKSPACE`。
- 可移动扩展模式：可移动盘根目录下的 `SoulDrive/`，由监听器发现并接管。

两种模式都必须经过口令初始化和解锁，且都不会回退到项目目录或旧的本机 `souldrive_db/`。业务工作区包含：

- `data/documents/`：加密后的导入文档，是当前默认文档库。
- `data/papers/`：兼容旧版本的明文 PDF 目录，只用于读取历史资料。
- `index/`：Chroma、父子切片索引、关键词索引和图谱数据库。
- `audit/`：hash-chain 审计日志。
- `models/`：本地 GGUF 聊天模型和 BGE 嵌入模型。
- `runtime/`：工作区运行态、索引状态和临时文件。

当前项目资料入口支持 PDF、PNG、JPG/JPEG、WebP、MD、TXT、CSV、JSON、YAML/YML、LOG、DOCX、XLSX 和 PPTX，与后端已有格式一致。图片在索引阶段由本地 RapidOCR/ONNX Runtime 识别，OCR 文本、置信度和归一化位置坐标会作为加密索引证据；原图与元数据仍保存在加密文档库中，索引临时明文会在任务结束后清理。用户把已索引图片设为当前上下文后，问答请求会携带带时间戳的 `image` 感知事件。

图像 OCR 评测：

```powershell
conda activate souldrive
python scripts\eval_image_ocr.py --dataset <数据集.jsonl> --output-json eval\results\image_ocr_v0.1.json
```

评测输出包括识别成功率、关键词召回率、区域定位 IoU、OCR 置信度、P50/P95 延迟和 `<5s` 达标率。数据格式与比赛采集口径见 `docs/image_ocr_evaluation_protocol.md`。当前图像能力聚焦截图和扫描件中的文字证据，不把 OCR 宣传为通用图表语义理解；图表关系、无文字物体和复杂视觉推理仍需后续本地 VLM。

当前开发重点是先把这些内部链路做清楚：文档进入、索引更新、检索证据、回答生成、运行态锁定和审计记录。

## 运行产物与编码

- `runtime/`、`models/`、`node_modules/`、`souldrive-ui/dist/` 和 `souldrive-ui/src-tauri/target/` 都是本地运行或构建产物，不应提交。
- `souldrive_db/` 是旧版本机工作区残留，不再作为桌面端业务数据源；确认不需要历史数据后可以手动删除。
- 项目根下的 `data/`、`index/`、`audit/`、`config/workspace.json` 属于旧伪工作区痕迹，不应保留或提交。
- 中文源码和文档使用 UTF-8。PowerShell 读取中文文件时建议显式加 `-Encoding utf8`。

## 历史计划文档

`docs/superpowers/plans/` 下的计划文件是历史执行记录，里面的 checkbox 不作为当前项目完成度来源。判断当前状态时以 `python -m unittest discover -s tests -v`、`.\scripts\check.ps1` 和 `.\scripts\audit.ps1` 的实时结果为准。
