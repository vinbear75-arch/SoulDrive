# 发行候选：sidecar 资源与内嵌代码只读复核

日期：2026-10-13。范围：当前本机文件、打包配置、既有二进制归档；未重打包、安装或启动用户工作区，未删减任何运行依赖。

## 已由当前状态证明的事实

| 对象 | 当前证据 |
| --- | --- |
| sidecar 目录 | `souldrive-ui/src-tauri/sidecars/souldrive-sidecar/`，9702 个文件，合计 **3,555,066,235 bytes**（十进制约 3.56 GB） |
| torch 资源 | 2201 个文件，**2,729,265,114 bytes**；大文件主要是 CUDA/torch DLL，但也有 CPU 运行库，不能按名称假定可删 |
| 最大三个文件 | `torch/lib/cublasLt64_13.dll` 477,896,816 bytes；`torch/lib/torch_cuda.dll` 408,274,432 bytes；`torch/lib/cufft64_12.dll` 284,331,040 bytes（均位于 `_internal/`） |
| sidecar exe | 66,411,225 bytes，文件时间 2026-08-22；SHA-256 `17278d4e1e1f3aa2a4c8e22cd4f0e1e01d28aabd6d548c71333d2258780a5dce` |
| 既有 Tauri release exe | 11,257,344 bytes，文件时间 2026-09-09；SHA-256 `e22637a1dff06afe3440ccfb68a164b0aee41d9a0f9badeb63009a1fe6a79ad9` |
| 当前 bundle 配置 | 目标 NSIS，将 `sidecars/**/*` 作为资源；配置 SHA-256 `911d135031f5f8f987bd70f4343d4c8748c05ea0cb8db10d492dae578df51f7d` |
| 当前打包入口 | `scripts/package-sidecar.ps1`，使用固定 dist/work/spec 路径及 `--noconfirm --clean --onedir`；SHA-256 `2755dfa694d4d76ee123cfcf0c601ac384e87454a2602adbe155e2893d1ea9d7` |

文件时间只能提示新旧，不能单独证明代码版本。因此进一步使用本机已安装 **PyInstaller 6.20.0** 的 `CArchiveReader` 只读打开 exe 的 `PYZ.pyz`，解压/反序列化代码对象用于清单查看，不执行其中的模块或二进制。

当前 PYZ 含 13147 个模块，包括 `core.answer_quality`、RAG、MCP、PPT 和 TaskJournal。`core.answer_quality` 的顶层函数清单没有当前源码中的 `_validate_claim_support`、`_fact_tokens`、`_normalize_fact_token`；该二进制不能作为本轮数字证据修复或当前语义引用门已随包交付的证据。清单输出见 `build/quality-gates/release-embedded-code-audit-20261013.log`，SHA-256 `a2365d05d19bc0bc97ddb40f3d0f72eb13a6e128f77aa0e0dc1ceb04ac6d6021`。这项结论来自内嵌代码，不只是文件日期；只读核查结束后两个 exe 的字节指纹再次核对未变。

## 外部资料与诊断边界

- [NSIS 大安装包说明](https://nsis.sourceforge.io/Failure_when_compiling_a_big_installer)指出约 2 GB 的理论限制，并建议将大数据放在安装器之外。历史报告中的 `makensis` 映射错误与当前 3.56 GB 资源树具有一致的风险背景；本轮没有再次执行 makensis，不能把它写成新的编译失败记录。
- [Tauri Windows installer 文档](https://v2.tauri.app/distribute/windows-installer/)区分 NSIS/MSI，并提示 MSI 的 `light.exe` 错误还可能涉及 VBSCRIPT 可选功能。**仅凭历史 light.exe 失败不能证明原因与 NSIS 相同**；当前未取得对应详细错误日志，也未核实/更改 Windows 可选功能。
- [PyInstaller 归档说明](https://pyinstaller.org/en/stable/advanced-topics.html)作为读取归档机制参考；本机实际工具版本与文档当前 stable 版本分别记录，不混同。

## 下一步及不越过的门

1. 先在新的隔离输出目录重建当前源码的 sidecar 候选，保留现有包；在构建前后核对源码指纹，再核对新 PYZ 的关键函数/代码。不能直接运行现有固定输出、`--noconfirm` 的入口覆盖原产物。
2. 新候选先验证启动/锁定与必要导入，使用虚构密钥的独立测试工作区，不用用户真实数据或真实模型服务做破坏性演示。
3. NSIS 若仍受规模限制，再提交外置 sidecar/分体包或有证据的依赖裁剪选择。改变分发形态、删减 CUDA/PyTorch、修改系统可选功能或覆盖原包不是本次只读核查的授权。
4. MSI 先取得具体失败日志及只读环境信息，再判断是否是 VBSCRIPT、容量或其他错误；未核实不修改系统。
5. 即使新包构建成功，真实安装后的 Tauri 解锁、项目主流程、取消/恢复、WPS 中文布局与可编辑性仍须分别验收。源码完整回归不替代新包测试，既有 exe 也不代表当前源码。

这份核查改变后续优先级：需要先建立“当前源码 -> 当前 sidecar -> 当前桌面候选”的可证明版本链，而不是继续拿旧二进制解释新测试结果。goal 保持 active，E1/E7 不标完成。

后续增量：第四十七轮已在独立新目录成功构建当前源码 sidecar，8 个关键模块编译代码对象匹配，并通过新/已有隔离工作区两次真实进程口令与重启检查；旧包未覆盖。详见 `2026-10-13-current-source-sidecar-candidate.md`。该进展不改变本文件对旧包和真实安装/UI 门未闭合的判断。
