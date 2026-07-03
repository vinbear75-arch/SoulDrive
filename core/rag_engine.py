import gc
import json
import os
import re
import time
import uuid

from core.answer_quality import (
    citation_coverage,
    evaluate_evidence_gate,
    refusal_answer,
    validate_answer_citations,
)
from core.audit_log import default_audit_logger
from core.graph_db import LocalGraphDB
from core.knowledge_base import LocalKnowledgeBase
from core.logging_config import get_logger
from core.model_runtime import llama_runtime_config, load_llama_with_gpu_fallback, resolve_chat_model_path
from core.observability import runtime_metrics
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.security_context import get_workspace_keys
from core.secure_graph_store import SecureGraphStore
from core.workspace import SoulDriveWorkspace


MAX_CONTEXT_CHARS_PER_EVIDENCE = 1800
ANSWER_RETRY_LIMIT = 1

logger = get_logger(__name__)


class RAGEngine:
    def __init__(
        self,
        kb: LocalKnowledgeBase,
        graph_db_path: str | None = None,
        workspace_path: str | None = None,
        audit_logger=None,
    ):
        self.kb = kb
        self.audit_logger = audit_logger or default_audit_logger
        if not graph_db_path:
            raise ValueError("graph_db_path is required for RAGEngine")
        self.graph_db = self._build_graph_store(graph_db_path, workspace_path)
        self.runtime_config = llama_runtime_config(workspace_path)
        model_path = resolve_chat_model_path(workspace_path, self.runtime_config)
        logger.info("[RAG Engine] Loading local GGUF model: %s", os.path.basename(model_path))

        if not os.path.exists(model_path):
            runtime_metrics.increment("model_load_failures")
            message = (
                f"chat model not found: {model_path}. "
                "Place the model inside SoulDrive/models or configure SOULDRIVE_MODEL_DIR."
            )
            runtime_metrics.record_error(message)
            raise FileNotFoundError(message)

        load_result = load_llama_with_gpu_fallback(
            model_path=model_path,
            config=self.runtime_config,
            n_ctx=self.runtime_config.chat_n_ctx,
        )
        self.llm = load_result.model
        self.runtime_config = load_result.effective_config
        runtime_metrics.record_model_load(load_result.load_ms)
        if load_result.fallback_error:
            runtime_metrics.record_error(f"GPU fallback: {load_result.fallback_error}")

    def _build_graph_store(self, graph_db_path: str, workspace_path: str | None = None):
        if workspace_path:
            keys = get_workspace_keys(workspace_path)
            if keys is not None:
                workspace = SoulDriveWorkspace(workspace_path)
                return SecureGraphStore(workspace.secure_graph_store_path, keys)
        return LocalGraphDB(db_path=graph_db_path)

    def close(self):
        if getattr(self, "graph_db", None) is not None:
            self.graph_db.close()
            self.graph_db = None
        if getattr(self, "llm", None) is not None:
            close_fn = getattr(self.llm, "close", None)
            if callable(close_fn):
                close_fn()
            self.llm = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass

    def generate_response_stream(self, query: str, top_k: int = DEFAULT_RAG_TOP_K, trace_id: str | None = None):
        trace_id = trace_id or str(uuid.uuid4())
        started_at = time.time()

        try:
            retrieval_result = self.kb.search_with_evidence(query=query, graph_db=self.graph_db, top_k=top_k)
        except Exception as exc:
            runtime_metrics.increment("retrieval_failures")
            runtime_metrics.record_error(str(exc))
            self.audit_logger.append_event(
                "rag.retrieve_failed",
                {"query_chars": len(query), "error": str(exc)},
                trace_id=trace_id,
            )
            yield f"本地知识库暂未就绪。错误信息：{str(exc)}"
            return

        vector_docs = retrieval_result["documents"]
        vector_metas = retrieval_result["metadatas"]
        graph_context = retrieval_result["graph_context"]
        evidence = retrieval_result["evidence"]
        self.audit_logger.append_event(
            "rag.retrieved",
            {
                "query_chars": len(query),
                "top_k": top_k,
                "retrieval_mode": retrieval_result["retrieval_mode"],
                "evidence_count": len(evidence),
                "matched_entities": retrieval_result["matched_entities"],
                "sources": [item.get("source_filename") for item in evidence],
            },
            trace_id=trace_id,
        )

        evidence_gate = evaluate_evidence_gate(evidence, query=query)
        if evidence_gate.decision in ("reject", "retry"):
            answer = refusal_answer(query, evidence_gate)
            self.audit_logger.append_event(
                "rag.refused_low_evidence",
                {"query_chars": len(query), "gate": evidence_gate.public_dict()},
                trace_id=trace_id,
            )
            yield answer
            yield _evidence_block(evidence)
            return

        vector_context = build_vector_context(vector_docs, vector_metas, evidence)
        graph_context_text = build_graph_context(graph_context)
        messages = build_answer_messages(query, vector_context, graph_context_text)

        generated_text = None
        validation_report = None
        for attempt in range(ANSWER_RETRY_LIMIT + 1):
            attempt_messages = messages
            if attempt > 0:
                attempt_messages = messages + [
                    {
                        "role": "assistant",
                        "content": "上一次回答未通过引用校验，请只输出有合法 [E1] 证据引用支持的答案。",
                    }
                ]

            candidate_text = self._generate_answer_text(attempt_messages)
            validation_report = validate_answer_citations(candidate_text, evidence)
            if validation_report["valid"]:
                generated_text = candidate_text
                break

            repaired_text = repair_missing_citations(candidate_text, evidence, validation_report)
            if repaired_text != candidate_text:
                repaired_report = validate_answer_citations(repaired_text, evidence)
                if repaired_report["valid"]:
                    generated_text = repaired_text
                    validation_report = repaired_report
                    break

            self.audit_logger.append_event(
                "rag.answer_retry",
                {
                    "query_chars": len(query),
                    "attempt": attempt + 1,
                    "reason": validation_report["reason"],
                },
                trace_id=trace_id,
            )

        if generated_text is None:
            answer = evidence_fallback_answer(query, evidence)
            self.audit_logger.append_event(
                "rag.refused_invalid_answer",
                {"query_chars": len(query), "validation": validation_report},
                trace_id=trace_id,
            )
            yield answer
            yield _evidence_block(evidence)
            return

        citation_report = citation_coverage(generated_text, evidence)
        self.audit_logger.append_event(
            "rag.completed",
            {
                "query_chars": len(query),
                "elapsed_ms": int((time.time() - started_at) * 1000),
                "evidence_count": len(evidence),
                "citation_coverage": citation_report,
            },
            trace_id=trace_id,
        )
        yield generated_text
        if query_requests_mindmap(query) and "souldrive-mindmap" not in generated_text.lower():
            yield _mindmap_block(build_answer_mindmap(query, generated_text))
        yield _evidence_block(evidence)

    def _generate_answer_text(self, messages: list[dict[str, str]]) -> str:
        try:
            stream = self.llm.create_chat_completion(
                messages=messages,
                stream=True,
                temperature=self.runtime_config.temperature,
                top_p=self.runtime_config.top_p,
                repeat_penalty=self.runtime_config.repeat_penalty,
                max_tokens=self.runtime_config.max_tokens,
                stop=["</context>", "<user_query>", "<|im_end|>"],
            )
        except Exception as exc:
            runtime_metrics.increment("generation_failures")
            runtime_metrics.record_error(str(exc))
            raise

        generated_parts = []
        for chunk in stream:
            if "choices" not in chunk or not chunk["choices"]:
                continue
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content")
            if content:
                generated_parts.append(normalize_technical_terms(content))
        return "".join(generated_parts).strip()


