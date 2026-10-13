# 云模型配置与凭据观测边界优化

日期：2026-09-13

## 对照实践

- [LiteLLM 配置示例](https://github.com/BerriAI/litellm/blob/main/proxy_server_config.yaml) 采用 `os.environ/...` 形式引用 API key，配置与秘密值分离。
- [LiteLLM API key 未脱敏输出问题](https://github.com/BerriAI/litellm/issues/34274) 说明模型代理的 verbose/诊断路径也可能成为凭据泄露面；SoulDrive 继续只返回 `api_key_configured`，不把 key 放进公有配置响应。
- [LiteLLM 配置仓库](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/proxy_config.yaml) 将模型路由配置、凭据引用和观测回调分开；本机版本不引入代理服务，仅修正本地保存完成后的观测故障边界。

## 失败优先发现

`POST /cloud-model/config` 已经通过 `save_cloud_model_config` 保存配置、必要时释放当前本地生成模型后，仍直接写 `cloud_model.configured`。审计 sink 故障会把已保存的配置变成 500，用户重试时可能误以为保存失败；同时这类异常不应把 API key 带回响应。

## 本轮修改

- 配置保存和本地生成模型释放保持原顺序与错误语义。
- `cloud_model.configured` 改为 best-effort；保存后的公有配置仍返回 200，审计故障只附加固定 warning。
- 保持 API key 不进入 `public_dict()`、响应文本和审计 payload；详细异常只进入本机日志。

## 验收证据

- 新增审计故障回归，确认配置已保存时仍返回模型配置和 warning，响应不包含 API key。
- 云模型完整回归 `11 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1322 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是云端模型可用性、网络重试、供应商 exactly-once 或凭据轮换实现。
- 这不是 LiteLLM 接入、远程代理或秘密管理器接入；本机凭据仍由现有 Credential/配置机制管理。
- 这不证明真实云模型请求、GPU 释放或 Tauri/UI/E2E 行为。
