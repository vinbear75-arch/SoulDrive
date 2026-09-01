import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from pathlib import Path


DEFAULT_OUTPUT_CONTRACT = "Return a concise result that follows the skill steps and calls out any missing inputs."
DEFAULT_SUCCESS_CRITERIA = ["The task follows the approved steps.", "The result satisfies the user's current request."]
RESOURCE_KINDS = {"asset", "instruction", "resource", "script", "style"}
EXECUTABLE_SUFFIXES = {".py", ".js", ".mjs"}
MAX_SCRIPT_OUTPUT_BYTES = 256 * 1024
MAX_SCRIPT_ARGS = 12
MAX_SCRIPT_ARG_BYTES = 2048
INSTALL_SCRIPT_TOKENS = {"install", "setup", "bootstrap", "uninstall", "remove"}


def normalize_skill_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    title = str(normalized.get("title") or "Reusable skill").strip()
    content = str(normalized.get("content") or "").strip()
    steps = _string_list(normalized.get("steps")) or _extract_steps(content)
    if not steps and content:
        steps = [content]
    if not steps:
        steps = ["Clarify the task, execute the reusable process, and verify the result."]
    target = str(normalized.get("target") or "").strip().casefold()
    if target not in {"conversation", "presentation"}:
        target = "presentation" if re.search(
            r"pptx?|potx|powerpoint|presentation|slides?|演示文稿|幻灯片",
            f"{title} {normalized.get('applicability') or ''} {content[:500]}",
            re.I,
        ) else "conversation"

    normalized.update(
        {
            "title": title,
            "content": content,
            "intent": str(normalized.get("intent") or title).strip(),
            "triggers": _dedupe(_string_list(normalized.get("triggers")) + _trigger_terms(title, content)),
            "required_inputs": _string_list(normalized.get("required_inputs")),
            "applicability": str(normalized.get("applicability") or content or title).strip(),
            "steps": steps,
            "tool_permissions": _string_list(normalized.get("tool_permissions")),
            "output_contract": str(normalized.get("output_contract") or DEFAULT_OUTPUT_CONTRACT).strip(),
            "success_criteria": _string_list(normalized.get("success_criteria")) or list(DEFAULT_SUCCESS_CRITERIA),
            "failure_modes": _string_list(normalized.get("failure_modes")),
            "eval_cases": list(normalized.get("eval_cases") or []),
            "tags": _string_list(normalized.get("tags")),
            "resource_manifest": _resource_manifest(normalized.get("resource_manifest")),
            "presentation_style": _bounded_mapping(normalized.get("presentation_style")),
            "presentation_style_source": str(normalized.get("presentation_style_source") or "").strip()[:160],
            "presentation_contract": _bounded_mapping(normalized.get("presentation_contract")),
            "target": target,
            # Legacy/reflection Skills have no downloaded adapter and remain
            # usable; downloaded project Skills carry pending_review in their
            # persisted adapter until the user explicitly activates them.
            "compatibility_status": str(normalized.get("compatibility_status") or "verified").strip().casefold(),
            "adapter_path": str(normalized.get("adapter_path") or "").strip()[:260],
            "adapter_schema": str(normalized.get("adapter_schema") or "").strip()[:80],
            "raw_source_digest": str(normalized.get("raw_source_digest") or "").strip()[:128],
        }
    )
    return normalized


def match_skills(query: str, skills: list[dict[str, Any]], top_k: int = 3, min_score: float = 0.25) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    if not query_terms:
        return []

    matches = []
    for skill in skills or []:
        normalized_skill = normalize_skill_payload(skill)
        skill_terms = _terms(_skill_text(normalized_skill))
        trigger_terms = _terms(" ".join(normalized_skill.get("triggers") or []))
        if not skill_terms:
            continue
        overlap = len(query_terms & skill_terms) / max(1, len(query_terms))
        trigger_overlap = len(query_terms & trigger_terms) / max(1, len(query_terms))
        shared_term_boost = min(0.5, len(query_terms & skill_terms) * 0.08)
        score = min(1.0, overlap + trigger_overlap * 0.5 + shared_term_boost)
        if score < min_score:
            continue
        matches.append(
            {
                "skill_id": normalized_skill.get("skill_id") or normalized_skill.get("asset_id") or "",
                "title": normalized_skill.get("title") or "",
                "score": round(score, 4),
                "reason": _match_reason(query_terms, normalized_skill),
                "skill": normalized_skill,
            }
        )

    matches.sort(key=lambda item: (-item["score"], item["title"], item["skill_id"]))
    unique_matches = []
    seen_ids = set()
    for match in matches:
        skill_key = str(match["skill_id"] or match["title"]).casefold()
        if skill_key in seen_ids:
            continue
        seen_ids.add(skill_key)
        unique_matches.append(match)
    return unique_matches[:top_k]