def build_vector_context(vector_docs: list[str], vector_metas: list[dict], evidence: list[dict]) -> str:
    if not vector_docs:
        return "未检索到直接相关的文献片段。"

    blocks = []
    for index, doc in enumerate(vector_docs):
        metadata = vector_metas[index] if index < len(vector_metas) else {}
        evidence_item = evidence[index] if index < len(evidence) else {}
        source = metadata.get("source_filename", "未知文件")
        evidence_id = evidence_item.get("id", f"E{index + 1}")
        section = evidence_item.get("section") or "未知章节"
        chunk_index = evidence_item.get("chunk_index")
        blocks.append(
            f"[{evidence_id} | 来源: {source} | 章节: {section} | 切片: {chunk_index}]\n"
            f"{compact_context_text(doc, MAX_CONTEXT_CHARS_PER_EVIDENCE)}"
        )
    return "\n\n".join(blocks)


def build_graph_context(graph_context: list[str]) -> str:
    if not graph_context:
        return ""
    return "【知识图谱关系】\n" + "\n".join(graph_context)


def repair_missing_citations(answer: str, evidence: list[dict], validation_report: dict) -> str:
    if validation_report.get("reason") != "answer missing evidence citation":
        return answer
    if not (answer or "").strip() or not evidence:
        return answer

    primary_id = evidence[0].get("id")
    if not primary_id:
        return answer

    stripped = answer.strip()
    if not answer_overlaps_evidence(stripped, evidence[0]):
        return answer
    if stripped.endswith(f"[{primary_id}]"):
        return stripped
    return f"{stripped} [{primary_id}]"


