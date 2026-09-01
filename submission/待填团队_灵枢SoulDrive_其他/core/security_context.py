import base64

from core.workspace_crypto import WorkspaceKeys, derive_workspace_keys


WORKSPACE_DATA_KEY_ENV = "SOULDRIVE_WORKSPACE_DATA_KEY_B64"


_UNLOCKED_WORKSPACE_KEYS: dict[str, WorkspaceKeys] = {}


def set_workspace_keys(workspace_path: str, keys: WorkspaceKeys) -> None:
    _UNLOCKED_WORKSPACE_KEYS[workspace_path] = keys


def get_workspace_keys(workspace_path: str) -> WorkspaceKeys | None:
    return _UNLOCKED_WORKSPACE_KEYS.get(workspace_path)


def clear_workspace_keys(workspace_path: str | None = None) -> None:
    if workspace_path is None:
        _UNLOCKED_WORKSPACE_KEYS.clear()
        return
    _UNLOCKED_WORKSPACE_KEYS.pop(workspace_path, None)


def export_workspace_data_key(keys: WorkspaceKeys) -> str:
    return base64.b64encode(keys.workspace_data_key).decode("ascii")


def restore_workspace_keys(workspace_path: str, encoded_data_key: str | None) -> bool:
    if not encoded_data_key:
        return False
    try:
        workspace_data_key = base64.b64decode(encoded_data_key.encode("ascii"))
    except Exception:
        return False
    set_workspace_keys(workspace_path, derive_workspace_keys(workspace_data_key))
    return True
