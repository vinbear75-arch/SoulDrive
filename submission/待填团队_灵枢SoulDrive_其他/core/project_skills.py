import hashlib
import json
import re
from pathlib import Path

from core.skill_runtime import normalize_skill_payload


MAX_RESOURCE_FILES = 48
MAX_RESOURCE_BYTES = 512 * 1024
SCRIPT_SUFFIXES = {".bat", ".cmd", ".js", ".mjs", ".ps1", ".py", ".sh", ".ts"}
ASSET_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
ADAPTER_NAME = "souldrive_skill.json"
ADAPTER_SCHEMA = "souldrive_skill_adapter_v1"
SKILL_TARGETS = {"conversation", "presentation"}


def discover_project_skills(skills_path: str) -> list[dict]:
    root = Path(skills_path).resolve()
    if not root.is_dir():
        return []
    files = sorted({*root.glob("*.md"), *root.glob("**/SKILL.md")})[:100]
    records = []
    for path in files:
        resolved = path.resolve()
        relative_parts = resolved.relative_to(root).parts if root in resolved.parents else ()
        if (
            root not in resolved.parents
            or any(part.startswith(".") for part in relative_parts)
            or resolved.stat().st_size > 200_000
        ):
            continue
        raw_content = resolved.read_text(encoding="utf-8", errors="replace").strip()
        if not raw_content:
            continue
        title, description = _front_matter(raw_content)
        adapter = compile_local_skill_adapter(resolved, title=title, description=description)
        resource_manifest = _resource_manifest(resolved, root)
        presentation_style = _presentation_style(resolved.parent) if adapter["target"] == "presentation" else {}
        digest_payload = {
            "adapter": adapter,
            "resources": [item for item in resource_manifest if item["kind"] != "script"],
            "presentation_style": presentation_style,
        }
        records.append({
            "title": adapter["title"],
            "content": adapter["content"],
            "intent": adapter["intent"],
            "applicability": adapter["applicability"],
            "steps": adapter["steps"],
            "output_contract": adapter["output_contract"],
            "success_criteria": adapter["success_criteria"],
            "presentation_contract": adapter.get("presentation_contract", {}),
            "triggers": adapter["triggers"],
            "tags": ["project", "local-file", adapter["target"]],
            "target": adapter["target"],
            "compatibility_status": adapter["compatibility_status"],
            "adapter_path": ADAPTER_NAME,
            "adapter_schema": ADAPTER_SCHEMA,
            "raw_source_digest": adapter["source_sha256"],
            "source_path": str(resolved.relative_to(root)).replace("\\", "/"),
            "source_digest": hashlib.sha256(
                json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "resource_manifest": [item for item in resource_manifest if item["kind"] != "script"],
            "presentation_style": presentation_style,
            **(
                {"presentation_style_source": "presentation_style.json"}
                if presentation_style
                else {}
            ),
        })
    return _deduplicate_skills(records)


def compile_local_skill_adapter(
    skill_file: Path,
    *,
    target: str | None = None,
    title: str = "",
    description: str = "",
) -> dict:
    """Compile and cache the short contract consumed by the local model."""
    raw_content = skill_file.read_text(encoding="utf-8", errors="replace").strip()
    raw_title, raw_description = _front_matter(raw_content)
    title = title or raw_title or skill_file.parent.name
    description = description or raw_description or title
    source_sha256 = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    adapter_path = skill_file.parent / ADAPTER_NAME
    manifest_target = _manifest_target(skill_file.parent)
    selected_target = str(target or manifest_target or _infer_target(title, description)).strip().casefold()
    if selected_target not in SKILL_TARGETS:
        selected_target = "conversation"

    try:
        cached = json.loads(adapter_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        cached = {}
    if (
        isinstance(cached, dict)
        and cached.get("schema_version") == ADAPTER_SCHEMA
        and str(cached.get("source_sha256") or "").startswith("builtin-")
        and cached.get("title") == title
        and cached.get("target") == selected_target
    ):
        return cached
    if (
        isinstance(cached, dict)
        and cached.get("schema_version") == ADAPTER_SCHEMA
        and cached.get("source_sha256") == source_sha256
        and cached.get("target") == selected_target
        and (selected_target != "presentation" or isinstance(cached.get("presentation_contract"), dict))
    ):
        return cached

    normalized = normalize_skill_payload({
        "title": title,
        "content": raw_content,
        "applicability": description,
    })
    steps = _compact_steps(normalized.get("steps") or [], description)
    if selected_target == "presentation":
        output_contract = "生成证据可追溯、结构清晰且可编辑的 PPTX 大纲与演示文稿。"
        success_criteria = ["每个事实页保留来源映射。", "输出通过 PPTX 结构校验。", "不执行原 Skill 中的脚本或安装命令。"]
        presentation_contract = _presentation_contract(raw_content, title, description)
    else:
        output_contract = "按照已适配步骤回答当前问题，并明确证据、缺失输入和待确认项。"
        success_criteria = ["回答遵循适配步骤。", "事实性内容保留引用。", "不执行原 Skill 中的脚本或安装命令。"]
        presentation_contract = {}
    adapter = {
        "schema_version": ADAPTER_SCHEMA,
        "source_sha256": source_sha256,
        "target": selected_target,
        "compatibility_status": "pending_review",
        "validation": {"passed": True, "mode": "deterministic_static_contract", "scripts_ignored": True},
        "title": title,
        "intent": title,
        "applicability": description[:500],
        "triggers": [item for item in normalized.get("triggers") or [] if len(item) <= 80][:12],
        "steps": steps,
        "content": "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1)),
        "output_contract": output_contract,
        "success_criteria": success_criteria,
        "presentation_contract": presentation_contract,
    }
    temporary = adapter_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(adapter, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(adapter_path)
    return adapter


def _compact_steps(values: list, fallback: str) -> list[str]:
    steps = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" -#*`")
        if not text or text.startswith(("http://", "https://")) or re.search(r"\b(?:npm|npx|pip|powershell|bash)\b", text, re.I):
            continue
        steps.append(text[:240])
        if len(steps) >= 6:
            break
    return steps or [re.sub(r"\s+", " ", fallback).strip()[:240]]


def _presentation_contract(content: str, title: str, description: str) -> dict:
    """Translate remote presentation intent into deterministic local PPTX rules."""
    text = f"{title} {description} {content}".casefold()
    source_format = "html" if re.search(r"single\s+html|html\s+file|webgl|网页|网页ppt", text) else "pptx"
    return {
        "schema_version": "souldrive_presentation_contract_v1",
        "source_format": source_format,
        "local_output_format": "pptx",
        "adaptation": "translated_to_pptx" if source_format == "html" else "native_pptx",
        "layout_profile": "editable_evidence",
        "point_limit": 3 if re.search(r"concise|简洁|要点|evidence|citation|引用|来源", text) else 4,
        "source_policy": "every_content_slide",
        "scripts": "ignored",
        "compatibility_notes": (
            ["远端是网页 HTML PPT；WebGL、演讲者视图和观众同步不会带入本地 PPTX。"]
            if source_format == "html" else []
        ),
    }


def _manifest_target(skill_dir: Path) -> str:
    try:
        payload = json.loads((skill_dir / "install_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return str(payload.get("target") or "").strip().casefold()


def _infer_target(title: str, description: str) -> str:
    searchable = f"{title} {description}".casefold()
    return "presentation" if re.search(r"pptx?|potx|powerpoint|presentation|slides?|演示文稿|幻灯片", searchable) else "conversation"


def _deduplicate_skills(records: list[dict]) -> list[dict]:
    """Keep one canonical directory when identical SKILL.md files were copied locally."""
    grouped: dict[str, list[dict]] = {}
    for record in records:
        content_key = record.get("raw_source_digest") or hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
        grouped.setdefault(content_key, []).append(record)

    unique = []
    for copies in grouped.values():
        copies.sort(key=_canonical_skill_rank)
        canonical = copies[0]
        duplicate_paths = [copy["source_path"] for copy in copies[1:]]
        if duplicate_paths:
            canonical["duplicate_source_paths"] = duplicate_paths
        unique.append(canonical)
    return sorted(unique, key=lambda record: record["source_path"].casefold())


def _canonical_skill_rank(record: dict) -> tuple:
    source_path = str(record.get("source_path") or "")
    directory = Path(source_path).parent.name
    title = str(record.get("title") or "")
    normalize = lambda value: re.sub(r"[^\w]+", "", value, flags=re.UNICODE).casefold()
    exact_name = normalize(directory) == normalize(title)
    looks_temporary = bool(re.search(r"(?:acceptance|goal[_-]|20\d{6})", source_path, re.I))
    return (not exact_name, looks_temporary, len(Path(source_path).parts), len(source_path), source_path.casefold())


def _front_matter(content: str) -> tuple[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.S)
    if not match:
        return "", ""
    fields = dict(re.findall(r"^([\w-]+):\s*[\"']?(.+?)[\"']?\s*$", match.group(1), re.M))
    return fields.get("name", "").strip(), fields.get("description", "").strip()


def _resource_manifest(skill_file: Path, root: Path) -> list[dict]:
    """Expose bounded local Skill resources without executing downloaded files."""
    skill_dir = skill_file.parent
    if skill_dir == root:
        candidates = [skill_file]
    else:
        candidates = [skill_file, *skill_dir.rglob("*")]
    records = []
    seen = set()
    for path in sorted(candidates, key=lambda item: str(item).casefold()):
        try:
            resolved = path.resolve()
            relative_to_root = resolved.relative_to(root)
            relative_to_skill = resolved.relative_to(skill_dir.resolve())
            if (
                not resolved.is_file()
                or str(resolved) in seen
                or any(part.startswith(".") for part in relative_to_root.parts)
                or any(part in {".", ".."} for part in relative_to_skill.parts)
            ):
                continue
            size_bytes = resolved.stat().st_size
            if size_bytes > MAX_RESOURCE_BYTES:
                continue
            payload = resolved.read_bytes()
        except (OSError, ValueError):
            continue
        seen.add(str(resolved))
        relative = str(relative_to_skill).replace("\\", "/")
        records.append({
            "path": relative,
            "kind": _resource_kind(resolved),
            "size_bytes": size_bytes,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "execution": "explicit_user_action_only" if resolved.suffix.casefold() in SCRIPT_SUFFIXES else "read_only",
        })
        if len(records) >= MAX_RESOURCE_FILES:
            break
    return records


def _presentation_style(skill_dir: Path) -> dict:
    adapter = skill_dir / "presentation_style.json"
    try:
        if not adapter.is_file() or adapter.stat().st_size > 16 * 1024:
            return {}
        payload = json.loads(adapter.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resource_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    name = path.name.casefold()
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in ASSET_SUFFIXES:
        return "asset"
    if suffix in {".css", ".json"} or any(term in name for term in ("style", "preset", "theme", "layout")):
        return "style"
    if suffix in {".md", ".rst", ".txt"}:
        return "instruction"
    return "resource"