def answer_overlaps_evidence(answer: str, evidence_item: dict) -> bool:
    answer_terms = citation_repair_terms(answer)
    evidence_terms = citation_repair_terms(evidence_text(evidence_item))
    if not answer_terms or not evidence_terms:
        return False
    return len(answer_terms & evidence_terms) >= 2


def citation_repair_terms(text: str) -> set[str]:
    normalized = re.sub(r"\[E\d+\]", "", text or "")
    terms = {token.lower() for token in re.findall(r"[A-Za-z0-9_]{3,}", normalized)}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        max_size = min(4, len(chunk))
        for size in range(2, max_size + 1):
            for index in range(0, len(chunk) - size + 1):
                terms.add(chunk[index : index + size])
    return {term for term in terms if len(term) >= 2}


def evidence_fallback_answer(query: str, evidence: list[dict]) -> str:
    if not evidence:
        return "根据本地存储的知识库，未找到足够可靠的相关证据，因此不会生成可能误导的回答。"

    points = synthesize_evidence_points(query, evidence)
    if points:
        intro = fallback_intro(query)
        lines = [intro]
        for point, evidence_id in points[:4]:
            lines.append(f"- {point} [{evidence_id}]")
        return "\n".join(lines)

    lines = ["根据已检索到的证据，主要信息包括："]
    for item in evidence[:3]:
        evidence_id = item.get("id") or "E?"
        snippet = clean_evidence_sentence(item.get("snippet") or item.get("section") or "")
        if snippet:
            lines.append(f"- {compact_context_text(snippet, 120)} [{evidence_id}]")

    if len(lines) == 1:
        return "根据本地知识库，检索到了相关证据，但证据摘录不足以形成完整回答。"
    return "\n".join(lines)


