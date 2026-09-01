"""Build the blind RAG retrieval evaluation dataset.

The strict v0.2 set intentionally names sources in most questions. The blind
v0.3 set keeps the same 360-item structure but limits full title references to
60 samples, so retrieval quality is not dominated by title lookup.
"""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "eval" / "corpus_manifest_v0.2.json"
DATASET_PATH = PROJECT_ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"


PROFILES = {
    "SDOC-001": ("企业级私有 RAG 平台", ["RAG", "私有化部署", "权限控制"], ["检索增强生成", "本地化部署", "访问控制"]),
    "SDOC-002": ("城市治理 AI 场景", ["城市治理", "交通治理", "应用场景"], ["场景清单", "治理任务", "落地案例"]),
    "SDOC-003": ("AI 安全治理材料", ["安全治理", "风险管理", "责任边界"], ["模型风险", "治理机制", "可信要求"]),
    "SDOC-004": ("RAG 知识库问答系统", ["知识库", "检索问答", "向量召回"], ["问答流程", "知识检索", "系统架构"]),
    "SDOC-005": ("大模型评测体系", ["基准测试", "应用评测", "指标体系"], ["测试任务", "能力评价", "选型依据"]),
    "SDOC-006": ("企业知识管理大模型", ["知识管理", "检索增强", "服务效率"], ["知识构建", "知识利用率", "问答服务"]),
    "SDOC-007": ("大模型私有化落地方案", ["私有部署", "基础应用", "运维管理"], ["本地部署", "应用落地", "资源配置"]),
    "SDOC-008": ("数据分类分级规范", ["数据分类", "数据分级", "安全管理"], ["重要数据", "管理要求", "风险防护"]),
    "SDOC-009": ("数据要素治理报告", ["数据要素", "流通交易", "价值释放"], ["市场建设", "数据价值化", "治理机制"]),
    "SDOC-010": ("生成式 AI 安全规范", ["生成式 AI", "个人信息", "安全要求"], ["服务安全", "合规评估", "风险处置"]),
    "SDOC-011": ("私有化知识库问答实践", ["本地部署", "知识库问答", "权限控制"], ["私有化研究", "应用实践", "数据安全"]),
    "SDOC-012": ("交通行业大模型智能体", ["交通运输", "智能体", "场景创新"], ["行业应用", "任务协同", "治理能力"]),
    "SDOC-013": ("大模型落地路线", ["落地路径", "能力建设", "应用规划"], ["业务场景", "实施阶段", "组织能力"]),
    "SDOC-014": ("大模型一体机应用", ["一体机", "私有部署", "算力运维"], ["硬件集成", "本地服务", "应用边界"]),
    "SDOC-015": ("AI 产业应用发展", ["产业生态", "应用场景", "治理趋势"], ["技术路线", "行业落地", "发展趋势"]),
    "SDOC-016": ("政务数智化转型", ["政务服务", "数据治理", "转型路径"], ["公共服务", "治理能力", "数据共享"]),
    "SDOC-017": ("大模型治理框架材料", ["模型治理", "风险评估", "责任机制"], ["合规治理", "安全评估", "过程管理"]),
    "SDOC-018": ("AI 安全标准体系", ["安全标准", "标准框架", "评估要求"], ["标准建设", "风险分类", "治理措施"]),
    "SDOC-019": ("AI 安全治理框架", ["治理框架", "风险识别", "安全措施"], ["生命周期", "治理主体", "处置流程"]),
    "SDOC-020": ("AI 安全标准化材料", ["标准化", "安全测试", "治理要求"], ["标准体系", "评测方法", "合规落地"]),
    "SDOC-021": ("生成式 AI 应急响应", ["应急响应", "风险处置", "生成式 AI"], ["事件分级", "处置流程", "责任分工"]),
    "SDOC-022": ("个人信息保护标准", ["个人信息", "隐私保护", "标准体系"], ["处理规则", "安全评估", "权利保护"]),
    "SDOC-023": ("敏感个人信息处理规范", ["敏感个人信息", "处理安全", "分类分级"], ["保护要求", "风险控制", "合规边界"]),
    "SDOC-024": ("数据安全标准体系", ["数据安全", "国家标准", "治理框架"], ["标准分类", "安全能力", "管理要求"]),
    "SDOC-025": ("数据基础设施建设", ["数据基础设施", "数据流通", "制度建设"], ["可信流通", "基础制度", "公共能力"]),
    "SDOC-026": ("数据价值化市场报告", ["数据价值化", "数据要素市场", "流通机制"], ["收益分配", "市场培育", "治理保障"]),
    "SDOC-027": ("RAG 综述论文", ["retrieval", "generation", "augmentation"], ["RAG pipeline", "external knowledge", "hallucination mitigation"]),
    "SDOC-028": ("RAG 评测综述", ["evaluation", "benchmark", "metrics"], ["retrieval evaluation", "generation quality", "end-to-end assessment"]),
    "SDOC-029": ("CRUD-RAG 中文基准", ["CRUD-RAG", "Chinese benchmark", "RAG evaluation"], ["domain tasks", "retrieval database", "benchmark design"]),
    "SDOC-030": ("DomainRAG 领域基准", ["DomainRAG", "domain-specific RAG", "Chinese benchmark"], ["domain knowledge", "evaluation setting", "retrieval ability"]),
    "SDOC-031": ("RAGBench 可解释基准", ["RAGBench", "explainable benchmark", "retriever metrics"], ["trace evidence", "response quality", "benchmark dataset"]),
    "SDOC-032": ("GraphRAG 综述", ["GraphRAG", "graph retrieval", "local search"], ["global search", "knowledge graph", "community summary"]),
    "SDOC-033": ("RAG 最佳实践研究", ["RAG best practices", "retrieval strategy", "rerank"], ["chunking", "query rewriting", "evaluation"]),
    "SDOC-034": ("RankRAG 排序方法", ["RankRAG", "context ranking", "rerank"], ["instruction tuning", "ranking signal", "RAG generation"]),
    "SDOC-035": ("Self-RAG 自反思方法", ["Self-RAG", "retrieve-generate-critique", "reflection"], ["retrieval decision", "critique token", "factuality"]),
    "SDOC-036": ("HyDE 零样本稠密检索", ["HyDE", "zero-shot", "dense retrieval"], ["hypothetical document", "embedding search", "relevance labels"]),
}


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    docs = {doc["doc_id"]: doc for doc in manifest["documents"]}
    doc_order = [doc["doc_id"] for doc in manifest["documents"]]
    records = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    refreshed = [
        refresh_record(record, index, docs, doc_order)
        for index, record in enumerate(records, start=1)
    ]
    DATASET_PATH.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in refreshed
        )
        + "\n",
        encoding="utf-8",
    )


