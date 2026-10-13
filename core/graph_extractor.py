import os
import json
import inspect
import re
from core.graph_db import LocalGraphDB
from core.logging_config import get_logger
from core.model_runtime import llama_runtime_config, load_llama_model, resolve_chat_model_path
from core.observability import runtime_metrics
from core.security_context import get_workspace_keys
from core.secure_graph_store import SecureGraphStore
from core.workspace import SoulDriveWorkspace

logger = get_logger(__name__)


GRAPH_EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "type", "description"],
                "additionalProperties": False,
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string"},
                },
                "required": ["source", "target", "relation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities", "relationships"],
    "additionalProperties": False,
}


class GraphExtractor:
    def __init__(
        self,
        max_chunks_per_document: int = 8,
        max_chars_per_chunk: int = 1800,
        graph_db_path: str | None = None,
        workspace_path: str | None = None,
    ):
        self.max_chunks_per_document = max_chunks_per_document
        self.max_chars_per_chunk = max_chars_per_chunk
        logger.info("[Graph Extractor] 正在点火大模型图谱抽取引擎...")
        self.runtime_config = llama_runtime_config(workspace_path)
        chat_provider = str(os.environ.get("SOULDRIVE_CHAT_PROVIDER") or "").strip().lower()
        if chat_provider == "ollama":
            from core.rag_engine import _OllamaChatModel

            model_name = str(os.environ.get("SOULDRIVE_OLLAMA_MODEL") or "qwen3.5:9b").strip()
            base_url = str(os.environ.get("SOULDRIVE_OLLAMA_URL") or "http://127.0.0.1:11434").strip()
            logger.info("[Graph Extractor] 使用本地 Ollama 模型: %s", model_name)
            self.llm = _OllamaChatModel(model_name, base_url)
            runtime_metrics.record_model_load(0)
            self.db = self._build_graph_store(graph_db_path, workspace_path)
            return

        model_path = resolve_chat_model_path(workspace_path, self.runtime_config)

        if not os.path.exists(model_path):
            runtime_metrics.increment("model_load_failures")
            raise FileNotFoundError(f"[致命错误] 找不到模型文件: {model_path}")

        try:
            load_result = load_llama_model(
                model_path=model_path,
                config=self.runtime_config,
                n_ctx=self.runtime_config.graph_n_ctx,
            )
            self.llm = load_result.model
            self.runtime_config = load_result.effective_config
            runtime_metrics.record_model_load(load_result.load_ms)
        except Exception as exc:
            runtime_metrics.increment("model_load_failures")
            runtime_metrics.record_error(str(exc))
            raise
        self.db = self._build_graph_store(graph_db_path, workspace_path)

    def _build_graph_store(self, graph_db_path: str | None, workspace_path: str | None):
        if workspace_path:
            keys = get_workspace_keys(workspace_path)
            if keys is not None:
                workspace = SoulDriveWorkspace(workspace_path)
                return SecureGraphStore(workspace.secure_graph_store_path, keys)

        if not graph_db_path:
            raise ValueError("graph_db_path is required for GraphExtractor")
        return LocalGraphDB(db_path=graph_db_path)

    def extract_chunks(self, chunks: list, source_filename: str = ""):
        """
        从文档切片中批量抽取实体关系，并写入本地图谱库。

        默认限制单文档抽取的切片数量和单片字符数，避免 CPU 端侧模型在入库阶段
        长时间阻塞。后续如需要高召回，可以调大构造参数。
        """
        selected_chunks = self._select_chunks(chunks)
        summary = {
            "attempted_chunks": 0,
            "chunks": 0,
            "failed_chunks": 0,
            "entities": 0,
            "relationships": 0,
            "documents": 0,
        }

        document_chunk_ids = [
            chunk_id
            for chunk in selected_chunks
            for chunk_id in (getattr(chunk, "metadata", {}) or {}).get("child_ids") or []
        ]
        project_ids = {
            str((getattr(chunk, "metadata", {}) or {}).get("project_id") or "").strip()
            for chunk in selected_chunks
            if str((getattr(chunk, "metadata", {}) or {}).get("project_id") or "").strip()
        }
        project_id = next(iter(project_ids)) if len(project_ids) == 1 else ""
        document_keywords = []
        document_texts = []

        for index, chunk in enumerate(selected_chunks, start=1):
            text = getattr(chunk, "page_content", str(chunk)).strip()
            if not text:
                continue

            if self.max_chars_per_chunk and len(text) > self.max_chars_per_chunk:
                text = text[:self.max_chars_per_chunk]

            if source_filename:
                text = f"文档来源：{source_filename}\n片段序号：{index}/{len(selected_chunks)}\n\n{text}"
            document_texts.append(text)

            summary["attempted_chunks"] += 1
            chunk_ids = list(getattr(chunk, "metadata", {}).get("child_ids") or [])
            data = self.extract_and_store(text, chunk_ids=chunk_ids)
            if not data:
                summary["failed_chunks"] += 1
                continue

            summary["chunks"] += 1
            summary["entities"] += len(data.get("entities", []))
            summary["relationships"] += len(data.get("relationships", []))
            document_keywords.extend(item.get("name") for item in data.get("entities", []))

        if source_filename and hasattr(self.db, "add_document"):
            digest = _document_digest(document_texts, source_filename)
            generated_summary = self.generate_document_summary(
                source_filename,
                document_texts,
                document_keywords,
            )
            document_kwargs = {
                "source_filename": source_filename,
                "title": source_filename,
                "summary": generated_summary or digest,
                "keywords": document_keywords[:40],
                "chunk_ids": document_chunk_ids,
            }
            if project_id:
                document_kwargs["project_id"] = project_id
            self.db.add_document(
                **document_kwargs,
            )
            summary["documents"] = 1

        logger.info(
            "[Graph Extractor] 文档图谱抽取完成: 成功 %s/%s 个片段, 失败 %s 个片段, %s 个实体, %s 条关系。",
            summary["chunks"],
            summary["attempted_chunks"],
            summary["failed_chunks"],
            summary["entities"],
            summary["relationships"],
        )
        return summary

    def generate_document_summary(
        self,
        source_filename: str,
        texts: list[str],
        keywords: list[str],
    ) -> str:
        """在索引期生成文档级压缩摘要；失败时由调用方保留结构化摘录兜底。"""
        excerpts = [str(item).strip()[:1200] for item in texts if str(item).strip()]
        if not excerpts:
            return ""
        prompt = (
            "你是中文论文知识索引器。请根据给定文档片段生成 120-360 字的文档级摘要，"
            "概括研究问题、方法、关键结论和适用边界。只能使用输入信息，不要补充外部事实，"
            "不要输出 Markdown、JSON、标题或提示词。该摘要仅用于 GraphRAG 导航，具体事实必须回到原文。\n\n"
            f"文档：{source_filename}\n"
            f"已提取关键词：{'、'.join(str(item) for item in keywords[:20])}\n"
            "文档片段：\n"
            + "\n\n".join(excerpts[:8])
        )
        try:
            response = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": "你只输出严谨、简洁的中文文档摘要。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=min(0.2, self.runtime_config.graph_temperature),
                top_p=self.runtime_config.top_p,
                max_tokens=min(700, self.runtime_config.graph_max_tokens),
                repeat_penalty=self.runtime_config.graph_repeat_penalty,
            )
            content = str(response.get("choices", [{}])[0].get("message", {}).get("content") or "")
            content = re.sub(r"```(?:text|markdown)?|```", "", content, flags=re.IGNORECASE)
            return " ".join(content.split()).strip()[:2200]
        except Exception as exc:
            logger.warning("[Graph Extractor] document summary degraded: %s", exc)
            return ""

    def generate_community_report(self, community: dict) -> str:
        """用索引阶段已加载的模型生成社区摘要；失败时由构建器使用模板兜底。"""
        system_prompt = (
            "你是本地文档索引阶段的知识抽象器。"
            "请根据给定的实体、关系和来源文件，生成一段 120-400 字的中文社区报告。"
            "只描述输入中能支持的主题、关系和来源，不补充外部事实；不要输出 Markdown、JSON、"
            "提示词或行动指令。社区报告是检索摘要，回答时必须回到原始证据核验。"
        )
        output_tokens = min(800, self.runtime_config.graph_max_tokens)
        prompt_budget = _graph_prompt_char_budget(
            self.runtime_config.graph_n_ctx,
            output_tokens,
        )
        user_prompt = _compact_community_payload(community, max_chars=prompt_budget)
        try:
            response = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=min(0.2, self.runtime_config.graph_temperature),
                max_tokens=output_tokens,
                repeat_penalty=self.runtime_config.graph_repeat_penalty,
            )
            content = str(response["choices"][0]["message"].get("content") or "").strip()
            content = re.sub(r"```(?:text|markdown)?|```", "", content, flags=re.IGNORECASE).strip()
            return content[:2200]
        except Exception as exc:
            logger.warning("[Graph Extractor] community report degraded: %s", exc)
            return ""

    def close(self):
        """主动释放图谱抽取器资源，避免进程退出阶段触发析构异常。"""
        if hasattr(self, "db") and self.db is not None:
            self.db.close()
            self.db = None

        if hasattr(self, "llm") and self.llm is not None:
            close_fn = getattr(self.llm, "close", None)
            if callable(close_fn):
                close_fn()
            self.llm = None

    def _select_chunks(self, chunks: list):
        if not chunks or self.max_chunks_per_document is None:
            return chunks

        if len(chunks) <= self.max_chunks_per_document:
            return chunks

        # 端侧抽取先覆盖文档开头、结尾与中段代表片段，保证速度和覆盖面平衡。
        first_count = max(1, self.max_chunks_per_document // 2)
        last_count = max(1, self.max_chunks_per_document - first_count - 1)
        middle_index = len(chunks) // 2

        selected = chunks[:first_count] + [chunks[middle_index]] + chunks[-last_count:]
        deduped = []
        seen_ids = set()
        for chunk in selected:
            marker = id(chunk)
            if marker not in seen_ids:
                seen_ids.add(marker)
                deduped.append(chunk)
        return deduped[:self.max_chunks_per_document]

    def extract_and_store(self, text: str, chunk_ids: list[str] | None = None):
        logger.info("[处理中] 正在让大模型咀嚼文本，提炼三元组 (实体与关系)...")

        # 架构师级 Prompt：强制进行 JSON 结构化输出
        system_prompt = (
            "你是一个运行在顶级边缘计算节点上的数据挖掘专家。\n"
            "你的任务是：阅读用户提供的文本，提取出核心的【知识实体(Entity)】以及它们之间的【关系(Relationship)】。\n"
            "【严格规则】：\n"
            "1. 实体类型(type)可以是：技术、概念、机构、人物、产品等。\n"
            "2. 你必须且只能返回合法的 JSON 格式字符串，绝对不要输出任何其他的解释性文字、开场白或 Markdown 标记符！\n"
            "【JSON 输出模板】：\n"
            "{\n"
            "  \"entities\": [{\"name\": \"实体名\", \"type\": \"类型\", \"description\": \"描述\"}],\n"
            "  \"relationships\": [{\"source\": \"源实体名\", \"target\": \"目标实体名\", \"relation\": \"关系动作\"}]\n"
            "}"
        )

        user_prompt = f"请提取以下文献片段中的图谱信息：\n\n{text}"

        try:
            # 触发极低温度的确定性推理
            completion_kwargs = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": self.runtime_config.graph_temperature,
                "max_tokens": self.runtime_config.graph_max_tokens,
                "repeat_penalty": self.runtime_config.graph_repeat_penalty,
            }
            if _supports_response_format(self.llm):
                completion_kwargs["response_format"] = GRAPH_EXTRACTION_RESPONSE_SCHEMA
            response = self.llm.create_chat_completion(**completion_kwargs)

            raw_output = response['choices'][0]['message']['content']
            data = self._parse_llm_json(raw_output)
            if not data:
                logger.error("[解析灾难] 大模型输出无法修复为 JSON。原始输出为：\n%s", raw_output)
                return None

            entities = data.get("entities", [])
            relations = data.get("relationships", [])

            # 开启数据库写入
            for ent in entities:
                self.db.add_entity(ent["name"], ent["type"], ent["description"], chunk_ids=chunk_ids)

            for rel in relations:
                self.db.add_relationship(rel["source"], rel["target"], rel["relation"], evidence_chunk_ids=chunk_ids)

            logger.info("[成功] 知识图谱落盘完成！入库 %s 个实体，%s 条关联边。", len(entities), len(relations))
            return data

        except Exception as e:
            logger.exception("[系统异常] 抽取中断: %s", e)
            return None

    def _parse_llm_json(self, raw_output: str):
        cleaned_output = self._extract_json_text(raw_output)
        normalized_output = self._normalize_json_text(cleaned_output)

        try:
            payload = json.loads(normalized_output)
        except Exception:
            return None

        return self._normalize_payload(payload)

    def _extract_json_text(self, raw_output: str):
        text = re.sub(r"```(?:json)?\s*|\s*```", "", (raw_output or "").strip(), flags=re.IGNORECASE)
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return text

    def _normalize_json_text(self, text: str):
        # 修复常见裸键名：description: -> "description":
        text = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', text)
        # 修复 "type=": 这类键名污染
        text = re.sub(r'"type="\s*:', '"type":', text, flags=re.IGNORECASE)
        # 兼容中文字段名
        text = text.replace('"类型"', '"type"').replace('"描述"', '"description"')
        return text

    def _normalize_payload(self, payload: dict):
        if not isinstance(payload, dict):
            return {"entities": [], "relationships": []}

        entities = []
        seen_entities = set()
        for item in payload.get("entities", []):
            if not isinstance(item, dict):
                continue

            name = self._safe_text(self._pick_field(item, ("name", "名称")), max_len=100)
            if not name:
                continue

            entity_type = self._safe_text(self._pick_field(item, ("type", "类型", "类别")), max_len=40) or "概念"
            description = self._safe_text(self._pick_field(item, ("description", "描述", "desc")), max_len=200) or name
            entity_key = (name, entity_type)
            if entity_key in seen_entities:
                continue
            seen_entities.add(entity_key)
            entities.append({
                "name": name,
                "type": entity_type,
                "description": description,
            })

        relationships = []
        seen_relations = set()
        for item in payload.get("relationships", []):
            if not isinstance(item, dict):
                continue

            source = self._safe_text(self._pick_field(item, ("source", "源", "起点")), max_len=100)
            target = self._safe_text(self._pick_field(item, ("target", "目标", "终点")), max_len=100)
            relation = self._safe_text(self._pick_field(item, ("relation", "关系")), max_len=40) or "相关"
            if not source or not target:
                continue

            relation_key = (source, target, relation)
            if relation_key in seen_relations:
                continue
            seen_relations.add(relation_key)
            relationships.append({
                "source": source,
                "target": target,
                "relation": relation,
            })

        return {"entities": entities, "relationships": relationships}

    def _pick_field(self, item: dict, aliases):
        for alias in aliases:
            if alias in item:
                return item[alias]

        normalized_aliases = {self._normalize_key(alias) for alias in aliases}
        for key, value in item.items():
            normalized_key = self._normalize_key(key)
            if normalized_key in normalized_aliases:
                return value

        return None

    def _normalize_key(self, key):
        text = str(key).lower()
        text = text.replace("：", ":").replace("=", "")
        text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
        return text

    def _safe_text(self, value, max_len: int):
        if value is None:
            return ""
        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        text = text.strip("[](){}\"'`")
        if not text:
            return ""
        return text[:max_len]


