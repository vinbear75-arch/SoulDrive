import re


TERM_ALIASES = (
    ("异常检测", "anomaly detection"),
    ("工业控制系统", "industrial control systems ICS"),
    ("时序", "time series"),
    ("时间序列", "time series"),
    ("生成对抗网络", "GAN generative adversarial network"),
    ("知识管理", "knowledge management"),
    ("知识库", "knowledge base"),
    ("检索增强", "RAG retrieval augmented generation"),
    ("分类分级", "data classification grading"),
    ("个人信息", "personal information"),
    ("生成式人工智能服务安全基本要求", "生成式AI AIGC 人工智能安全 标准 合规 风险治理"),
    ("生成式人工智能", "生成式AI AIGC generative AI"),
    ("人工智能安全标准体系", "AI safety standard 人工智能安全 标准体系"),
    ("数据分类分级规则", "数据安全技术 数据分类分级规则 data classification data grading 敏感个人信息"),
    ("数据分类分级", "数据安全技术 数据分类分级规则 data classification data grading"),
    ("敏感个人信息", "personal information privacy data protection"),
    ("MaxKB", "基于大模型和RAG的知识库问答系统 knowledge base QA"),
    ("LazyLLM", "企业级RAG方案 私有化部署 权限安全 access control"),
    ("私有化部署", "本地化部署 私有部署 private deployment on-premise"),
    ("权限安全", "访问控制 access control permission security"),
    ("GraphRAG", "graph retrieval augmented generation graph rag local search global search"),
    ("城市+AI", "城市AI应用场景 industry scenario 城市治理 交通治理"),
    ("城市AI", "城市+AI 应用场景 industry scenario"),
    ("综合交通运输大模型", "综合交通运输 大模型 智能体 agent transportation"),
    ("智能体创新应用", "智能体 agent innovation application"),
    ("政府数智化转型", "政府数字化转型 政务数据治理 government digital transformation"),
    ("大模型治理蓝皮报告", "大模型治理 AI governance 风险治理 蓝皮报告"),
    ("数据要素市场", "数据价值化 数据要素 data market"),
    ("RAGBench", "RAG benchmark retrieval augmented generation evaluation"),
    ("CRUD-RAG", "Chinese RAG benchmark retrieval augmented generation"),
    ("DomainRAG", "domain specific RAG benchmark"),
    ("RankRAG", "ranking rerank retrieval augmented generation"),
    ("Self-RAG", "self reflection retrieve generate critique"),
    ("HyDE", "hypothetical document embeddings zero-shot dense retrieval"),
)


def expand_query_variants(query: str, limit: int = 5) -> list[str]:
    base = (query or "").strip()
    if not base:
        return []

    variants = [base]
    lowered = base.lower()

    comparison_variants = split_comparison_query(base)
    for comparison_query in comparison_variants:
        variants.append(comparison_query)
        alias_query = build_alias_query(comparison_query)
        if alias_query:
            variants.append(alias_query)

    if any(token in base for token in ("怎么", "如何")):
        variants.append(base.replace("怎么", "实现方式").replace("如何", "实现方式"))

    if "隐私" in base:
        variants.append(base.replace("隐私", "数据安全与隐私保护"))
    elif "安全" in base:
        variants.append(base.replace("安全", "风险控制与安全机制"))

    if "是什么" in base:
        variants.append(base.replace("是什么", "定义与核心机制"))

    if "graphrag" in lowered and "local search" in lowered:
        variants.append(base.replace("怎么工作", "核心机制").replace("如何工作", "核心机制"))

    alias_query = build_alias_query(base)
    if alias_query:
        variants.append(alias_query)

    deduped = []
    seen = set()
    for item in variants:
        normalized = " ".join(item.split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= limit:
            break
    return deduped


def split_comparison_query(query: str) -> list[str]:
    base = (query or "").strip()
    if not base or not query_requires_source_diversity(base):
        return []

    title_hints = [
        title.strip()
        for title in re.findall(r"《([^》]{2,120})》", base)
        if title.strip()
    ]
    if len(title_hints) >= 2:
        return [f"{title} 核心证据" for title in title_hints[:3]]

    match = re.search(
        r"(?:对比|比较)\s*"
        r"(?P<left>[^，,。；;：:？?]{2,80}?)"
        r"(?:与|和|及)\s*"
        r"(?P<right>[^，,。；;：:？?]{2,80}?)"
        r"(?:两类资料|两份资料|两类文档|两个方案|二者|在|：|:)",
        base,
        flags=re.IGNORECASE,
    )
    if not match:
        return []

    return [
        f"{_clean_comparison_subject(match.group('left'))} 核心证据",
        f"{_clean_comparison_subject(match.group('right'))} 核心证据",
    ]


def query_requires_source_diversity(query: str) -> bool:
    normalized = (query or "").lower()
    if any(
        marker in normalized
        for marker in (
            "对比",
            "比较",
            "异同",
            "差异",
            "两类资料",
            "两份资料",
            "多份文档",
            "compare",
            "versus",
            " vs ",
        )
    ):
        return True

    if "分别" not in normalized and "各自" not in normalized:
        return False
    title_count = len(re.findall(r"《[^》]{2,120}》", normalized))
    return title_count >= 2 or any(
        marker in normalized
        for marker in (
            "两个方案",
            "两种方案",
            "两类文档",
            "两份文档",
            "两项政策",
            "二者",
            "双方",
        )
    )


def _clean_comparison_subject(value: str) -> str:
    cleaned = re.sub(
        r"^(?:在不(?:直接)?(?:透露|给出)文档名的情况下[，,\s]*|请\s*)",
        "",
        (value or "").strip(),
    )
    return cleaned.strip(" ，,。；;：:")


def build_alias_query(query: str) -> str | None:
    aliases = []
    lowered_query = query.lower()
    for term, alias in TERM_ALIASES:
        lowered_term = term.lower()
        if term in query or lowered_term in lowered_query:
            if alias in aliases:
                continue
            aliases.append(alias)
    if not aliases:
        return None
    return " ".join([query, *aliases])
