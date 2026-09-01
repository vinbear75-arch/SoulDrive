import argparse
import os
from pathlib import Path

from core.indexer import DriveIndexer
from core.runtime_state import use_workspace_runtime_state
from core.security_context import WORKSPACE_DATA_KEY_ENV, restore_workspace_keys
from core.workspace import SoulDriveWorkspace


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="SoulDrive isolated indexing worker")
    parser.add_argument("drive_path", nargs="?")
    parser.add_argument("drive_auth_level", nargs="?")
    parser.add_argument("--workspace-path", dest="workspace_path")
    parser.add_argument("--auth-level", dest="auth_level")
    args = parser.parse_args(argv)

    indexer = DriveIndexer()
    try:
        auth_level = args.auth_level or args.drive_auth_level or "PRO"
        if args.workspace_path:
            # Local mode receives the workspace root directly.
            workspace = SoulDriveWorkspace(str(Path(args.workspace_path).resolve())).ensure()
            use_workspace_runtime_state(workspace.root_path)
            restore_workspace_keys(workspace.root_path, os.environ.get(WORKSPACE_DATA_KEY_ENV))
            indexer.sync_workspace(workspace, auth_level)
        else:
            if not args.drive_path:
                parser.error("drive_path is required unless --workspace-path is provided")
            workspace = SoulDriveWorkspace.from_drive(args.drive_path).ensure()
            use_workspace_runtime_state(workspace.root_path)
            restore_workspace_keys(workspace.root_path, os.environ.get(WORKSPACE_DATA_KEY_ENV))
            indexer.sync_workspace(workspace, auth_level)
    finally:
        indexer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