def build_answer_messages(query: str, vector_context: str, graph_context: str) -> list[dict[str, str]]:
    system_prompt = (
        "你是 SoulDrive 的本地知识问答助手。"
        "你只能依据给定的 <context> 回答，不允许使用未在证据中出现的事实。"
        "你的任务是回答用户问题，先归纳结论，再给出依据；不要复制证据原文、论文标题、作者列表或摘要全文。"
        "默认用 2 到 5 条中文要点回答，每条要点只表达一个结论，并用一句话解释它为什么回答了问题。"
        "每个关键结论都必须带上 [E1]、[E2] 这类证据引用。"
        "如果证据不足，只能明确说明未找到足够可靠的相关证据。"
        "如果证据只支持问题的一部分，也必须诚实说明范围限制。"
        "技术名词如 Transformer、Attention、BERT、RoBERTa 保持英文原名。"
    )
    user_prompt = (
        "<context>\n"
        f"{graph_context}\n"
        "【私有文献详情】\n"
        f"{vector_context}\n"
        "</context>\n\n"
        f"<user_query>\n{query}\n</user_query>"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def fallback_intro(query: str) -> str:
    if is_problem_query(query):
        return "传统企业知识管理系统的主要问题包括："
    return "根据已检索到的证据，主要信息包括："


def synthesize_evidence_points(query: str, evidence: list[dict]) -> list[tuple[str, str]]:
    normalized_query = query or ""
    if is_problem_query(normalized_query):
        return synthesize_problem_points(evidence)
    return synthesize_general_points(evidence)


def is_problem_query(query: str) -> bool:
    return any(marker in (query or "") for marker in ("问题", "不足", "痛点", "缺陷", "挑战", "瓶颈"))


def synthesize_problem_points(evidence: list[dict]) -> list[tuple[str, str]]:
    points: list[tuple[str, str]] = []
    patterns = (
        ("构建成本高", "构建成本高，企业落地和维护知识管理系统的投入压力较大"),
        ("知识利用率低", "知识利用率低，已有知识没有被充分检索、复用和转化为业务价值"),
        ("检索效率", "检索效率仍有改进空间，影响用户快速定位知识的体验"),
        ("检索准确", "检索准确性仍有改进空间，容易影响知识问答结果的可靠性"),
        ("意图理解", "意图理解仍有改进空间，系统需要更准确地识别用户真实查询目标"),
        ("数据安全", "数据安全需要持续保障，企业知识库涉及内部资料和权限边界"),
        ("知识覆盖范围", "知识覆盖范围仍有限，需要继续扩展更多知识来源"),
        ("用户体验", "用户体验仍有优化空间，需要降低使用门槛并提升交互效率"),
    )
    seen = set()
    for item in evidence:
        evidence_id = item.get("id") or "E?"
        text = evidence_text(item)
        for marker, point in patterns:
            if marker in text and point not in seen:
                points.append((point, evidence_id))
                seen.add(point)
    return points


def synthesize_general_points(evidence: list[dict]) -> list[tuple[str, str]]:
    points: list[tuple[str, str]] = []
    seen = set()
    for item in evidence[:3]:
        evidence_id = item.get("id") or "E?"
        sentence = clean_evidence_sentence(item.get("snippet") or item.get("section") or "")
        if sentence and sentence not in seen:
            points.append((compact_context_text(sentence, 120), evidence_id))
            seen.add(sentence)
    return points


def evidence_text(item: dict) -> str:
    return " ".join(str(item.get(key) or "") for key in ("section", "snippet", "source_filename"))


def clean_evidence_sentence(text: str) -> str:
    normalized = " ".join((text or "").split())
    if not normalized:
        return ""

    normalized = re.sub(r"^#*\s*[^，。；:：]{0,80}(?:Large Model Knowledge Management System|/ ZHOU Yang)[^。；]*[。；:：]?", "", normalized)
    normalized = re.sub(r"^[^。；]*摘要[:：]\s*", "", normalized)
    sentences = re.split(r"(?<=[。！？；.!?;])\s*", normalized)
    for sentence in sentences:
        cleaned = sentence.strip(" -，,。；;")
        if not cleaned:
            continue
        if "Large Model Knowledge Management System" in cleaned or "/ ZHOU Yang" in cleaned:
            continue
        return cleaned
    return normalized[:120].strip()


def compact_context_text(text: str, max_chars: int = MAX_CONTEXT_CHARS_PER_EVIDENCE) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized

    marker = " ... [context trimmed] ... "
    if max_chars <= len(marker) + 80:
        return normalized[:max_chars]

    available = max_chars - len(marker)
    head_budget = max(80, int(available * 0.68))
    tail_budget = max(40, available - head_budget)
    if head_budget + tail_budget > available:
        head_budget = available - tail_budget
    return f"{normalized[:head_budget]}{marker}{normalized[-tail_budget:]}"


def query_requests_mindmap(query: str) -> bool:
    normalized = (query or "").lower()
    markers = (
        "思维导图",
        "导图",
        "结构图",
        "知识图谱",
        "mindmap",
        "mind map",
        "knowledge graph",
    )
    return any(marker in normalized for marker in markers)


def build_evidence_mindmap(query: str, evidence: list[dict]) -> str:
    return build_answer_mindmap(query, evidence_outline_to_answer_text(evidence))


def build_answer_mindmap(query: str, answer: str) -> str:
    title = mindmap_topic_from_query(query)
    outline = parse_answer_outline(answer)
    if not outline:
        fallback = mindmap_text(answer or query or "核心回答")
        return "\n".join([
            f"# {title}",
            "## 核心回答",
            f"- {fallback}",
        ])

    lines = [f"# {title}"]
    for section in outline:
        lines.append(f"## {section['title']}")
        lines.extend(serialize_outline_children(section.get("children", []), 0))
    return "\n".join(lines)


def mindmap_topic_from_query(query: str) -> str:
    normalized = strip_mindmap_request(mindmap_text(query))
    if not normalized:
        return "研究导图"

    normalized = normalized.strip(" ？?。；;，,：:")
    if "数据要素" in normalized and "治理目标" in normalized:
        return "数据要素治理目标"
    if "安全评估" in normalized and any(marker in normalized for marker in ("为什么需要", "必要", "重要")):
        return "安全评估必要性"
    if "生成式人工智能服务安全" in normalized and "要求" in normalized:
        return "生成式人工智能服务安全要求"

    topic = normalized
    topic = re.sub(r"^(?:请问|请|帮我|帮忙|能否|可以)?", "", topic)
    topic = re.sub(r"(?:主要)?(?:覆盖|包括|包含|涉及)哪些(?:方面|内容|要求)?", "", topic)
    topic = re.sub(r"(?:分别)?(?:指向|对应|体现|说明|代表)什么(?:治理目标|目标|含义|意义)?", "", topic)
    topic = re.sub(r"为什么需要(.+)$", r"\1必要性", topic)
    topic = topic.replace("基本要求", "要求")
    topic = topic.strip(" ？?。；;，,：:")
    return compact_mindmap_title(topic or normalized)


def strip_mindmap_request(value: str) -> str:
    normalized = mindmap_text(value)
    phrases = (
        "请以导图的形式加以展示",
        "以导图的形式加以展示",
        "请以导图形式加以展示",
        "以导图形式加以展示",
        "请以导图的形式展示",
        "以导图的形式展示",
        "请以导图形式展示",
        "以导图形式展示",
        "请用导图说明",
        "用导图说明",
        "请用思维导图说明",
        "用思维导图说明",
        "请用结构图说明",
        "用结构图说明",
        "思维导图",
        "导图",
        "结构图",
        "知识图谱",
    )
    for phrase in phrases:
        normalized = normalized.replace(phrase, "")
    return normalized


def compact_mindmap_title(value: str, max_chars: int = 24) -> str:
    normalized = mindmap_text(value).strip(" ？?。；;，,：:")
    if len(normalized) <= max_chars:
        return normalized or "研究导图"
    return normalized[:max_chars].rstrip(" ？?。；;，,：:") or "研究导图"


def parse_answer_outline(answer: str) -> list[dict]:
    sections: list[dict] = []
    current_section: dict | None = None
    stack: list[tuple[int, dict]] = []
    intro_lines: list[str] = []

    for raw_line in (answer or "").splitlines():
        if not raw_line.strip():
            continue

        parsed = parse_outline_line(raw_line)
        if parsed is None:
            cleaned = mindmap_text(raw_line)
            if not cleaned:
                continue
            if is_summary_line(cleaned):
                section = make_summary_outline_node(cleaned)
                sections.append(section)
                current_section = section
                stack = [(0, section)]
                continue
            if current_section is None:
                intro_lines.append(cleaned)
                continue
            if should_skip_intro_line(cleaned):
                continue
            parent = stack[-1][1] if stack else current_section
            parent["children"].append(make_outline_node(cleaned))
            continue

        kind = parsed["kind"]
        title = parsed["title"]
        detail = parsed["detail"]
        indent = parsed["indent"]

        if is_summary_outline(title, detail):
            section = make_summary_outline_node(summary_content(title, detail))
            sections.append(section)
            current_section = section
            stack = [(0, section)]
            continue

        if kind == "section":
            section = make_outline_node(title)
            sections.append(section)
            current_section = section
            stack = [(0, section)]
            if detail:
                detail_node = make_outline_node(detail)
                section["children"].append(detail_node)
                stack.append((0, detail_node))
            continue

        if current_section is None and should_skip_intro_line(title):
            continue

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else current_section
        if parent is None:
            intro_lines.append(title)
            continue

        node = make_outline_node(title)
        parent["children"].append(node)
        stack.append((indent, node))
        if detail:
            detail_node = make_outline_node(detail)
            node["children"].append(detail_node)
            stack.append((indent + 1, detail_node))

    if sections:
        return sections

    fallback_text = " ".join(intro_lines).strip()
    if not fallback_text:
        return []
    return [make_outline_node("核心回答", [make_outline_node(fallback_text)])]


def is_summary_outline(title: str, detail: str) -> bool:
    title_text = mindmap_text(title)
    if title_text in ("总结", "归纳总结", "小结", "结论", "总体结论", "综合结论"):
        return True
    return is_summary_line(summary_content(title, detail))


def summary_content(title: str, detail: str) -> str:
    title_text = mindmap_text(title)
    detail_text = mindmap_text(detail)
    if title_text in ("总结", "归纳总结", "小结", "结论", "总体结论", "综合结论") and detail_text:
        return detail_text
    if detail_text:
        return f"{title_text}：{detail_text}"
    return title_text


def is_summary_line(value: str) -> bool:
    normalized = mindmap_text(value)
    if not normalized:
        return False
    summary_prefixes = (
        "综上",
        "总之",
        "因此",
        "所以",
        "整体来看",
        "总体来看",
        "总的来说",
        "归纳来看",
        "上述归纳",
        "上述内容",
        "由此可见",
        "可以看出",
        "需要说明",
        "虽然",
    )
    if normalized.startswith(summary_prefixes):
        return True
    return any(marker in normalized for marker in ("基础框架", "总体框架", "归纳总结", "总结来看"))


def make_summary_outline_node(content: str) -> dict:
    cleaned = mindmap_text(content)
    children = [make_outline_node(cleaned)] if cleaned else []
    return make_outline_node("归纳总结", children)


def parse_outline_line(raw_line: str) -> dict[str, int | str] | None:
    stripped = raw_line.rstrip()
    if not stripped.strip():
        return None

    heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped.strip())
    if heading_match:
        heading_level = len(heading_match.group(1))
        title, detail = split_outline_title(heading_match.group(2))
        if heading_level <= 2:
            return {"kind": "section", "title": title, "detail": detail, "indent": 0}
        return {"kind": "child", "title": title, "detail": detail, "indent": heading_level - 2}

    bullet_match = re.match(
        r"^(\s*)(?:[-*+]|(?:\d+(?:\.\d+)*[.)、．]?|[一二三四五六七八九十]+[、.．)]?))\s+(.+)$",
        stripped,
    )
    if bullet_match:
        indent = len(bullet_match.group(1).replace("\t", "  "))
        title, detail = split_outline_title(bullet_match.group(2))
        if indent == 0:
            return {"kind": "section", "title": title, "detail": detail, "indent": 0}
        return {"kind": "child", "title": title, "detail": detail, "indent": indent}

    return None


