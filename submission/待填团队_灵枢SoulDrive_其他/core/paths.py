import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def app_root():
    configured = os.environ.get("SOULDRIVE_APP_ROOT")
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def local_data_root():
    return app_root() / "runtime"


def model_search_dirs(workspace_path: str | None = None):
    dirs = []
    configured = os.environ.get("SOULDRIVE_MODEL_DIR")
    if configured:
        dirs.append(Path(configured))
    if workspace_path:
        dirs.append(Path(workspace_path) / "models")
    dirs.append(app_root() / "models")
    dirs.append(PROJECT_ROOT / "models")
    return dirs


def resolve_model_path(model_filename: str, workspace_path: str | None = None):
    for directory in model_search_dirs(workspace_path):
        candidate = directory / model_filename
        if candidate.exists():
            return str(candidate)
    return str(model_search_dirs(workspace_path)[0] / model_filename)
