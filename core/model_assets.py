import os
import shutil
from pathlib import Path

from core.model_runtime import DEFAULT_CHAT_MODEL, DEFAULT_EMBEDDING_MODEL
from core.paths import model_search_dirs
from core.reranker import PREFERRED_RERANKER_MODELS
from core.workspace import SoulDriveWorkspace


REQUIRED_MODEL_ASSETS = (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
)


def sync_workspace_models(workspace: SoulDriveWorkspace) -> list[dict]:
    destination_root = Path(workspace.models_path)
    destination_root.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(workspace.root_path)
    results = []

    for asset_name in REQUIRED_MODEL_ASSETS:
        destination = destination_root / asset_name
        source = _find_model_asset_source(asset_name, workspace_root)
        if source is None:
            results.append({"name": asset_name, "status": "missing_source"})
            continue

        if destination.exists():
            copied_files = _copy_missing_files(source, destination) if source.is_dir() else 0
            results.append({
                "name": asset_name,
                "status": "updated" if copied_files else "already_present",
                "files_copied": copied_files,
            })
            continue

        if source.is_dir():
            shutil.copytree(source, destination)
            copied_files = sum(1 for item in destination.rglob("*") if item.is_file())
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_files = 1
        results.append({"name": asset_name, "status": "copied", "files_copied": copied_files})

    reranker_asset_name = _first_available_asset(PREFERRED_RERANKER_MODELS, workspace_root)
    if reranker_asset_name is not None:
        results.extend(_sync_single_asset(reranker_asset_name, destination_root, workspace_root))

    return results


def _sync_single_asset(asset_name: str, destination_root: Path, workspace_root: Path) -> list[dict]:
    destination = destination_root / asset_name
    source = _find_model_asset_source(asset_name, workspace_root)
    if source is None:
        return [{"name": asset_name, "status": "missing_source"}]

    if destination.exists():
        copied_files = _copy_missing_files(source, destination) if source.is_dir() else 0
        return [{
            "name": asset_name,
            "status": "updated" if copied_files else "already_present",
            "files_copied": copied_files,
        }]

    if source.is_dir():
        shutil.copytree(source, destination)
        copied_files = sum(1 for item in destination.rglob("*") if item.is_file())
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_files = 1
    return [{"name": asset_name, "status": "copied", "files_copied": copied_files}]


def _find_model_asset_source(asset_name: str, workspace_root: Path) -> Path | None:
    workspace_models = workspace_root / "models"
    for directory in model_search_dirs(str(workspace_root)):
        candidate = directory / asset_name
        if not candidate.exists():
            continue
        if _is_relative_to(candidate, workspace_models):
            continue
        return candidate
    return None


def _copy_missing_files(source: Path, destination: Path) -> int:
    if not source.is_dir() or not destination.is_dir():
        return 0

    copied_files = 0
    for source_item in source.rglob("*"):
        relative_path = source_item.relative_to(source)
        destination_item = destination / relative_path
        if source_item.is_dir():
            destination_item.mkdir(parents=True, exist_ok=True)
            continue
        if destination_item.exists():
            continue
        destination_item.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_item, destination_item)
        copied_files += 1
    return copied_files


def _first_available_asset(asset_names: tuple[str, ...], workspace_root: Path) -> str | None:
    configured_dir = os.environ.get("SOULDRIVE_MODEL_DIR")
    if configured_dir:
        configured_root = Path(configured_dir)
        for asset_name in asset_names:
            if (configured_root / asset_name).exists():
                return asset_name
        return None
    for asset_name in asset_names:
        if _find_model_asset_source(asset_name, workspace_root) is not None:
            return asset_name
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
