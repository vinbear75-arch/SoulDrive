# SoulDrive 多模态工作流与运行时项目范围优化记录

日期：2026-09-10

## 本轮结论

多模态工作流目录、工作流计划、直接推理和上传推理现在都会在 API 边界核验传入的项目是否存在。未知 `project_id` 统一返回 404，不再把不存在的命名空间当成可规划/可标注的项目继续运行。

这不是把“项目存在”误当成“证据已授权”：文档是否已索引、是否属于当前项目、Playbook 是否审核通过仍由后续工作流与 RAG 门禁分别判断。

## 外部实践依据

- LangGraph 的持久化文档将 thread/store 作为状态和长期记忆的持久命名空间，并要求以明确的 thread/namespace 读取和恢复状态：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
- Open WebUI 的知识文件工具在解析文件前先计算可访问知识库，再限制到模型绑定或授权资源；客户端提供的资源标识不能单独扩大访问范围：[Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py)。
- Open WebUI 的权限文档将资源访问控制作为独立层，而不是只依赖前端显示：[Open WebUI RBAC permissions](https://github.com/open-webui/docs/blob/main/docs/features/authentication-access/rbac/permissions.md)。

本轮只借鉴“服务端先核验命名空间/资源存在与范围”的边界原则，没有引入外部框架或改变本机单用户部署边界。

## 实现范围

1. 新增统一的 `_unknown_project_response` 边界辅助函数，复用现有项目目录查询。
2. `/multimodal/workflows` 与 `/multimodal/workflows/plan` 在读取 Playbook、核验文档和加载实验对比前拒绝未知项目。
3. `/multimodal/runtime/infer` 与 `/multimodal/runtime/infer-upload` 在生成感知事件前拒绝未知项目，避免把事件挂到不存在的项目空间。
4. 保留纯函数 `plan_workflow` 和 `soulalign_decide` 的无数据库特性；本轮没有把“项目存在性”硬塞进纯逻辑层。

## 验收证据

- 先添加失败测试：旧实现对未知项目的工作流目录、工作流计划和两类运行时推理均返回 200。
- `tests/test_multimodal_workflows.py`：28 passed，1 个既有 Starlette/httpx 弃用警告。
- `tests/test_topic5_runtime.py` 与工作流模块合计：71 passed，1 个同类弃用警告。
- `git diff --check`：未发现空白错误；输出中的 LF/CRLF 为 Windows 工作区既有换行提示。

全量 `scripts/check.ps1` 随后通过：后端 `1056 passed, 1 warning`；前端生产构建通过，7 组 UI 测试合计 `46 passed`；Tauri `cargo check --locked` 通过。唯一既有警告仍是 Starlette/httpx 弃用提示，前端仍有单个 575.68 kB 产物体积提示。

## 边界与后续

- 该门禁只证明项目目录存在，不证明项目中有可用证据，也不代表模型回答正确；RAG 证据范围和引用校验仍是独立验收项。
- `/multimodal/decide` 仍是无副作用的目标评分函数，本轮没有把仅用于评分的字符串标识升级为数据库依赖；若未来它写入或读取项目资源，应复用同一门禁。
- 下一轮继续检查剩余客户端可提交的项目标识和多模态事件回传路径，尤其是事件是否能在后续 `/chat` 中绕过会话项目绑定。
