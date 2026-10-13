# GraphRAG 全局回答逐 claim 来源多样性门

日期：2026-10-11。本轮对照 [Microsoft GraphRAG global search（固定 commit）](https://github.com/microsoft/graphrag/blob/769542fbf1d8e5b4c6a8677fefc34621c87894c5/docs/query/global_search.md) 的跨语料聚合语义，修复 SoulDrive 全局 GraphRAG 只检查“整段答案累计引用了两份文档”的证据缺口。

## 缺口与 B0 反例

全局 GraphRAG 的回答可能包含“跨资料共同结论”。旧门只统计整段答案的 distinct source count，因此下面的答案会被接受：第一条共同结论只引用资料 A，第二条独立补充同时引用 A/B；总数虽然是 2，但共同结论没有两份来源支持。

用旧调用方式复现的结果为 `valid=True`、`cited_source_count=2`、`reason=answer citations validated`。这不是答案质量达标，而是暴露了门只看总量的错误边界。

新回归 `tests/test_answer_quality.py::test_validate_answer_citations_can_require_source_diversity_per_global_claim` 在候选实现前先失败（接口尚未存在）；随后补入最小实现，并用真实 `RAGEngine` 全局回退测试验证失败原因和用户可见提示。

## 最小改动

- `validate_answer_citations()` 新增可选 `require_source_count_per_claim`，默认关闭，保持普通 hybrid/local/单文档回答兼容。
- 仅当 `RAGEngine` 已判定为需要全局 GraphRAG 多来源综合时开启；每个事实 claim 按 `answer_claim_units()` 分组，按稳定 `document_id/source_document_ids` 或作用域来源键计算来源数。
- 任何 claim 未达到要求时返回 `answer claim source diversity insufficient`，带 claim 序号和计数，不泄露正文；来源定位脚手架不作为事实 claim 重复要求。
- 全局回答因此进入已有有界重试；仍失败时回退为“各来源分别可定位的证据”，新增透明标签“逐条结论的来源多样性校验”，不把单来源材料强行归纳成共同结论。
- 没有引入 Agent 框架、自由循环、额外检索轮次或新的外部服务。

## 验证记录

- B0 旧行为探针：错误答案形态被 `valid=True` 接受，累计来源数为 2。
- 候选定向：`tests/test_answer_quality.py` `48 passed`；全局回退真实链路 `1 passed`。
- 扩大回归：回答质量、RAG prompting、GraphRAG、GraphRAG acceptance 合计 `226 passed in 2.33s`；`compileall` 通过。
- 全量 `scripts/check.ps1`：退出码 0；后端 `1734 passed, 1 warning in 441.46s`；记忆质量 `7/7` 场景、`8/8` 查询、`memory_recall_at_k=1.0`、stale/forbidden leakage `0.0`；GraphRAG 路由 `4/4`；workflow 角色 `6/6`；前端生产构建成功、UI 脚本 `66 passed`；锁定 Cargo 与编译检查通过。
- 全量日志：`build/quality-gates/global-claim-source-diversity-full-check-20261010.log`。
- 当前工作区文件 SHA-256：`core/answer_quality.py`=`90b41460837b91fb2dc41e6a5253d7af1c5f65e38b0fb12d6966ac07f1259f45`；`core/rag_engine.py`=`6703eb7d3a0c101cf64680b3d7d1211a2e65b65010ad6ced08ff0d0666133e78`；`tests/test_answer_quality.py`=`0639e353e93aa58c4593dbf8ff0d1e717e94232dad6a304cfa3df660e674ceab`；`tests/test_rag_prompting.py`=`05b46e878b1f23ee079ced06a6f41c31c4715e449386420670c08224e43bfd5c`。

## 边界

本轮只证明全局 GraphRAG 的来源计数合同和失败回退行为；不证明 claim 与来源之间的完整语义蕴含、事实正确率、跨文档综合答案质量、召回率、引用完整性或真实模型收益。逐 claim 双来源是保守门，不等价于两份来源真的互相独立或结论已经被实验验证。现有冻结评测集、30 条待审核业务种子、gold、训练数据和提示词调参均未改变。
