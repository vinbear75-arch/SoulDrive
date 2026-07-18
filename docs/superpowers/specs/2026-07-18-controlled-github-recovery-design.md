# SoulDrive 受控 GitHub 迁移与重装恢复设计

## 目标

在当前电脑疑似受恶意程序影响的前提下，只把经过审查的 SoulDrive 源码、测试、配置与恢复文档上传到现有 GitHub 仓库。重装系统后，从 GitHub 克隆代码、重新下载并校验模型、重新安装依赖和生成构建产物，最终恢复本地运行能力。

## 发布边界

- 保留本地 `main` 已有但尚未推送的 10 个提交。
- 保留当前全部已修改源码和 7 个新增评测/脚本文件。
- 不上传 `models/`、`runtime/`、`souldrive_db/`、业务文档、索引、审计数据、密钥、虚拟环境、`node_modules/`、Tauri `target/`、sidecar 和其他构建产物。
- 不重写 Git 历史；历史最大对象远小于 GitHub 大文件限制，且未发现模型权重。

## 安全审计

- 检查 Git hooks、仓库本地配置、可执行文件、大文件和高置信度凭据特征。
- 审查全部未提交差异，重点关注 PowerShell、Python、Rust 和前端脚本中的进程启动、下载执行、持久化、编码载荷、凭据访问和异常外连。
- 未知二进制文件不运行、不提交；打包 sidecar 必须在重装后的干净系统重新生成。
- 当前系统上的静态审计不能证明绝对无污染。重装后应重新扫描源码，并撤销这台电脑使用过的 GitHub 凭据。

## 恢复入口

根目录 `README.md` 是重装后的唯一操作入口，包含：

- Python 3.10、Node.js/npm、Rust/Cargo 和 Git 的环境基线。
- Conda 环境、Python 依赖、前端依赖和 Rust 依赖的恢复命令。
- Qwen 聊天模型、BGE 嵌入模型和可选 mMARCO 重排模型的固定来源、固定版本、目标目录和 SHA-256。
- 后端测试、前端构建、Cargo 检查、模型检查、Tauri 开发启动和重新打包步骤。
- 给 Codex 的恢复提示，要求先阅读 `AGENTS.md` 与 README，再按验证清单执行。

## 验证与发布

发布前必须完成：

1. 后端完整 `unittest`。
2. Python `compileall`。
3. 前端锁文件安装状态下的生产构建。
4. Rust `cargo check --locked`。
5. Git 暂存边界、大文件、凭据和忽略规则复核。
6. 创建单一恢复提交并推送 `main` 到现有 `origin`。
7. 对比远程 `main` 与本地提交标识，确认上传完成。

## 已知限制

- GitHub 只恢复源代码和重新构建能力，不恢复旧知识库、索引、运行态或密钥。
- 模型不进入 Git；恢复依赖 Hugging Face 上固定版本的模型快照。
- GPU 加速依赖重装后机器的 CUDA、驱动和 `llama-cpp-python` 构建方式；CPU 回退仍应可用。