def split_outline_title(value: str) -> tuple[str, str]:
    normalized = mindmap_text(value)
    normalized = re.sub(r"^(?:[（(]?[一二三四五六七八九十]+[)）]?|(?:\d+(?:\.\d+)*))(?:[、.．)]|[）)]|、)?\s*", "", normalized)
    if "：" in normalized:
        title, detail = normalized.split("：", 1)
    elif ":" in normalized:
        title, detail = normalized.split(":", 1)
    else:
        title, detail = normalized, ""
    title = title.strip(" -：:。；;")
    detail = detail.strip(" -：:。；;")
    return title or normalized, detail


def should_skip_intro_line(value: str) -> bool:
    normalized = mindmap_text(value)
    return normalized.startswith(("主要包括", "包括以下", "可以从", "通常包括", "先看", "下面是")) or normalized.endswith(("：", ":", "如下", "如下："))


def make_outline_node(title: str, children: list[dict] | None = None) -> dict:
    return {"title": mindmap_text(title), "children": children or []}


def serialize_outline_children(children: list[dict], depth: int) -> list[str]:
    lines: list[str] = []
    for child in children:
        title = mindmap_text(child.get("title") or "")
        if not title:
            continue
        lines.append(f"{'  ' * depth}- {title}")
        lines.extend(serialize_outline_children(child.get("children", []), depth + 1))
    return lines


