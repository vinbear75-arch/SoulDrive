# SoulDrive

SoulDrive 是一个面向私有资产的端侧知识引擎，核心目标是把本地知识文档的解析、索引、检索增强生成、证据链和知识图谱打磨稳定。当前演示与测试仍以论文作为默认切入口，但系统能力不局限于论文场景。

## 核心模块

- `core/`：本地 sidecar、工作区管理、文档解析、索引、检索、RAG、审计日志、模型运行配置。
- `souldrive-ui/`：React + Tauri 桌面界面，用于展示知识文档、运行态、问答、证据链和导图。
- `tests/`：后端核心逻辑的 `unittest` 测试。
- `config/enterprise-policy.json`：本地运行策略，保留 API 限流、CORS、lite/license 等基础约束。

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
git clone https://github.com/Vin-Bear/SoulDrive.git
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

hf download nreimers/mmarco-mMiniLMv2-L6-H384-v1 `
  --revision 4ceabf2d1e212e16da0d1fb94d5dea66a9a1cca0 `
  --local-dir models\mmarco-mMiniLMv2-L6-H384-v1
```

模型用途：

- `qwen2.5-3b-instruct-q4_k_m.gguf`：必需，本地聊天生成模型；来源为 [Qwen 官方 GGUF 仓库](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF)。
- `bge-small-zh-v1.5/`：必需，本地中文嵌入模型；来源为 [BAAI 官方模型仓库](https://huggingface.co/BAAI/bge-small-zh-v1.5)。
- `mmarco-mMiniLMv2-L6-H384-v1/`：可选，本地重排模型；来源为 [nreimers 模型仓库](https://huggingface.co/nreimers/mmarco-mMiniLMv2-L6-H384-v1)。缺失时系统关闭重排，但基础检索仍可工作。

校验关键权重：

```powershell
Get-FileHash -Algorithm SHA256 `
  models\qwen2.5-3b-instruct-q4_k_m.gguf, `
  models\bge-small-zh-v1.5\model.safetensors, `
  models\mmarco-mMiniLMv2-L6-H384-v1\pytorch_model.bin
```

预期 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `qwen2.5-3b-instruct-q4_k_m.gguf` | `626B4A6678B86442240E33DF819E00132D3BA7DDDFE1CDC4FBB18E0A9615C62D` |
| `bge-small-zh-v1.5/model.safetensors` | `354763B9B1357BC9C44F62C6BE2276321081ED2567773608C0D0785B61D5A026` |
| `mmarco-mMiniLMv2-L6-H384-v1/pytorch_model.bin` | `795F7056AC9D129F5A7FE4BB77CDA35CBEFA5E5E98B507CF8EE0B2C6DB122796` |

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

### 7. 启动开发版桌面端

插入一个可移动存储设备。桌面端会在盘符根目录自动创建或接管 `SoulDrive/` 工作区，并从项目根目录的 `models/` 同步所需模型。然后运行：

```powershell
conda activate souldrive
cd souldrive-ui
npm.cmd run tauri dev
```

调试构建会通过 `conda run -n souldrive python -m core.sidecar_runtime` 启动源码 sidecar。没有检测到可移动工作区时，API 会保持锁定，这是预期行为。

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

## 参赛便携形态

桌面端启动 sidecar 时会设置 `SOULDRIVE_WATCH_REMOVABLE=1`，后端会等待并接管已插入的移动存储工作区；工作区应位于可移动盘根目录下的 `SoulDrive/`。不要把固定盘符写入业务代码，盘符变化时由可移动盘探测逻辑处理。

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

SoulDrive 只把可移动盘根目录下的 `SoulDrive/` 作为业务工作区。桌面端未检测到该工作区时保持锁定，不会回退到项目目录或本机 `souldrive_db/`。

- `data/documents/`：加密后的导入文档，是当前默认文档库。
- `data/papers/`：兼容旧版本的明文 PDF 目录，只用于读取历史资料。
- `index/`：Chroma、父子切片索引、关键词索引和图谱数据库。
- `audit/`：hash-chain 审计日志。
- `models/`：本地 GGUF 聊天模型和 BGE 嵌入模型。
- `runtime/`：工作区运行态、索引状态和临时文件。

当前开发重点是先把这些内部链路做清楚：文档进入、索引更新、检索证据、回答生成、运行态锁定和审计记录。

## 运行产物与编码

- `runtime/`、`models/`、`node_modules/`、`souldrive-ui/dist/` 和 `souldrive-ui/src-tauri/target/` 都是本地运行或构建产物，不应提交。
- `souldrive_db/` 是旧版本机工作区残留，不再作为桌面端业务数据源；确认不需要历史数据后可以手动删除。
- 项目根下的 `data/`、`index/`、`audit/`、`config/workspace.json` 属于旧伪工作区痕迹，不应保留或提交。
- 中文源码和文档使用 UTF-8。PowerShell 读取中文文件时建议显式加 `-Encoding utf8`。

## 历史计划文档

`docs/superpowers/plans/` 下的计划文件是历史执行记录，里面的 checkbox 不作为当前项目完成度来源。判断当前状态时以 `python -m unittest discover -s tests -v`、`.\scripts\check.ps1` 和 `.\scripts\audit.ps1` 的实时结果为准。