def _document_digest(texts: list[str], source_filename: str) -> str:
    """保留原文前提下的轻量文档级知识单元，供社区报告回溯。"""
    normalized = [re.sub(r"\s+", " ", str(text or "")).strip() for text in texts]
    normalized = [text for text in normalized if text]
    if not normalized:
        return f"文档：{source_filename}；索引阶段未提取到可用正文。"
    excerpts = normalized[:2]
    if len(normalized) > 2:
        excerpts.append(normalized[-1])
    return f"文档：{source_filename}\n索引阶段保留的结构化摘要片段：" + "\n".join(excerpts)[:3800]


def _supports_response_format(llm) -> bool:
    """仅向明确声明该参数的模型传递结构化输出约束。"""
    try:
        parameters = inspect.signature(llm.create_chat_completion).parameters.values()
    except (TypeError, ValueError, AttributeError):
        return False
    return "response_format" in {parameter.name for parameter in parameters}


def _graph_prompt_char_budget(graph_n_ctx: int, output_tokens: int) -> int:
    """为本地模型的社区摘要预留系统提示词和生成空间。"""
    context_limit = max(1024, int(graph_n_ctx or 8192))
    generation_budget = max(128, int(output_tokens or 800))
    return max(2200, min(6000, context_limit - generation_budget - 512))