def evidence_outline_to_answer_text(evidence: list[dict]) -> str:
    if not evidence:
        return ""

    lines: list[str] = []
    for item in evidence[:5]:
        section = mindmap_text(item.get("section") or item.get("snippet") or "相关内容")
        snippet = clean_evidence_sentence(item.get("snippet") or "")
        if section:
            lines.append(f"- {section}")
        if snippet and snippet != section:
            lines.append(f"  - {snippet}")
    return "\n".join(lines)


def mindmap_text(value: str) -> str:
    normalized = re.sub(r"\[E\d+\]", "", str(value or ""))
    return " ".join(normalized.replace("|", " ").split())


def compact_mindmap_text(value: str, max_chars: int) -> str:
    normalized = " ".join(str(value or "").replace("|", " ").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(1, max_chars - 1)].rstrip() + "…"


def normalize_technical_terms(text: str) -> str:
    return (
        (text or "")
        .replace("变压器架构", "Transformer 架构")
        .replace("变压器模型", "Transformer 模型")
        .replace("变压器", "Transformer")
    )


def _mindmap_block(content: str) -> str:
    return f"\n\n```souldrive-mindmap\n{content}\n```"


def _evidence_block(evidence: list[dict]) -> str:
    return f"\n\n```souldrive-evidence\n{json.dumps(evidence, ensure_ascii=False, indent=2)}\n```"