def create_skill_execution_plan(
    skill: dict[str, Any],
    query: str,
    *,
    matched_score: float = 0.0,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_skill = normalize_skill_payload(skill)
    safe_resources = [
        item for item in normalized_skill.get("resource_manifest") or []
        if item.get("kind") != "script"
    ]
    return {
        "skill_id": normalized_skill.get("skill_id") or normalized_skill.get("asset_id") or "",
        "skill_title": normalized_skill.get("title") or "",
        "status": "planned",
        "query": str(query or ""),
        "matched_score": matched_score,
        "required_inputs": list(normalized_skill.get("required_inputs") or []),
        "inputs": dict(inputs or {}),
        "steps": [
            {
                "index": index + 1,
                "instruction": step,
                "expected_output": "Step result or verification note.",
            }
            for index, step in enumerate(normalized_skill.get("steps") or [])
        ],
        "tool_permissions": list(normalized_skill.get("tool_permissions") or []),
        "output_contract": normalized_skill.get("output_contract") or DEFAULT_OUTPUT_CONTRACT,
        "success_criteria": list(normalized_skill.get("success_criteria") or DEFAULT_SUCCESS_CRITERIA),
        "failure_modes": list(normalized_skill.get("failure_modes") or []),
        "resources": safe_resources,
        "resource_policy": "原 Skill 脚本不进入本地小模型运行契约。",
        "execution": {
            "mode": "plan_only",
            "script_count": 0,
            "result": "只注入自动编译并验证的短契约；原始脚本不会执行。",
        },
    }


def execute_skill_script(
    skills_path: str,
    skill: dict[str, Any],
    script_path: str,
    *,
    args: list[str] | None = None,
    input_payload: dict[str, Any] | None = None,
    output_directory: str | None = None,
    allowed_output_root: str | None = None,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = MAX_SCRIPT_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one explicitly requested, reviewed Skill helper without a shell.

    A project Skill is active only after it has been synchronized into the
    encrypted store.  The resource manifest and digest are checked again here
    so a changed local file cannot silently become executable code.
    """
    started = time.monotonic()
    normalized = normalize_skill_payload(skill)
    result: dict[str, Any] = {
        "status": "rejected",
        "script_path": str(script_path or "").replace("\\", "/"),
        "returncode": None,
        "duration_ms": 0.0,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "error": "",
    }

    def reject(message: str) -> dict[str, Any]:
        result["error"] = message
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    try:
        from core.project_skill_installer import _resolve_project_skill_source_path

        root = Path(skills_path).resolve()
        source = _resolve_project_skill_source_path(root, str(normalized.get("source_path") or ""))
        skill_dir = source.parent
        relative = str(script_path or "").strip().replace("\\", "/")
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
            return reject("Skill 脚本路径必须是 Skill 根目录内的安全相对路径")
        script = (skill_dir / relative_path).resolve()
        if skill_dir not in script.parents or not script.is_file():
            return reject("Skill 脚本必须位于已审核 Skill 根目录内")
        suffix = script.suffix.casefold()
        if suffix not in EXECUTABLE_SUFFIXES:
            return reject("仅允许执行 .py、.js、.mjs Skill 脚本")
        tokens = set(re.split(r"[-_.]+", script.stem.casefold()))
        if tokens & INSTALL_SCRIPT_TOKENS:
            return reject("安装、卸载或初始化脚本禁止执行")
        manifest = next(
            (
                item for item in normalized.get("resource_manifest") or []
                if str(item.get("path") or "").replace("\\", "/").casefold() == relative.casefold()
                and str(item.get("kind") or "").casefold() == "script"
            ),
            None,
        )
        if manifest is None:
            return reject("脚本未在已审核 Skill 资源清单中声明")
        expected_digest = str(manifest.get("sha256") or "").strip().lower()
        if expected_digest and hashlib.sha256(script.read_bytes()).hexdigest() != expected_digest:
            return reject("Skill 脚本校验失败，文件已变化")

        safe_args = [str(value) for value in (args or [])]
        if len(safe_args) > MAX_SCRIPT_ARGS or any("\x00" in value or len(value.encode("utf-8")) > MAX_SCRIPT_ARG_BYTES for value in safe_args):
            return reject("Skill 脚本参数数量或长度超出限制")
        try:
            timeout = max(0.1, min(float(timeout_seconds), 120.0))
            output_limit = max(1, min(int(max_output_bytes), MAX_SCRIPT_OUTPUT_BYTES))
        except (TypeError, ValueError):
            return reject("Skill 脚本执行限制参数无效")

        output_root = Path(allowed_output_root or skill_dir).resolve()
        output_dir = Path(output_directory or (skill_dir / ".souldrive-output")).resolve()
        if output_dir != output_root and output_root not in output_dir.parents:
            return reject("Skill 脚本输出目录必须位于允许的本地输出根目录内")
        output_dir.mkdir(parents=True, exist_ok=True)

        if suffix == ".py":
            command = [sys.executable, str(script), *safe_args]
        else:
            node = shutil.which("node")
            if not node:
                return reject("未找到 Node.js，无法执行 JavaScript Skill 脚本")
            command = [node, str(script), *safe_args]
        env = {
            "PATH": os.environ.get("PATH", ""),
            # CPython on Windows needs SystemRoot to initialize its secure
            # randomness provider; these values are runtime plumbing, not
            # user secrets or project configuration.
            "SystemRoot": os.environ.get("SystemRoot", ""),
            "WINDIR": os.environ.get("WINDIR", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        process = subprocess.Popen(
            command,
            cwd=str(skill_dir),
            env=env,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        payload = json.dumps(input_payload or {}, ensure_ascii=False).encode("utf-8")
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                process.stdin.close()
            except OSError:
                pass

        captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"value": False}

        def drain(name: str, stream) -> None:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                remaining = output_limit - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated["value"] = True

        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True) for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            returncode = process.wait()
        for reader in readers:
            reader.join(timeout=2.0)
        result.update({
            "status": "timed_out" if timed_out else ("completed" if returncode == 0 else "failed"),
            "returncode": returncode,
            "stdout": bytes(captured["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(captured["stderr"]).decode("utf-8", errors="replace"),
            "output_truncated": truncated["value"],
        })
        if timed_out:
            result["error"] = f"Skill 脚本执行超时（限制 {timeout:g} 秒）"
        elif returncode != 0:
            result["error"] = (result["stderr"] or result["stdout"] or f"Skill 脚本退出码 {returncode}").strip()[-1000:]
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return result
    except Exception as exc:
        return reject(f"Skill 脚本执行被拒绝：{exc}")


def _extract_steps(content: str) -> list[str]:
    normalized = str(content or "").strip()
    if not normalized:
        return []
    front_matter = re.match(r"^---\s*\n.*?\n---\s*\n?(.*)$", normalized, re.S)
    if front_matter:
        normalized = front_matter.group(1).strip()
    if not normalized:
        return []
    lines = []
    for raw_line in normalized.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line).strip()
        if line:
            lines.append(line)
    if len(lines) > 1:
        return lines
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    return sentences if len(sentences) > 1 else lines


def _trigger_terms(title: str, content: str) -> list[str]:
    text = f"{title} {content}".lower()
    triggers = []
    for marker in ("tdd", "test", "implement", "workflow", "troubleshoot", "template", "review"):
        if marker in text:
            triggers.append(marker)
    return triggers


def _match_reason(query_terms: set[str], skill: dict[str, Any]) -> str:
    matched = sorted(query_terms & _terms(_skill_text(skill)))
    if not matched:
        return "matched by approved skill metadata"
    return f"matched terms: {', '.join(matched[:5])}"


def _skill_text(skill: dict[str, Any]) -> str:
    parts = [
        skill.get("title"),
        skill.get("content"),
        skill.get("intent"),
        skill.get("applicability"),
        " ".join(skill.get("triggers") or []),
        " ".join(skill.get("steps") or []),
        " ".join(skill.get("tags") or []),
    ]
    return " ".join(str(part or "") for part in parts)


def _string_list(values: Any) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        marker = value.lower()
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _resource_manifest(values: Any) -> list[dict[str, Any]]:
    result = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "").strip().replace("\\", "/")[:240]
        if not path or any(part in {"", ".", ".."} for part in path.split("/")):
            continue
        kind = str(value.get("kind") or "resource").strip().lower()
        if kind not in RESOURCE_KINDS:
            kind = "resource"
        try:
            size_bytes = max(0, min(int(value.get("size_bytes") or 0), 512 * 1024))
        except (TypeError, ValueError):
            size_bytes = 0
        result.append({
            "path": path,
            "kind": kind,
            "size_bytes": size_bytes,
            "sha256": str(value.get("sha256") or "").strip()[:64],
            "execution": "explicit_user_action_only" if kind == "script" else "read_only",
        })
        if len(result) >= 48:
            break
    return result


def _bounded_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in list(value.items())[:24]:
        name = str(key or "").strip()[:80]
        if not name:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = str(item)[:240] if isinstance(item, str) else item
        elif isinstance(item, list):
            result[name] = item[:12]
    return result


def _terms(value: Any) -> set[str]:
    stopwords = {"the", "and", "with", "this", "that", "does", "what", "about", "please"}
    text = str(value or "").lower()
    terms = {term for term in re.findall(r"[a-z0-9_]{3,}", text) if term not in stopwords}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        max_size = min(4, len(chunk))
        for size in range(2, max_size + 1):
            for index in range(0, len(chunk) - size + 1):
                terms.add(chunk[index : index + size])
    bilingual_aliases = {
        "实现": {"implement", "implementation"},
        "功能": {"feature"},
        "测试": {"test", "tests", "testing"},
        "编写": {"write", "writing"},
        "流程": {"workflow"},
        "计划": {"plan"},
    }
    for chinese, aliases in bilingual_aliases.items():
        if chinese in text:
            terms.update(aliases)
    return terms
