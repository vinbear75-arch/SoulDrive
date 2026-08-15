import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from core.paths import PROJECT_ROOT, app_root
from core.openxml_presentation import render_presentation
from core.secure_vector_store import SecureVectorStore
from core.task_journal import TaskJournal


PRESENTATION_STEPS = (
    "collect_evidence",
    "build_outline",
    "render_pptx",
    "verify_pptx",
    "open_in_wps",
)
ALLOWED_AUDIENCES = {"management", "technical", "government", "general"}
SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]{12,220}[。！？!?；;]?")
NOISE_PATTERN = re.compile(
    r"^(?:目录|目\s*录|contents|copyright|版权所有|第\s*\d+\s*页|page\s+\d+)$",
    flags=re.IGNORECASE,
)
TERM_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,6}")
STOP_TERMS = {
    "以及", "通过", "进行", "相关", "主要", "可以", "需要", "包括", "其中", "对于",
    "本项目", "本方案", "文件", "项目", "工作", "建设", "实施", "要求", "内容", "管理",
    "the", "and", "for", "with", "from", "this", "that",
}


class PresentationService:
    def __init__(
        self,
        workspace,
        keys,
        *,
        audit_logger=None,
        outline_generator: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace
        self.keys = keys
        self.audit_logger = audit_logger
        self.outline_generator = outline_generator

    def create_task(self, request: dict[str, Any]) -> dict[str, Any]:
        inputs = validate_presentation_request(request)
        task_id = f"task_{os.urandom(10).hex()}"
        inputs["task_id"] = task_id
        journal = TaskJournal(self.workspace, self.keys)
        try:
            task = journal.create_task(
                "presentation.generate",
                inputs,
                PRESENTATION_STEPS,
                task_id=task_id,
            )
            self._audit("presentation.task_created", task, {"source_count": len(inputs["source_filenames"])})
            return task
        finally:
            journal.close()

    def run_task(self, task_id: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            handlers = {
                "collect_evidence": self._collect_evidence_step,
                "build_outline": self._build_outline_step,
                "render_pptx": self._render_pptx_step,
                "verify_pptx": self._verify_pptx_step,
                "open_in_wps": self._open_in_wps_step,
            }
            result = journal.run_steps(
                task_id,
                f"{os.getpid()}:{uuid.uuid4().hex[:8]}",
                handlers,
                lambda outputs: {
                    "output_path": outputs["render_pptx"]["output_path"],
                    "slide_count": outputs["verify_pptx"]["slide_count"],
                    "renderer": outputs["render_pptx"]["renderer"],
                    "opened_in_wps": outputs["open_in_wps"]["opened"],
                    "source_count": outputs["collect_evidence"]["source_count"],
                },
            )
            event = "presentation.task_completed" if result["status"] == "completed" else "presentation.task_failed"
            self._audit(event, result, {"error": result.get("error")})
            return result
        finally:
            journal.close()

    def resume_task(self, task_id: str) -> dict[str, Any]:
        return self.run_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            return journal.public_task(task_id)
        finally:
            journal.close()

    def list_tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            return journal.list_tasks(kind="presentation.generate", limit=limit)
        finally:
            journal.close()

    def _collect_evidence_step(self, inputs: dict[str, Any], _outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        selected = inputs["source_filenames"]
        records = collect_indexed_evidence(self.workspace, self.keys, selected)
        found_sources = {record["source_filename"] for record in records}
        missing = [source for source in selected if source not in found_sources]
        if missing:
            raise ValueError(f"selected documents are not indexed: {', '.join(missing[:3])}")
        return {
            "records": records,
            "source_count": len(found_sources),
            "record_count": len(records),
        }

    def _build_outline_step(self, inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        records = outputs["collect_evidence"]["records"]
        if self.outline_generator is not None:
            try:
                generated = self.outline_generator(records, inputs)
                return validate_presentation_plan(generated, inputs)
            except Exception:
                pass
        return build_extractable_presentation_plan(records, inputs)

    def _render_pptx_step(self, inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        plan = outputs["build_outline"]
        output_directory = Path(self.workspace.presentations_path).resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        filename = presentation_filename(
            inputs["title"],
            inputs["source_filenames"],
            inputs["task_id"],
        )
        output_path = output_directory / filename
        script_path = resolve_wps_render_script()
        expected_slide_count = len(plan["slides"])
        if output_path.exists() and verify_pptx(output_path, expected_slide_count)["valid"]:
            return {
                "output_path": str(output_path),
                "renderer": "recovered",
                "planned_slide_count": expected_slide_count,
            }
        if os.environ.get("SOULDRIVE_PRESENTATION_RENDERER", "openxml").strip().lower() != "com":
            payload = render_presentation(plan, output_path)
            return {
                "output_path": str(output_path),
                "renderer": payload["renderer"],
                "planned_slide_count": expected_slide_count,
            }
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-OutputPath",
                str(output_path),
            ],
            input=json.dumps(plan, ensure_ascii=False),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=180,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "WPS presentation rendering failed").strip()
            raise RuntimeError(error[-1000:])
        payload = _last_json_object(completed.stdout)
        return {
            "output_path": str(output_path),
            "renderer": str(payload.get("renderer") or "WPS"),
            "planned_slide_count": len(plan["slides"]),
        }

    def _verify_pptx_step(self, _inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        output_path = Path(outputs["render_pptx"]["output_path"])
        expected = int(outputs["render_pptx"]["planned_slide_count"])
        report = verify_pptx(output_path, expected)
        if not report["valid"]:
            raise RuntimeError("generated PPTX failed validation: " + ", ".join(report["errors"]))
        return report

    def _open_in_wps_step(self, inputs: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not inputs.get("open_in_wps"):
            return {"opened": False, "reason": "not_requested"}
        output_path = outputs["render_pptx"]["output_path"]
        marker = Path(self.workspace.runtime_path) / f"{inputs['task_id']}.wps-opened"
        if marker.exists():
            return {"opened": False, "reason": "already_requested"}
        executable = find_wps_presentation_executable()
        if not executable:
            raise RuntimeError("WPS presentation executable was not found")
        marker.write_text(str(time.time()), encoding="ascii")
        try:
            subprocess.Popen(
                [executable, output_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            marker.unlink(missing_ok=True)
            raise
        return {"opened": True, "application": "WPS", "executable_name": Path(executable).name}

    def _audit(self, event_type: str, task: dict[str, Any], extra: dict[str, Any]) -> None:
        if self.audit_logger is None:
            return
        self.audit_logger.append_event(
            event_type,
            {
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                **extra,
            },
        )


def validate_presentation_request(request: dict[str, Any]) -> dict[str, Any]:
    sources = []
    for value in request.get("source_filenames") or []:
        source = Path(str(value or "").strip()).name
        if source and source not in sources:
            sources.append(source)
    if not 1 <= len(sources) <= 12:
        raise ValueError("select between 1 and 12 indexed documents")
    title = " ".join(str(request.get("title") or "").split())[:120]
    audience = str(request.get("audience") or "management").strip().lower()
    if audience not in ALLOWED_AUDIENCES:
        raise ValueError("unsupported presentation audience")
    try:
        max_slides = int(request.get("max_slides") or 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_slides must be an integer") from exc
    max_slides = max(4, min(max_slides, 16))
    return {
        "source_filenames": sources,
        "title": title,
        "audience": audience,
        "max_slides": max_slides,
        "open_in_wps": bool(request.get("open_in_wps", True)),
    }


def collect_indexed_evidence(workspace, keys, selected_sources: list[str]) -> list[dict[str, Any]]:
    selected = {Path(source).name.casefold(): Path(source).name for source in selected_sources}
    records: list[dict[str, Any]] = []
    secure_path = Path(workspace.secure_vector_store_path)
    if keys is not None and secure_path.exists():
        store = SecureVectorStore(str(secure_path), keys)
        try:
            payload = store.collection.get(include=["metadatas"])
            parent_ids: dict[str, dict[str, Any]] = {}
            for metadata in payload.get("metadatas") or []:
                source = Path(str((metadata or {}).get("source_filename") or "")).name
                parent_id = str((metadata or {}).get("parent_id") or "")
                if source.casefold() in selected and parent_id:
                    parent_ids.setdefault(parent_id, dict(metadata or {}))
            for parent_id, child_metadata in parent_ids.items():
                parent = store.get_parent(parent_id)
                if parent:
                    records.append(_evidence_record(parent_id, parent["content"], {**child_metadata, **parent["metadata"]}))
        finally:
            store.close()
    else:
        db_path = Path(workspace.parent_doc_path)
        if not db_path.exists():
            raise FileNotFoundError("parent document index is not available")
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT parent_id, content, metadata_json FROM parent_documents"
            ).fetchall()
        for parent_id, content, metadata_json in rows:
            metadata = json.loads(metadata_json)
            source = Path(str(metadata.get("source_filename") or "")).name
            if source.casefold() in selected:
                records.append(_evidence_record(parent_id, content, metadata))

    records.sort(
        key=lambda item: (
            selected_sources.index(item["source_filename"]),
            item["page_number"],
            item["parent_index"],
        )
    )
    limited = []
    per_source: Counter[str] = Counter()
    for record in records:
        if per_source[record["source_filename"]] >= 18:
            continue
        if len(limited) >= 96:
            break
        limited.append(record)
        per_source[record["source_filename"]] += 1
    return limited


def build_extractable_presentation_plan(
    records: list[dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    sources = inputs["source_filenames"]
    by_source = {source: [] for source in sources}
    for record in records:
        by_source.setdefault(record["source_filename"], []).append(record)

    title = inputs["title"] or _default_title(sources)
    audience_label = {
        "management": "管理决策汇报",
        "technical": "技术方案汇报",
        "government": "政策与项目汇报",
        "general": "专题汇报",
    }[inputs["audience"]]
    slides: list[dict[str, Any]] = [
        {
            "kind": "cover",
            "eyebrow": audience_label,
            "title": _compact(title, 54),
            "subtitle": _compact(f"基于 {len(sources)} 份已入库文档生成", 80),
            "points": [],
            "sources": sources,
            "footer": "",
        }
    ]

    summary_points = []
    for source in sources:
        sentences = _ranked_sentences(by_source.get(source) or [])
        if sentences:
            summary_points.append(f"{Path(source).stem}：{sentences[0]}")
    slides.append(
        _content_slide(
            "执行摘要",
            summary_points[:5] or ["所选文档缺少可用于摘要的结构化正文。"],
            sources,
            "摘要内容直接提炼自所选文档",
        )
    )

    remaining_content_slots = max(1, inputs["max_slides"] - 3)
    source_slots = min(len(sources), remaining_content_slots)
    for source in sources[:source_slots]:
        source_records = by_source.get(source) or []
        points = _ranked_sentences(source_records)[:5]
        section = next((item["section"] for item in source_records if item["section"]), "文档要点")
        slides.append(
            _content_slide(
                _compact(Path(source).stem, 38),
                points or ["未提取到足够的结构化正文。"],
                [source],
                _compact(section, 70),
            )
        )

    if len(sources) > 1 and len(slides) < inputs["max_slides"] - 1:
        common_terms = _common_terms(records, limit=5)
        observations = [f"共同关注主题：{term}" for term in common_terms]
        if not observations:
            observations = ["所选文档的共同主题需要结合原文证据进一步核对。"]
        slides.append(
            _content_slide(
                "跨文档综合观察",
                observations,
                sources,
                "仅呈现多文档中重复出现的主题词",
            )
        )

    slides = slides[: inputs["max_slides"] - 1]
    slides.append(
        {
            "kind": "sources",
            "title": "资料来源",
            "points": [],
            "sources": [_compact(source, 96) for source in sources],
            "footer": "所有内容均来自本地已入库文档",
        }
    )
    return validate_presentation_plan(
        {
            "schema_version": "souldrive_presentation_plan_v1",
            "generated_at_label": time.strftime("%Y-%m-%d %H:%M"),
            "title": title,
            "slides": slides,
        },
        inputs,
    )


def validate_presentation_plan(plan: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or not isinstance(plan.get("slides"), list):
        raise ValueError("invalid presentation plan")
    selected = set(inputs["source_filenames"])
    slides = []
    for index, raw_slide in enumerate(plan["slides"][: inputs["max_slides"]]):
        if not isinstance(raw_slide, dict):
            continue
        kind = str(raw_slide.get("kind") or ("cover" if index == 0 else "content"))
        if kind not in {"cover", "content", "sources"}:
            kind = "content"
        title = _compact(raw_slide.get("title") or "未命名", 60)
        points = [
            _compact(point, 105)
            for point in raw_slide.get("points") or []
            if str(point or "").strip()
        ][:5]
        sources = [
            Path(str(source or "")).name
            for source in raw_slide.get("sources") or []
            if Path(str(source or "")).name in selected
        ]
        if kind != "cover" and not sources:
            sources = inputs["source_filenames"][:3]
        slides.append(
            {
                "kind": kind,
                "eyebrow": _compact(raw_slide.get("eyebrow") or "专题汇报", 40),
                "title": title,
                "subtitle": _compact(raw_slide.get("subtitle") or "", 100),
                "points": points,
                "sources": sources[:12] if kind == "sources" else sources[:4],
                "footer": _compact(raw_slide.get("footer") or "SoulDrive 本地证据链", 90),
            }
        )
    if len(slides) < 2:
        raise ValueError("presentation plan requires at least two slides")
    if slides[0]["kind"] != "cover":
        slides[0]["kind"] = "cover"
    return {
        "schema_version": "souldrive_presentation_plan_v1",
        "generated_at_label": _compact(plan.get("generated_at_label") or time.strftime("%Y-%m-%d %H:%M"), 40),
        "title": _compact(plan.get("title") or inputs.get("title") or "专题汇报", 120),
        "slides": slides,
    }


def verify_pptx(path: Path, expected_slide_count: int) -> dict[str, Any]:
    errors = []
    actual_count = 0
    if not path.exists():
        errors.append("file_missing")
    elif path.stat().st_size < 8_000:
        errors.append("file_too_small")
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "ppt/presentation.xml" not in names:
                    errors.append("presentation_xml_missing")
                actual_count = sum(
                    1
                    for name in names
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                )
        except zipfile.BadZipFile:
            errors.append("invalid_openxml_package")
    if actual_count != expected_slide_count:
        errors.append("slide_count_mismatch")
    return {
        "valid": not errors,
        "output_path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "slide_count": actual_count,
        "expected_slide_count": expected_slide_count,
        "errors": errors,
    }


def find_wps_presentation_executable() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        registry_paths = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wpp.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wpp.exe"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wps.exe"),
        )
        for hive, path in registry_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    executable = str(winreg.QueryValueEx(key, None)[0])
                    if Path(executable).exists():
                        return executable
            except OSError:
                continue
    except ImportError:
        pass
    return None


def resolve_wps_render_script() -> Path:
    candidates = [
        app_root() / "scripts" / "render_pptx_wps.ps1",
        PROJECT_ROOT / "scripts" / "render_pptx_wps.ps1",
        Path(getattr(sys, "_MEIPASS", app_root())) / "scripts" / "render_pptx_wps.ps1",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("WPS presentation renderer script is missing")


def presentation_filename(title: str, sources: list[str], task_id: str) -> str:
    base = title or Path(sources[0]).stem or "SoulDrive-presentation"
    safe = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", base).strip(" ._")[:80]
    suffix = re.sub(r"[^A-Za-z0-9]", "", task_id)[-8:] or "task"
    return f"{safe or 'SoulDrive-presentation'}-{suffix}.pptx"


def _evidence_record(parent_id: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(metadata.get("source_filename") or metadata.get("document_title") or "")).name
    section_path = metadata.get("section_path")
    if isinstance(section_path, list):
        section = " > ".join(str(item) for item in section_path if str(item).strip())
    else:
        section = str(
            metadata.get("section")
            or metadata.get("Header 3")
            or metadata.get("Header 2")
            or metadata.get("Header 1")
            or ""
        )
    return {
        "parent_id": str(parent_id),
        "source_filename": source,
        "document_title": str(metadata.get("document_title") or Path(source).stem),
        "section": _compact(section, 200),
        "page_number": _safe_int(metadata.get("page_number") or metadata.get("page"), 0),
        "parent_index": _safe_int(metadata.get("parent_index"), 0),
        "content": _compact(content, 4200),
    }


def _ranked_sentences(records: list[dict[str, Any]]) -> list[str]:
    candidates = []
    seen = set()
    for record in records:
        for sentence in SENTENCE_PATTERN.findall(str(record.get("content") or "")):
            normalized = " ".join(sentence.split()).strip(" -•")
            key = re.sub(r"\W+", "", normalized).casefold()
            if len(normalized) < 16 or len(normalized) > 170 or NOISE_PATTERN.match(normalized):
                continue
            if key in seen:
                continue
            seen.add(key)
            terms = [term.casefold() for term in TERM_PATTERN.findall(normalized)]
            score = len(set(terms)) + (2 if any(char.isdigit() for char in normalized) else 0)
            candidates.append((score, normalized))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return [_compact(sentence, 105) for _, sentence in candidates]


def _common_terms(records: list[dict[str, Any]], limit: int) -> list[str]:
    sources_by_term: dict[str, set[str]] = {}
    counts: Counter[str] = Counter()
    for record in records:
        source = record["source_filename"]
        for raw_term in set(TERM_PATTERN.findall(str(record.get("content") or ""))):
            term = raw_term.casefold()
            if term in STOP_TERMS or len(term) < 2:
                continue
            sources_by_term.setdefault(term, set()).add(source)
            counts[term] += 1
    ranked = [
        term
        for term, _count in counts.most_common()
        if len(sources_by_term.get(term) or ()) >= 2
    ]
    return ranked[:limit]


def _content_slide(title: str, points: list[str], sources: list[str], footer: str) -> dict[str, Any]:
    return {
        "kind": "content",
        "title": _compact(title, 54),
        "points": [_compact(point, 105) for point in points[:5]],
        "sources": sources[:4],
        "footer": _compact(footer, 90),
    }


def _default_title(sources: list[str]) -> str:
    if len(sources) == 1:
        return Path(sources[0]).stem
    return "多文档综合分析汇报"


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(str(output or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _compact(value: Any, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