def _compact_community_payload(community: dict, *, max_chars: int) -> str:
    """构造有界且仍为合法 JSON 的社区摘要输入。"""
    def candidate(entity_limit, source_limit, relation_limit, document_limit, summary_limit, keyword_limit):
        payload = {
            "community_id": str(community.get("community_id") or ""),
            "title": str(community.get("title") or "")[:180],
            "entity_names": [str(item) for item in (community.get("entity_names") or [])[:entity_limit]],
            "source_filenames": [
                str(item) for item in (community.get("source_filenames") or [])[:source_limit]
            ],
            "relationships": [
                str(item) for item in (community.get("relationships") or [])[:relation_limit]
            ],
            "document_summaries": [
                {
                    "source_filename": str(item.get("source_filename") or ""),
                    "summary": str(item.get("summary") or "")[:summary_limit],
                    "keywords": [
                        str(keyword) for keyword in (item.get("keywords") or [])[:keyword_limit]
                    ],
                }
                for item in (community.get("document_summaries") or [])[:document_limit]
                if isinstance(item, dict)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    candidates = (
        (24, 8, 16, 4, 500, 8),
        (16, 6, 10, 2, 300, 6),
        (10, 4, 6, 1, 180, 4),
        (6, 3, 3, 0, 0, 0),
        (3, 2, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
    )
    for limits in candidates:
        encoded = candidate(*limits)
        if len(encoded) <= max_chars:
            return encoded
    minimal = {
        "community_id": str(community.get("community_id") or ""),
        "title": str(community.get("title") or "")[: max(80, max_chars // 2)],
    }
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))[:max_chars]

# ==================== 自动化点火测试 ====================
if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m core.graph_extractor <SoulDrive-workspace-path>")

    workspace = SoulDriveWorkspace(sys.argv[1]).ensure()
    extractor = GraphExtractor(graph_db_path=workspace.graph_db_path, workspace_path=workspace.root_path)

    # 模拟一段极其经典的 RAG 喂入文本
    sample_text = (
        "Transformer 是由 Google 团队在 2017 年提出的一种深度学习架构。"
        "它彻底抛弃了传统的循环神经网络（RNN）和卷积神经网络（CNN），"
        "完全依赖于自注意力机制（Self-Attention）来处理序列数据。"
        "Transformer 的核心组件包括编码器（Encoder）和解码器（Decoder）。"
    )

    logger.info("[测试] 喂入生肉文本：")
    logger.info(sample_text)

    # 启动榨汁机！
    extractor.extract_and_store(sample_text)
    extractor.close()
