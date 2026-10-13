"""Read-only workspace backup preflight and safe SQLite snapshot primitive.

The preflight intentionally stops short of copying a user's workspace.  It
describes the write gate, authoritative data, rebuildable indexes, and key
dependencies so that a later operator-approved backup or restore can be
rehearsed without guessing what must be preserved.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


BACKUP_PREFLIGHT_SCHEMA = "souldrive_workspace_backup_preflight_v0.1"
_ACTIVE_INDEX_STATUSES = frozenset({"queued", "scanning", "indexing"})
_ACTIVE_TASK_STATUSES = frozenset({"pending", "running", "interrupted"})


# A directory entry is intentionally reported as an aggregate.  The preflight
# never emits document names or file contents, which keeps the diagnostic safe
# to show in the local UI and audit output.
_SCOPE_DEFINITIONS = (
    ("config", "authoritative", False, True),
    ("config/keystore.json", "key_dependency", True, True),
    ("data/papers", "authoritative", True, True),
    ("data/documents", "authoritative", True, True),
    ("data/learning.sqlite", "authoritative", False, True),
    ("data/project_catalog.sqlite", "authoritative", False, False),
    ("data/presentations", "authoritative", False, False),
    ("data/calendars", "authoritative", False, False),
    ("skills", "authoritative", False, False),
    ("audit", "authoritative", False, False),
    ("runtime/history", "authoritative", False, True),
    ("runtime/task_journal.sqlite", "authoritative", False, True),
    ("runtime/index_run_journal.sqlite", "authoritative", False, False),
    ("runtime/runtime_state.json", "authoritative", False, False),
    ("index", "derived_rebuildable", False, False),
    ("models", "optional_cache", False, False),
)


def build_backup_preflight(
    workspace,
    *,
    runtime_state: dict[str, Any] | None = None,
    worker_active: bool | None = None,
) -> dict[str, Any]:
    """Describe whether a workspace is ready for an operator-approved snapshot.

    This function is read-only.  It does not stop workers, mutate runtime
    state, open the keystore, or create a backup destination.
    """

    root = Path(workspace.root_path).resolve()
    state = runtime_state if isinstance(runtime_state, dict) else None
    blockers: list[dict[str, str]] = []

    if state is None:
        blockers.append({
            "code": "runtime_state_unverified",
            "message": "runtime state was not supplied; indexing quiescence cannot be verified",
        })
        indexing_status = "unknown"
    else:
        indexing = state.get("indexing") or {}
        indexing_status = str(indexing.get("status") or "unknown")
        if indexing_status in _ACTIVE_INDEX_STATUSES:
            blockers.append({
                "code": "indexing_active",
                "message": f"indexing status is {indexing_status}",
            })
        elif indexing_status == "unknown":
            blockers.append({
                "code": "runtime_state_unverified",
                "message": "indexing status is unavailable",
            })

    if worker_active is True:
        blockers.append({
            "code": "indexer_worker_active",
            "message": "indexer worker process is still alive",
        })

    task_journal = _inspect_task_journal(Path(workspace.task_journal_path))
    if task_journal["status"] == "unreadable":
        blockers.append({
            "code": "task_journal_unverified",
            "message": "task journal exists but cannot be read safely",
        })
    elif task_journal["active_count"]:
        blockers.append({
            "code": "durable_tasks_active",
            "message": "durable task records are pending, running, or interrupted",
        })

    scope = [_inspect_scope_entry(root, definition) for definition in _SCOPE_DEFINITIONS]
    required_missing = [
        item["path"]
        for item in scope
        if item["required"] and item["status"] != "present"
    ]
    for path in required_missing:
        blockers.append({
            "code": "required_scope_missing",
            "message": f"required backup scope is not present: {path}",
        })

    return {
        "schema_version": BACKUP_PREFLIGHT_SCHEMA,
        "read_only": True,
        "ready": not blockers,
        "decision": "allow" if not blockers else "block",
        "indexing_status": indexing_status,
        "indexer_worker_status": (
            "active" if worker_active is True
            else "inactive" if worker_active is False
            else "unverified"
        ),
        "blockers": blockers,
        "task_journal": task_journal,
        "scope": scope,
        "key_dependencies": ["config/keystore.json"],
        "stop_write_order": [
            {
                "ordinal": 1,
                "action": "停止索引器和任务 worker",
                "reason": "避免源文件、任务状态和派生索引在快照期间继续变更",
            },
            {
                "ordinal": 2,
                "action": "等待运行步骤进入终态并确认没有可恢复任务",
                "reason": "pending、running、interrupted 记录都可能在快照后继续写入",
            },
            {
                "ordinal": 3,
                "action": "重新读取 runtime state、任务日志和本预检清单",
                "reason": "快照前的停写门必须在同一时刻重新核对",
            },
            {
                "ordinal": 4,
                "action": "先保存权威资料和 SQLite 一致快照，再保存可重建索引",
                "reason": "索引不能替代原始资料、目录、会话、记忆和密钥依赖",
            },
        ],
        "restore_order": [
            {
                "ordinal": 1,
                "action": "在隔离测试工作区恢复 config 和 keystore",
                "reason": "先验证解锁依赖，禁止把测试密钥写回用户工作区",
            },
            {
                "ordinal": 2,
                "action": "恢复 authoritative 范围并逐个执行 SQLite integrity_check",
                "reason": "源资料、目录、任务、会话和产物是恢复依据",
            },
            {
                "ordinal": 3,
                "action": "核对项目、来源版本和任务身份后再启动服务",
                "reason": "避免把另一个项目或旧版本资料当成当前范围",
            },
            {
                "ordinal": 4,
                "action": "最后重建 derived_rebuildable 索引并重新做来源新鲜度预览",
                "reason": "派生索引可重建，恢复时不能把陈旧索引当作权威数据",
            },
        ],
    }


def backup_sqlite_snapshot(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Copy one SQLite database through SQLite's consistent backup API.

    The destination must be new.  Refusing overwrite is deliberate: restore
    rehearsal must not silently replace an existing file.  This primitive does
    not select a workspace, stop writers, or restore files by itself.
    """

    source = Path(source_path).resolve()
    destination = Path(destination_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    if source == destination:
        raise ValueError("SQLite snapshot destination must differ from source")
    if destination.exists():
        raise FileExistsError(f"SQLite snapshot destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = None
    destination_connection = None
    try:
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro",
            uri=True,
            timeout=timeout,
        )
        destination_connection = sqlite3.connect(str(destination), timeout=timeout)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        integrity_value = str(integrity[0] if integrity else "").strip().lower()
        if integrity_value != "ok":
            raise sqlite3.DatabaseError("destination SQLite integrity check failed")
        return {
            "source": str(source),
            "destination": str(destination),
            "integrity_check": integrity_value,
        }
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _inspect_scope_entry(root: Path, definition: tuple[str, str, bool, bool]) -> dict[str, Any]:
    relative_path, role, required, sensitive = definition
    target = root / relative_path
    item: dict[str, Any] = {
        "path": relative_path,
        "role": role,
        "required": required,
        "sensitive": sensitive,
        "status": "missing",
        "file_count": 0,
        "total_bytes": 0,
        "latest_mtime": None,
        "symlink_count": 0,
    }
    try:
        if target.is_symlink():
            item["status"] = "unsafe_symlink"
            item["symlink_count"] = 1
            return item
        if not target.exists():
            return item
        if target.is_file():
            stat = target.stat()
            item.update(
                status="present",
                file_count=1,
                total_bytes=int(stat.st_size),
                latest_mtime=float(stat.st_mtime),
            )
            return item

        latest_mtime = None
        for current_root, directories, files in os.walk(target, followlinks=False):
            safe_directories = []
            for name in directories:
                child = Path(current_root) / name
                if child.is_symlink():
                    item["symlink_count"] += 1
                else:
                    safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                child = Path(current_root) / name
                if child.is_symlink():
                    item["symlink_count"] += 1
                    continue
                stat = child.stat()
                item["file_count"] += 1
                item["total_bytes"] += int(stat.st_size)
                latest_mtime = max(latest_mtime or 0.0, float(stat.st_mtime))
        item["latest_mtime"] = latest_mtime
        item["status"] = "unsafe_symlink" if item["symlink_count"] else "present"
        return item
    except OSError:
        item["status"] = "unreadable"
        return item


def _inspect_task_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "absent",
            "active_count": 0,
            "status_counts": {},
        }

    connection = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM runtime_tasks GROUP BY status"
        ).fetchall()
        status_counts = {str(status): int(count) for status, count in rows}
        active_count = sum(status_counts.get(status, 0) for status in _ACTIVE_TASK_STATUSES)
        return {
            "status": "readable",
            "active_count": active_count,
            "status_counts": status_counts,
        }
    except (OSError, sqlite3.Error):
        return {
            "status": "unreadable",
            "active_count": 0,
            "status_counts": {},
        }
    finally:
        if connection is not None:
            connection.close()
