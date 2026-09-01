import os
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_DIR = "SoulDrive"
WORKSPACE_MANIFEST = "workspace.json"
WORKSPACE_VERSION = 1


@dataclass(frozen=True)
class SoulDriveWorkspace:
    root_path: str

    @classmethod
    def default(cls):
        configured = os.environ.get("SOULDRIVE_WORKSPACE")
        if not configured:
            raise RuntimeError("SOULDRIVE_WORKSPACE must be set for an explicit local workspace")
        return cls(configured)

    @classmethod
    def from_drive(cls, drive_path: str):
        return cls(str(Path(drive_path) / WORKSPACE_DIR))

    @property
    def papers_path(self):
        return str(Path(self.root_path) / "data" / "papers")

    @property
    def documents_path(self):
        return str(Path(self.root_path) / "data" / "documents")

    @property
    def document_manifest_path(self):
        return str(Path(self.documents_path) / "manifest.sqlite")

    @property
    def learning_store_path(self):
        return str(Path(self.root_path) / "data" / "learning.sqlite")

    @property
    def project_catalog_path(self):
        return str(Path(self.root_path) / "data" / "project_catalog.sqlite")

    @property
    def task_journal_path(self):
        return str(Path(self.root_path) / "runtime" / "task_journal.sqlite")

    @property
    def history_path(self):
        """本机完整会话、摘要、长期记忆和 outbox 的专用目录。"""
        return str(Path(self.root_path) / "runtime" / "history")

    @property
    def conversation_store_path(self):
        return str(Path(self.history_path) / "conversations.sqlite")

    @property
    def conversation_archive_path(self):
        return str(Path(self.history_path) / "conversations")

    @property
    def memory_archive_path(self):
        return str(Path(self.history_path) / "memories")

    @property
    def outbox_path(self):
        return str(Path(self.history_path) / "outbox.jsonl.enc")

    @property
    def presentations_path(self):
        return str(Path(self.root_path) / "data" / "presentations")

    @property
    def calendars_path(self):
        return str(Path(self.root_path) / "data" / "calendars")

    @property
    def skills_path(self):
        return str(Path(self.root_path) / "skills")

    @property
    def index_path(self):
        return str(Path(self.root_path) / "index")

    @property
    def chroma_path(self):
        return str(Path(self.index_path) / "chroma")

    @property
    def graph_db_path(self):
        return str(Path(self.index_path) / "knowledge_graph.sqlite")

    @property
    def secure_graph_store_path(self):
        return str(Path(self.index_path) / "secure_graph.sqlite")

    @property
    def parent_doc_path(self):
        return str(Path(self.index_path) / "parent_docs.sqlite")

    @property
    def keyword_index_path(self):
        return str(Path(self.index_path) / "keyword_index.sqlite")

    @property
    def secure_vector_store_path(self):
        return str(Path(self.index_path) / "secure_vectors.sqlite")

    @property
    def audit_log_path(self):
        return str(Path(self.root_path) / "audit" / "audit_log.jsonl")

    @property
    def audit_state_path(self):
        return str(Path(self.root_path) / "audit" / "audit_state.json")

    @property
    def models_path(self):
        return str(Path(self.root_path) / "models")

    @property
    def config_path(self):
        return str(Path(self.root_path) / "config")

    @property
    def runtime_path(self):
        return str(Path(self.root_path) / "runtime")

    @property
    def manifest_path(self):
        return str(Path(self.config_path) / WORKSPACE_MANIFEST)

    @property
    def keystore_path(self):
        return str(Path(self.config_path) / "keystore.json")

    def ensure(self):
        for path in (
            self.papers_path,
            self.documents_path,
            self.presentations_path,
            self.calendars_path,
            self.skills_path,
            self.index_path,
            self.chroma_path,
            str(Path(self.root_path) / "audit"),
            self.config_path,
            self.runtime_path,
            self.history_path,
            self.models_path,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_history()
        self._ensure_manifest()
        return self

    def _migrate_legacy_history(self):
        """保留旧 runtime 布局，首次启动时复制到专用 history 目录。"""
        legacy_root = Path(self.root_path) / "runtime"
        history_root = Path(self.history_path)
        legacy_db = legacy_root / "conversations.sqlite"
        new_db = history_root / "conversations.sqlite"
        if legacy_db.is_file() and not new_db.exists():
            shutil.copy2(legacy_db, new_db)
        for name in ("conversations", "memories"):
            source = legacy_root / name
            target = history_root / name
            if source.is_dir() and not target.exists():
                shutil.copytree(source, target)
        legacy_outbox = legacy_root / "outbox.jsonl.enc"
        new_outbox = history_root / "outbox.jsonl.enc"
        if legacy_outbox.is_file() and not new_outbox.exists():
            shutil.copy2(legacy_outbox, new_outbox)

    def _ensure_manifest(self):
        manifest = Path(self.manifest_path)
        if manifest.exists():
            return
        payload = {
            "product": "SoulDrive",
            "workspace_version": WORKSPACE_VERSION,
            "layout": {
                "papers": "data/papers",
                "documents": "data/documents",
                "presentations": "data/presentations",
                "calendars": "data/calendars",
                "skills": "skills",
                "index": "index",
                "audit": "audit",
                "models": "models",
                "runtime": "runtime",
                "history": "runtime/history",
            },
        }
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def diagnose(self):
        root = Path(self.root_path)
        checks = {
            "root": root.exists(),
            "papers": Path(self.papers_path).exists(),
            "documents": Path(self.documents_path).exists(),
            "index": Path(self.index_path).exists(),
            "audit": Path(self.audit_log_path).parent.exists(),
            "models": Path(self.models_path).exists(),
            "manifest": Path(self.manifest_path).exists(),
        }
        disk = self.disk_diagnostics()
        return {
            "ready": all(checks.values()),
            "root_name": root.name,
            "workspace_version": WORKSPACE_VERSION,
            "checks": checks,
            "disk": disk,
        }

    def disk_diagnostics(self, minimum_free_bytes: int = 1024 * 1024 * 1024):
        root = Path(self.root_path)
        probe = root if root.exists() else root.parent
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except Exception:
            return {
                "ready": False,
                "minimum_free_bytes": minimum_free_bytes,
                "error": "disk usage unavailable",
            }
        return {
            "ready": usage.free >= minimum_free_bytes,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "minimum_free_bytes": minimum_free_bytes,
        }


def is_souldrive_workspace(drive_path: str):
    manifest = Path(drive_path) / WORKSPACE_DIR / "config" / WORKSPACE_MANIFEST
    return manifest.exists()


def resolve_workspace(active_drive: str | None = None):
    if active_drive:
        return SoulDriveWorkspace.from_drive(active_drive).ensure()
    if os.environ.get("SOULDRIVE_WORKSPACE"):
        return SoulDriveWorkspace.default().ensure()
    raise RuntimeError("removable SoulDrive workspace is not mounted")
