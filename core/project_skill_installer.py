"""Download project Skills into an isolated review inbox.

Remote Skill files are untrusted instructions.  This module only downloads and
stores them; it never imports Python, runs shell commands, or activates a
Skill.  Activation is a separate, explicit move performed after review.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from core.atomic_file import atomic_write_text


MAX_FILES = 64
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
# GitHub repository archives include the repository wrapper even when the
# selected Skill subtree is small. Keep the download bounded, but do not force
# a raw-SKILL-only fallback for otherwise valid public Skill packages.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
GITHUB_HOSTS = {
    "github.com",
    "www.github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillInstallError(ValueError):
    """Raised when a remote Skill cannot pass the local installation policy."""


def install_skill_to_inbox(
    skills_path: str,
    source: str,
    skill_name: str | None = None,
    target: str = "conversation",
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    skill_target = _skill_target(target)
    spec = _parse_source(source)
    files = _download_skill_files(spec, opener or urllib.request.urlopen)
    skill_rel = _find_skill_file(files)
    skill_text = files[skill_rel].decode("utf-8-sig", errors="strict")
    metadata = _skill_metadata(skill_text)
    if not metadata["name"] or not metadata["description"]:
        raise SkillInstallError("SKILL.md must define name and description")

    name = _safe_name(skill_name or _skill_slug(metadata["name"]) or spec["name"])
    root = Path(skills_path).resolve()
    inbox = root / ".inbox"
    target_path = inbox / name
    root.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        if _inbox_entry_matches(
            target_path,
            source=str(source).strip(),
            target=skill_target,
            files=files,
            skill_rel=skill_rel,
        ):
            return _install_result(
                status="pending_review",
                replayed=True,
                name=name,
                title=metadata["name"],
                description=metadata["description"],
                source=str(source).strip(),
                target=skill_target,
                path=target_path,
                files=files,
            )
        raise SkillInstallError(
            f"Skill inbox entry already exists with different content: {name}"
        )

    temporary = inbox / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.mkdir()
        for relative, payload in files.items():
            destination = _safe_child(temporary, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        manifest = {
            "schema_version": "souldrive_project_skill_inbox_v1",
            "skill_name": name,
            "skill_title": metadata["name"],
            "description": metadata["description"],
            "source": str(source).strip(),
            "target": skill_target,
            "downloaded_at": time.time(),
            "files": sorted(files),
            "sha256": hashlib.sha256(files[skill_rel]).hexdigest(),
        }
        (temporary / "install_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.replace(target_path)
        except FileExistsError:
            if _inbox_entry_matches(
                target_path,
                source=str(source).strip(),
                target=skill_target,
                files=files,
                skill_rel=skill_rel,
            ):
                replay = _install_result(
                    status="pending_review",
                    replayed=True,
                    name=name,
                    title=metadata["name"],
                    description=metadata["description"],
                    source=str(source).strip(),
                    target=skill_target,
                    path=target_path,
                    files=files,
                )
                shutil.rmtree(temporary, ignore_errors=True)
                return replay
            raise SkillInstallError(
                f"Skill inbox entry was created concurrently with different content: {name}"
            )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return _install_result(
        status="pending_review",
        replayed=False,
        name=name,
        title=metadata["name"],
        description=metadata["description"],
        source=str(source).strip(),
        target=skill_target,
        path=target_path,
        files=files,
    )


def _install_result(
    *,
    status: str,
    replayed: bool,
    name: str,
    title: str,
    description: str,
    source: str,
    target: str,
    path: Path,
    files: dict[str, bytes],
) -> dict[str, Any]:
    return {
        "status": status,
        "replayed": bool(replayed),
        "skill_name": name,
        "skill_title": title,
        "description": description,
        "source": source,
        "target": target,
        "path": str(path),
        "files": sorted(files),
    }


def _inbox_entry_matches(
    target_path: Path,
    *,
    source: str,
    target: str,
    files: dict[str, bytes],
    skill_rel: str,
) -> bool:
    """Recognize a completed inbox write without trusting the display name alone."""
    manifest_path = target_path / "install_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return False
        if str(manifest.get("source") or "").strip() != source:
            return False
        if _skill_target(str(manifest.get("target") or "conversation")) != target:
            return False
        if sorted(str(item) for item in manifest.get("files") or []) != sorted(files):
            return False
        if str(manifest.get("sha256") or "") != hashlib.sha256(files[skill_rel]).hexdigest():
            return False
        for relative, payload in files.items():
            candidate = _safe_child(target_path, relative)
            if not candidate.is_file() or candidate.read_bytes() != payload:
                return False
    except (OSError, UnicodeError, json.JSONDecodeError, SkillInstallError, ValueError):
        return False
    return True


def list_skill_inbox(skills_path: str) -> list[dict[str, Any]]:
    inbox = Path(skills_path).resolve() / ".inbox"
    if not inbox.is_dir():
        return []
    entries = []
    for directory in sorted(inbox.iterdir(), key=lambda item: item.name.casefold()):
        if not directory.is_dir() or not SAFE_NAME.fullmatch(directory.name):
            continue
        if (inbox.parent / directory.name).is_dir():
            continue
        manifest_path = directory / "install_manifest.json"
        skill_path = _find_local_skill_file(directory)
        if not manifest_path.is_file() or skill_path is None:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata = _skill_metadata(skill_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError, SkillInstallError):
            continue
        entries.append({
            "skill_name": directory.name,
            "skill_title": metadata["name"] or manifest.get("skill_title") or directory.name,
            "description": metadata["description"] or manifest.get("description") or "",
            "source": str(manifest.get("source") or ""),
            "target": _skill_target(str(manifest.get("target") or "conversation")),
            "files": list(manifest.get("files") or []),
            "downloaded_at": manifest.get("downloaded_at"),
        })
    return entries


def approve_skill_from_inbox(
    skills_path: str,
    skill_name: str,
    *,
    mark_review: bool = True,
) -> dict[str, Any]:
    root = Path(skills_path).resolve()
    name = _safe_name(skill_name)
    source = _safe_child(root / ".inbox", name)
    if not source.is_dir():
        legacy_name = _safe_legacy_name(skill_name)
        if legacy_name:
            name = legacy_name
            source = _safe_child(root / ".inbox", name)
    if not source.is_dir():
        raise SkillInstallError("Skill inbox entry was not found")
    try:
        manifest = json.loads((source / "install_manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillInstallError("Skill install manifest is invalid") from exc
    previously_reviewed = str(manifest.get("review_status") or "") == "adapter_ready"
    requested_target = str(manifest.get("target") or "").strip() or None
    from core.project_skills import compile_local_skill_adapter

    skill_file = _find_local_skill_file(source)
    if skill_file is None:
        raise SkillInstallError("待审核 Skill 必须包含且只能包含一个 SKILL.md")
    adapter = compile_local_skill_adapter(skill_file, target=requested_target)
    target = adapter["target"]
    if mark_review and manifest:
        _mark_review_ready(source, manifest)
    return {
        "status": "pending_review",
        "replayed": previously_reviewed,
        "skill_name": name,
        "path": str(source),
        "target": target,
        "compatibility_status": adapter["compatibility_status"],
        "adapter_path": str(source / "souldrive_skill.json"),
        "adapter": adapter,
        "presentation_adapter_created": False,
    }


def _mark_review_ready(source: Path, manifest: dict[str, Any]) -> None:
    if str(manifest.get("review_status") or "") == "adapter_ready":
        return
    manifest_path = source / "install_manifest.json"
    updated = {
        **manifest,
        "review_status": "adapter_ready",
        "reviewed_at": time.time(),
    }
    atomic_write_text(
        manifest_path,
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
    )


def activate_skill_from_inbox(skills_path: str, skill_name: str) -> dict[str, Any]:
    """Move a reviewed inbox entry into the active Skill directory."""
    from core.project_skills import compile_local_skill_adapter

    root = Path(skills_path).resolve()
    name = _safe_name(skill_name)
    source = _safe_child(root / ".inbox", name)
    destination = _safe_child(root, name)
    if not source.is_dir():
        replay = _active_skill_replay(destination, name)
        if replay is not None:
            return replay
        raise SkillInstallError("Skill inbox entry was not found")
    if destination.exists():
        raise SkillInstallError("A project Skill with this name already exists")
    skill_file = _find_local_skill_file(source)
    if skill_file is None:
        raise SkillInstallError("待审核 Skill 必须包含且只能包含一个 SKILL.md")
    try:
        manifest = json.loads((source / "install_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = {}
    adapter = compile_local_skill_adapter(skill_file, target=str(manifest.get("target") or "") or None)
    adapter["compatibility_status"] = "verified"
    atomic_write_text(
        skill_file.parent / "souldrive_skill.json",
        json.dumps(adapter, ensure_ascii=False, indent=2) + "\n",
    )
    try:
        source.replace(destination)
    except FileExistsError:
        replay = _active_skill_replay(destination, name)
        if replay is not None:
            return replay
        raise SkillInstallError(
            f"A project Skill with this name was created concurrently: {name}"
        )
    return {
        "status": "active",
        "replayed": False,
        "skill_name": name,
        "path": str(destination),
        "target": adapter["target"],
        "compatibility_status": adapter["compatibility_status"],
        "adapter_path": str(destination / "souldrive_skill.json"),
    }


def _active_skill_replay(destination: Path, name: str) -> dict[str, Any] | None:
    """Return a durable activation result only for a verified moved inbox entry."""
    if not destination.is_dir():
        return None
    try:
        manifest = json.loads(
            (destination / "install_manifest.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict) or str(manifest.get("skill_name") or "") != name:
            return None
        skill_file = _find_local_skill_file(destination)
        adapter_path = skill_file.parent / "souldrive_skill.json" if skill_file else None
        if skill_file is None or adapter_path is None or not adapter_path.is_file():
            return None
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        if not isinstance(adapter, dict) or adapter.get("compatibility_status") != "verified":
            return None
        target = _skill_target(str(adapter.get("target") or manifest.get("target") or "conversation"))
    except (OSError, UnicodeError, json.JSONDecodeError, SkillInstallError):
        return None
    return {
        "status": "active",
        "replayed": True,
        "skill_name": name,
        "path": str(destination),
        "target": target,
        "compatibility_status": "verified",
        "adapter_path": str(adapter_path),
    }


def _skill_target(value: str) -> str:
    target = str(value or "").strip().casefold()
    if target not in {"conversation", "presentation"}:
        raise SkillInstallError("Skill target must be conversation or presentation")
    return target


def delete_project_skill(skills_path: str, skill_name: str) -> dict[str, Any]:
    root = Path(skills_path).resolve()
    name = _safe_name(skill_name)
    target = _safe_child(root, name)
    if not target.exists():
        legacy_name = _safe_legacy_name(skill_name)
        if legacy_name:
            name = legacy_name
            target = _safe_child(root, name)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.is_file():
        target.unlink()
    else:
        raise SkillInstallError("A project Skill with this name was not found")
    return {"status": "deleted", "skill_name": name, "path": str(target)}


def delete_project_skill_source_path(skills_path: str, source_path: str) -> dict[str, Any]:
    root = Path(skills_path).resolve()
    target = _resolve_project_skill_source_path(root, source_path)
    candidate = target
    delete_target = (
        target.parent
        if target.name.casefold() == "skill.md" and target.is_file() and target.parent != root
        else target
    )
    if delete_target.is_dir():
        shutil.rmtree(delete_target)
    elif delete_target.is_file():
        delete_target.unlink()
    else:
        raise SkillInstallError("Skill source path was not found")
    return {"status": "deleted", "skill_name": delete_target.name or candidate.stem, "path": str(delete_target)}


def ensure_project_skill_presentation_adapter(skills_path: str, source_path: str) -> dict[str, Any]:
    """Create a user-editable, allow-listed PPT style contract for one Skill.

    This is an explicit user action.  It never executes a downloaded script and
    never overwrites an existing adapter, so a locally curated presentation
    style remains stable across Skill re-syncs.
    """
    root = Path(skills_path).resolve()
    target = _resolve_project_skill_source_path(root, source_path)
    if not target.is_file() or target.name.casefold() != "skill.md" or target.parent == root:
        raise SkillInstallError("Skill source path must point to a Skill directory containing SKILL.md")
    adapter = target.parent / "presentation_style.json"
    created = False
    if adapter.exists():
        try:
            payload = json.loads(adapter.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillInstallError("existing presentation_style.json is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SkillInstallError("existing presentation_style.json must contain an object")
    else:
        atomic_write_text(
            adapter,
            json.dumps(_default_presentation_style(), ensure_ascii=False, indent=2) + "\n",
        )
        created = True
    return {
        "status": "ready",
        "created": created,
        "style_path": str(adapter),
        "skill_directory": str(target.parent),
    }


def _resolve_project_skill_source_path(root: Path, source_path: str) -> Path:
    raw_path = str(source_path or "").strip().replace("\\", "/")
    if not raw_path:
        raise SkillInstallError("Skill source path is required")
    supplied = Path(raw_path)
    if supplied.is_absolute():
        # Some early records stored the absolute project Skill path.  Resolve
        # it directly, then apply the same containment check below.
        target = supplied.resolve()
    else:
        parts = raw_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise SkillInstallError("Skill source path must stay inside the skills root")
        # Records from older builds may contain either ``skills/foo/...`` or
        # ``runtime/.../skills/foo/...``.  Strip the first skills-root marker;
        # the resulting target is still checked against the resolved root.
        root_marker = next(
            (index for index, part in enumerate(parts) if part.casefold() == root.name.casefold()),
            None,
        )
        if root_marker is not None:
            parts = parts[root_marker + 1 :]
        if not parts:
            raise SkillInstallError("Skill source path must name a Skill inside the skills root")
        candidate = Path(*parts)
        if candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
            raise SkillInstallError("Skill source path must stay inside the skills root")
        target = (root / candidate).resolve()
    if target == root or root not in target.parents:
        raise SkillInstallError("Skill source path must stay inside the skills root")
    return target


def _default_presentation_style() -> dict[str, Any]:
    return {
        "schema_version": "souldrive_presentation_style_v1",
        "name": "Editable local Skill presentation starter",
        "primary": "102A43",
        "accent": "F97316",
        "background": "F7F3EA",
        "text": "243B53",
        "surface": "FFFDF7",
        "cover_variant": "editorial_light",
        "content_variant": "split_cards",
        "font_family": "Arial",
    }


def _ensure_presentation_adapter(skill_directory: Path) -> bool:
    """Create an editable local adapter for skills that publish slide presets.

    The adapter is deliberately declarative.  It gives the existing PPTX
    renderer a small, validated layout contract while leaving the downloaded
    Skill instructions and scripts untouched.
    """
    preset_file = next(
        (path for path in skill_directory.iterdir() if path.is_file() and path.name.casefold() == "style_presets.md"),
        None,
    ) if skill_directory.is_dir() else None
    adapter = skill_directory / "presentation_style.json"
    if preset_file is None or adapter.exists():
        return False
    payload = {
        "schema_version": "souldrive_presentation_style_v1",
        "name": "Local adapter generated from STYLE_PRESETS.md",
        "primary": "102A43",
        "accent": "F97316",
        "background": "F7F3EA",
        "text": "243B53",
        "surface": "FFFDF7",
        "cover_variant": "editorial_light",
        "content_variant": "split_cards",
        "font_family": "Arial",
    }
    try:
        atomic_write_text(adapter, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return False
    return True


def _parse_source(source: str) -> dict[str, str]:
    value = str(source or "").strip()
    if not value:
        raise SkillInstallError("Skill source is required")
    command_match = re.fullmatch(
        r"npx\s+(?:skills\s+add|add-skill)\s+(https://(?:www\.)?github\.com/[^\s]+?)(?:\s+(?:--skill|--path)\s+([A-Za-z0-9._/-]+))?",
        value,
        re.IGNORECASE,
    )
    if command_match:
        spec = _parse_source(command_match.group(1))
        selected_skill = command_match.group(2)
        if selected_skill:
            selected_skill = selected_skill.strip().strip("/")
            spec["path"] = selected_skill
            spec["name"] = Path(selected_skill).name
        return spec
    npm_match = re.fullmatch(r"(?:npm\s+install\s+|npm:\s*)(@?[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?(?:@[A-Za-z0-9._-]+)?)", value)
    if npm_match:
        package = npm_match.group(1)
        return {"kind": "npm", "package": package, "name": package.rsplit("/", 1)[-1].split("@")[-1]}
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in GITHUB_HOSTS:
            raise SkillInstallError("Skill downloads only allow HTTPS GitHub sources")
        host = (parsed.hostname or "").lower()
        parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
        if host == "raw.githubusercontent.com":
            if len(parts) < 4:
                raise SkillInstallError("Raw GitHub source must include owner, repo, ref and file")
            return {"kind": "raw", "owner": parts[0], "repo": parts[1], "ref": parts[2], "path": "/".join(parts[3:]), "name": Path(parts[-1]).stem}
        if host in {"github.com", "www.github.com"}:
            if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
                return {"kind": "repo", "owner": parts[0], "repo": parts[1], "ref": parts[3], "path": "/".join(parts[4:]), "name": parts[-1]}
            if len(parts) >= 2:
                return {"kind": "repo", "owner": parts[0], "repo": parts[1], "ref": "main", "path": "/".join(parts[2:]), "name": parts[-1] if len(parts) > 2 else parts[1]}
        raise SkillInstallError("Unsupported GitHub Skill source URL")

    parts = [part for part in value.split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise SkillInstallError("Skill source must be a GitHub URL or owner/repository/path")
    owner, repo = parts[:2]
    path = "/".join(parts[2:])
    return {"kind": "repo", "owner": owner, "repo": repo, "ref": "main", "path": path, "name": parts[-1] if path else repo}


def _download_skill_files(spec: dict[str, str], opener: Callable[..., Any]) -> dict[str, bytes]:
    if spec["kind"] == "npm":
        package_url = "https://registry.npmjs.org/" + urllib.parse.quote(spec["package"], safe="@/")
        metadata = json.loads(_download_bytes(package_url, opener, max_bytes=MAX_TOTAL_BYTES).decode("utf-8"))
        version = str((metadata.get("dist-tags") or {}).get("latest") or "")
        tarball_url = str(((metadata.get("versions") or {}).get(version) or {}).get("dist", {}).get("tarball") or "")
        if not tarball_url:
            raise SkillInstallError("npm 包没有可下载的 latest 版本")
        return _extract_npm_tarball(_download_bytes(tarball_url, opener, max_bytes=MAX_TOTAL_BYTES))
    if spec["kind"] == "raw":
        url = "https://raw.githubusercontent.com/" + "/".join((spec["owner"], spec["repo"], spec["ref"], spec["path"]))
        relative = _remote_name(spec["path"])
        if Path(relative).name.casefold() != "skill.md":
            raise SkillInstallError("Raw GitHub source must point to SKILL.md")
        return {relative: _download_bytes(url, opener)}

    return _download_github_repository_files(spec, opener)


def _download_github_repository_files(spec: dict[str, str], opener: Callable[..., Any]) -> dict[str, bytes]:
    """Download a GitHub skill with fallbacks that do not require API quota."""
    errors = []
    candidate_specs = [dict(spec)]
    path = str(spec.get("path") or "").strip("/")
    if path.startswith("skills/"):
        candidate_specs.append({**spec, "path": path[7:]})
    elif path:
        candidate_specs.append({**spec, "path": f"skills/{path}"})
    for candidate in candidate_specs:
        try:
            return _download_github_api_files(candidate, opener)
        except SkillInstallError as api_error:
            errors.append(str(api_error))
        # GitHub's unauthenticated Contents API is rate-limited.  A project
        # Skill should still be installable from a public repository when that
        # API is unavailable, so fall back to the official archive and then to
        # the raw SKILL.md path.
        try:
            return _download_github_archive_files(candidate, opener)
        except SkillInstallError as archive_error:
            errors.append(str(archive_error))
            try:
                return _download_github_raw_skill(candidate, opener)
            except SkillInstallError as raw_error:
                errors.append(str(raw_error))
    if path:
        try:
            return _download_github_archive_files({**spec, "path": ""}, opener)
        except SkillInstallError as root_error:
            errors.append(str(root_error))
    raise SkillInstallError("无法安装 GitHub Skill：" + "；".join(dict.fromkeys(errors))[-720:])


def _download_github_api_files(spec: dict[str, str], opener: Callable[..., Any]) -> dict[str, bytes]:
    path = spec.get("path") or ""
    api_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{spec['owner']}/{spec['repo']}/contents/{api_path}?ref={urllib.parse.quote(spec['ref'])}"
    payload = json.loads(_download_bytes(url, opener).decode("utf-8"))
    if isinstance(payload, dict) and payload.get("type") == "file":
        return {_remote_name(payload.get("path") or path): _download_bytes(str(payload.get("download_url") or ""), opener)}
    if not isinstance(payload, list):
        raise SkillInstallError("GitHub Skill source did not return a file or directory")

    files: dict[str, bytes] = {}
    total = 0
    queue = [(str(item.get("path") or ""), str(item.get("url") or "")) for item in payload if item.get("type") in {"file", "dir"}]
    base = path.rstrip("/")
    while queue:
        remote_path, remote_url = queue.pop(0)
        if len(files) >= MAX_FILES:
            raise SkillInstallError(f"Skill package exceeds the {MAX_FILES}-file limit")
        data = json.loads(_download_bytes(remote_url, opener).decode("utf-8"))
        if isinstance(data, list):
            for item in data:
                if item.get("type") in {"file", "dir"}:
                    queue.append((str(item.get("path") or ""), str(item.get("url") or "")))
            continue
        if data.get("type") != "file":
            continue
        download_url = str(data.get("download_url") or "")
        if int(data.get("size") or 0) > MAX_FILE_BYTES and _is_optional_large_asset(remote_path):
            continue
        payload_bytes = _download_bytes(download_url, opener)
        try:
            relative = str(PurePosixPath(remote_path).relative_to(PurePosixPath(base))) if base else str(PurePosixPath(remote_path))
        except ValueError as exc:
            raise SkillInstallError("GitHub Skill response contains an unsafe path") from exc
        files[_safe_remote_relative(relative)] = payload_bytes
        total += len(payload_bytes)
        if total > MAX_TOTAL_BYTES:
            raise SkillInstallError(f"Skill package exceeds the {MAX_TOTAL_BYTES}-byte limit")
    return files


def _download_github_archive_files(spec: dict[str, str], opener: Callable[..., Any]) -> dict[str, bytes]:
    last_error: SkillInstallError | None = None
    for ref in _github_refs(spec.get("ref")):
        url = (
            f"https://codeload.github.com/{spec['owner']}/{spec['repo']}/zip/"
            f"{urllib.parse.quote(ref, safe='')}"
        )
        try:
            archive = _download_bytes(url, opener, max_bytes=MAX_ARCHIVE_BYTES)
            files = _extract_github_archive(archive, spec.get("path") or "")
            if files:
                return files
            last_error = SkillInstallError("GitHub archive does not contain the selected Skill path")
        except SkillInstallError as exc:
            last_error = exc
    raise last_error or SkillInstallError("GitHub Skill archive download failed")


def _extract_github_archive(payload: bytes, selected_path: str) -> dict[str, bytes]:
    """Extract only the requested repository subtree from a codeload ZIP."""
    selected = PurePosixPath(str(selected_path or "").strip("/")) if selected_path else None
    files: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                member_path = PurePosixPath(member.filename)
                # GitHub archives have one generated repository-root directory.
                if len(member_path.parts) < 2:
                    continue
                repository_path = PurePosixPath(*member_path.parts[1:])
                if selected is not None:
                    if repository_path == selected:
                        relative = PurePosixPath(repository_path.name)
                    else:
                        try:
                            relative = repository_path.relative_to(selected)
                        except ValueError:
                            continue
                else:
                    relative = repository_path
                safe_relative = _safe_remote_relative(str(relative))
                if len(files) >= MAX_FILES:
                    raise SkillInstallError(f"Skill package exceeds the {MAX_FILES}-file limit")
                data = archive.read(member)
                if len(data) > MAX_FILE_BYTES and _is_optional_large_asset(str(repository_path)):
                    continue
                if len(data) > MAX_FILE_BYTES:
                    raise SkillInstallError(f"Skill file exceeds the {MAX_FILE_BYTES}-byte limit")
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise SkillInstallError(f"Skill package exceeds the {MAX_TOTAL_BYTES}-byte limit")
                files[safe_relative] = data
    except zipfile.BadZipFile as exc:
        raise SkillInstallError("GitHub Skill archive is not a valid ZIP") from exc
    return files


def _download_github_raw_skill(spec: dict[str, str], opener: Callable[..., Any]) -> dict[str, bytes]:
    path = str(spec.get("path") or "").strip("/")
    if not path:
        raise SkillInstallError("GitHub repository root cannot be resolved without the Contents API")
    candidates = [path] if PurePosixPath(path).name.casefold() == "skill.md" else [f"{path}/SKILL.md"]
    last_error: SkillInstallError | None = None
    for ref in _github_refs(spec.get("ref")):
        for candidate in candidates:
            url = (
                f"https://raw.githubusercontent.com/{spec['owner']}/{spec['repo']}/"
                f"{urllib.parse.quote(ref, safe='')}/{urllib.parse.quote(candidate, safe='/')}"
            )
            try:
                return {"SKILL.md": _download_bytes(url, opener)}
            except SkillInstallError as exc:
                last_error = exc
    raise last_error or SkillInstallError("GitHub raw SKILL.md download failed")


def _github_refs(ref: str | None) -> list[str]:
    values = [str(ref or "main").strip() or "main"]
    if values[0].casefold() == "head":
        values.extend(["main", "master"])
    return list(dict.fromkeys(values))


def _download_bytes(url: str, opener: Callable[..., Any], *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    if not url or urllib.parse.urlparse(url).scheme != "https":
        raise SkillInstallError("Skill download returned an invalid HTTPS URL")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in GITHUB_HOSTS and host not in {"registry.npmjs.org", "npmjs.com", "www.npmjs.com"}:
        raise SkillInstallError("Skill downloads only allow HTTPS GitHub or npm sources")
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "SoulDrive-Skill-Installer/1"})
    try:
        with opener(request, timeout=20) as response:
            payload = response.read(max_bytes + 1)
    except Exception as exc:
        raise SkillInstallError("Skill download failed") from exc
    if len(payload) > max_bytes:
        raise SkillInstallError(f"Skill package exceeds the {max_bytes}-byte limit")
    return payload


def _extract_npm_tarball(payload: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                relative = PurePosixPath(member.name)
                if relative.parts and relative.parts[0] == "package":
                    relative = PurePosixPath(*relative.parts[1:])
                safe = _safe_remote_relative(str(relative))
                if len(files) >= MAX_FILES:
                    raise SkillInstallError(f"Skill package exceeds the {MAX_FILES}-file limit")
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise SkillInstallError(f"Skill file exceeds the {MAX_FILE_BYTES}-byte limit")
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise SkillInstallError(f"Skill package exceeds the {MAX_TOTAL_BYTES}-byte limit")
                files[safe] = data
    except (tarfile.TarError, OSError) as exc:
        raise SkillInstallError("npm 包不是有效的 Skill 压缩包") from exc
    return files


def _find_skill_file(files: dict[str, bytes]) -> str:
    candidates = [path for path in files if Path(path).name.casefold() == "skill.md"]
    if len(candidates) != 1:
        raise SkillInstallError("Skill source must contain exactly one SKILL.md")
    return candidates[0]


def _find_local_skill_file(root: Path) -> Path | None:
    candidates = [path for path in root.rglob("SKILL.md") if path.is_file()]
    return candidates[0] if len(candidates) == 1 else None


def _skill_metadata(content: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.S)
    if not match:
        return {"name": "", "description": ""}
    fields = dict(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):\s*[\"']?(.+?)[\"']?\s*$", match.group(1), re.M))
    return {"name": fields.get("name", "").strip(), "description": fields.get("description", "").strip()}


def _safe_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise SkillInstallError("Skill name contains unsupported characters")
    if SAFE_NAME.fullmatch(name) and name not in {".inbox", "install_manifest.json"}:
        return name
    slug = _skill_slug(name)
    if not slug:
        slug = "skill-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    if not SAFE_NAME.fullmatch(slug) or slug in {".inbox", "install_manifest.json"}:
        raise SkillInstallError("Skill name could not be normalized safely")
    return slug


def _safe_legacy_name(value: str) -> str | None:
    """Accept an old Unicode directory name only when it names one component."""
    name = str(value or "").strip()
    if not name or name in {".", "..", ".inbox", "install_manifest.json"}:
        return None
    if "\x00" in name or "/" in name or "\\" in name:
        return None
    return name


def _skill_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return slug[:64]


def _safe_remote_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillInstallError("Skill package contains an unsafe relative path")
    return str(path)


def _is_optional_large_asset(path: str) -> bool:
    return PurePosixPath(path).suffix.casefold() in {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise SkillInstallError("Skill path escapes the project Skill directory")
    return candidate


def _remote_name(value: str) -> str:
    name = PurePosixPath(str(value or "")).name
    return _safe_remote_relative(name)
