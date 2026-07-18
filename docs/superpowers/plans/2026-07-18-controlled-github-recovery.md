# SoulDrive 受控 GitHub 迁移与重装恢复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将经过静态审计、可在重装后重新构建的 SoulDrive 源码完整上传到现有 GitHub `main`。

**Architecture:** 保留现有 Git 历史和全部当前源码改动，通过 `.gitignore` 与暂存复核隔离模型、业务数据和构建产物。README 固定环境恢复、模型快照、SHA-256 与验证命令，重装后只从源码和可信模型源重建。

**Tech Stack:** Python 3.10、Conda、FastAPI、React、TypeScript、Tauri、Rust、Hugging Face Hub、Git/GitHub

---

### Task 1: 审计发布边界

**Files:**
- Inspect: `.git/config`
- Inspect: `.git/hooks/`
- Inspect: `.gitignore`
- Inspect: 全部 `git diff` 与未跟踪文件

- [ ] **Step 1: 核对 Git 状态、远程和 hooks**

```powershell
git status --short --branch
git remote -v
Get-ChildItem .git\hooks -File
```

Expected: `origin` 指向 `Vin-Bear/SoulDrive`，hooks 只有未启用的 `.sample` 文件。

- [ ] **Step 2: 核对大文件、凭据和危险行为特征**

```powershell
git diff --check
git ls-files
git ls-files --others --exclude-standard
```

Expected: 没有模型、密钥、sidecar 或构建产物进入候选文件。

### Task 2: 写入重装恢复文档

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/specs/2026-07-18-controlled-github-recovery-design.md`
- Create: `docs/superpowers/plans/2026-07-18-controlled-github-recovery.md`

- [ ] **Step 1: 写入环境和依赖恢复命令**

README 使用 `conda create -n souldrive python=3.10`、`python -m pip install -r requirements.txt`、`npm.cmd ci` 和 `cargo check --locked`。

- [ ] **Step 2: 写入固定模型快照与校验值**

README 固定三个 Hugging Face revision，并记录三个关键权重的 SHA-256。

- [ ] **Step 3: 写入 Codex 恢复入口和安全边界**

README 明确不复制旧系统上的二进制、依赖目录、缓存、索引和运行数据。

### Task 3: 验证可重建性

**Files:**
- Verify: `core/`
- Verify: `tests/`
- Verify: `souldrive-ui/`

- [ ] **Step 1: 运行后端测试**

```powershell
python -m unittest discover -s tests -v
```

Expected: 全部测试通过。

- [ ] **Step 2: 编译 Python 源码**

```powershell
python -m compileall -q core scripts tests
```

Expected: 退出码为 0。

- [ ] **Step 3: 构建前端**

```powershell
cd souldrive-ui
npm.cmd run build
```

Expected: TypeScript 与 Vite 构建成功。

- [ ] **Step 4: 检查 Rust**

```powershell
cd souldrive-ui\src-tauri
cargo check --locked
```

Expected: Cargo 检查成功且不改写 `Cargo.lock`。

### Task 4: 提交并上传

**Files:**
- Stage: 全部已审核源码、测试、配置、评测和文档

- [ ] **Step 1: 暂存并复核**

```powershell
git add -A
git status --short
git diff --cached --check
```

Expected: 暂存区不含模型、业务数据、依赖目录、可执行文件或构建产物。

- [ ] **Step 2: 创建恢复提交**

```powershell
git commit -m "chore: prepare reproducible recovery snapshot"
```

Expected: 生成一个包含当前全部审核后成果的提交。

- [ ] **Step 3: 推送并核验远程**

```powershell
git push origin main
git ls-remote origin refs/heads/main
git rev-parse HEAD
```

Expected: 远程 `main` 与本地 `HEAD` 的提交标识一致。