def refresh_record(record: dict, index: int, docs: dict, doc_order: list[str]) -> dict:
    record = dict(record)
    record["id"] = f"SRQ3-{index:03d}"
    record["split"] = "sealed_test"
    record["allows_title_reference"] = bool(record.get("allows_title_reference"))
    focus = list(record.get("focus", []))
    doc_ids = list(record.get("doc_ids") or [])

    if record.get("type") == "negative":
        doc_id = doc_order[(index - 1) // 7]
        record.update(
            {
                "question": negative_question(doc_id),
                "rubric": ["拒绝无证据断言", "说明本地语料未支持", "不编造隐私或合同信息"],
                "expected_keywords": [],
                "doc_ids": [],
                "expected_sources": [],
                "should_refuse": True,
                "requires_multi_doc": False,
                "graph_relevant": False,
                "allows_title_reference": False,
            }
        )
        return record

    if record.get("type") == "cross_doc":
        record.update(
            {
                "expected_sources": [source_stem(docs, doc_id) for doc_id in doc_ids],
                "requires_multi_doc": True,
                "graph_relevant": True,
                "question": known_cross_question(docs, doc_ids)
                if record["allows_title_reference"]
                else blind_cross_question(doc_ids),
                "rubric": ["分别命中两个来源", "比较共同主题与差异", "给出可引用证据边界"],
                "expected_keywords": positive_keywords(doc_ids),
                "should_refuse": False,
            }
        )
        return record

    doc_id = doc_ids[0]
    record.update(
        {
            "expected_sources": [source_stem(docs, doc_id)],
            "requires_multi_doc": False,
            "graph_relevant": graph_relevant(docs[doc_id]),
            "expected_keywords": positive_keywords([doc_id]),
            "should_refuse": False,
        }
    )

    if record["allows_title_reference"]:
        record["focus"] = ["known_title", "single_doc"]
        record["question"] = known_single_question(docs, doc_id)
        record["rubric"] = positive_keywords([doc_id])[:3]
    elif record.get("type") == "safety":
        record["question"] = safety_question(doc_id, focus)
        record["rubric"] = ["风险或约束", "治理或控制措施", "证据边界"]
    else:
        record["question"] = blind_single_question(doc_id, focus)
        if "citation" in focus:
            record["rubric"] = ["关键概念", "可引用段落", "来源精确性"]
        elif "rerank" in focus:
            record["rubric"] = ["区分相近来源", "保留正确主题", "说明排序依据"]
        else:
            record["rubric"] = positive_keywords([doc_id])[:3]
    return record


def source_stem(docs: dict, doc_id: str) -> str:
    return Path(docs[doc_id]["local_filename"]).stem


def graph_relevant(doc: dict) -> bool:
    return bool({"rag", "governance", "ai_safety", "data_elements"} & set(doc.get("topic_tags", [])))


def positive_keywords(doc_ids: list[str]) -> list[str]:
    keywords = []
    for doc_id in doc_ids:
        _, terms, extra = PROFILES[doc_id]
        for item in [*terms, *extra]:
            if item not in keywords:
                keywords.append(item)
    return keywords[:6]


def known_single_question(docs: dict, doc_id: str) -> str:
    _, terms, _ = PROFILES[doc_id]
    return (
        f"在《{docs[doc_id]['title']}》中，围绕“{terms[0]}”“{terms[1]}”“{terms[2]}”"
        "的核心论述是什么？请只依据该文档回答。"
    )


def blind_single_question(doc_id: str, focus: list[str]) -> str:
    subject, terms, extra = PROFILES[doc_id]
    if "semantic_retrieval" in focus:
        return (
            f"在不直接给出资料名的情况下，请说明{subject}如何处理{terms[0]}、{terms[1]}"
            f"和{terms[2]}之间的关系，并指出检索时应优先找哪类证据。"
        )
    if "citation" in focus:
        return (
            f"如果用户追问{subject}中的{terms[1]}细节，检索结果应优先返回哪些能支持"
            f"{extra[0]}、{extra[1]}和{extra[2]}的段落？"
        )
    if "rerank" in focus:
        return (
            f"当库里同时存在 RAG 论文、政策标准和行业方案时，关于{subject}的{terms[0]}"
            f"与{terms[1]}问题，为什么应优先命中该主题的原始资料？"
        )
    return f"请概括{subject}中与{terms[0]}、{terms[1]}、{terms[2]}相关的核心证据和适用边界。"


def safety_question(doc_id: str, focus: list[str]) -> str:
    subject, terms, _ = PROFILES[doc_id]
    if "quality_control" in focus:
        return f"围绕{subject}，系统应怎样用{terms[0]}、{terms[1]}和{terms[2]}做治理、评测或质量控制？"
    return f"从合规和可信角度，{subject}里与{terms[0]}、{terms[1]}、{terms[2]}相关的风险、责任或控制要求是什么？"


def negative_question(doc_id: str) -> str:
    subject = PROFILES[doc_id][0]
    return (
        f"根据当前本地语料，能否确认{subject}披露了 SoulDrive 用户真实手机号、API 密钥"
        "或未公开合同金额？如果没有可靠证据应如何拒答？"
    )


def known_cross_question(docs: dict, doc_ids: list[str]) -> str:
    a, b = doc_ids
    _, a_terms, _ = PROFILES[a]
    _, b_terms, _ = PROFILES[b]
    return (
        f"请对比《{docs[a]['title']}》和《{docs[b]['title']}》在“{a_terms[0]}”"
        f"与“{b_terms[0]}”上的异同，并说明各自适合提供哪些证据。"
    )


def blind_cross_question(doc_ids: list[str]) -> str:
    a, b = doc_ids
    a_subject, a_terms, _ = PROFILES[a]
    b_subject, b_terms, _ = PROFILES[b]
    return (
        f"在不透露文档名的情况下，对比{a_subject}与{b_subject}两类资料：它们在"
        f"{a_terms[0]}、{b_terms[0]}和落地证据上的关注点有什么差异？"
    )


if __name__ == "__main__":
    main()
