import signal
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import uvicorn

from core.audit_log import append_audit_event_best_effort, default_audit_logger
from core.paths import app_root
from core.runtime_config import api_host, api_port
from core.runtime_state import lock_runtime, mark_software_unlocked, unlock_runtime, use_workspace_runtime_state
from core.security_context import set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import (
    IncorrectPassphraseError,
    KeystoreNotInitializedError,
    initialize_keystore,
    unlock_keystore,
)

_RUNTIME_LOG_STREAM = None


def runtime_log_path():
    return app_root() / "runtime" / "sidecar.log"


def startup_log(message: str):
    log_path = runtime_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def _stream_is_usable(stream):
    if stream is None:
        return False
    return callable(getattr(stream, "isatty", None))


def configure_runtime_streams():
    global _RUNTIME_LOG_STREAM
    if _stream_is_usable(sys.stdout) and _stream_is_usable(sys.stderr):
        return

    log_path = runtime_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _RUNTIME_LOG_STREAM = open(log_path, "a", encoding="utf-8", buffering=1)
    if not _stream_is_usable(sys.stdout):
        sys.stdout = _RUNTIME_LOG_STREAM
    if not _stream_is_usable(sys.stderr):
        sys.stderr = _RUNTIME_LOG_STREAM


def uvicorn_log_config():
    log_path = str(runtime_log_path())
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "default": {
                "class": "logging.FileHandler",
                "formatter": "default",
                "filename": log_path,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }


def parent_pid_from_env():
    raw_pid = os.environ.get("SOULDRIVE_PARENT_PID")
    if not raw_pid:
        return None
    try:
        parent_pid = int(raw_pid)
    except ValueError:
        return None
    if parent_pid <= 0 or parent_pid == os.getpid():
        return None
    return parent_pid


def removable_watch_enabled():
    return os.environ.get("SOULDRIVE_WATCH_REMOVABLE", "1") != "0"


def workspace_mode():
    if os.environ.get("SOULDRIVE_PC_ONLY", "0") == "1":
        return "local"
    configured = os.environ.get("SOULDRIVE_WORKSPACE_MODE")
    if configured:
        normalized = configured.strip().lower()
        if normalized not in {"local", "removable"}:
            raise RuntimeError("SOULDRIVE_WORKSPACE_MODE must be 'local' or 'removable'")
        return normalized
    if os.environ.get("SOULDRIVE_WORKSPACE"):
        return "local"
    return "removable"


def development_workspace_bypass_enabled():
    return os.environ.get("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", "0") == "1"


def initialize_local_workspace():
    workspace = SoulDriveWorkspace.default().ensure()
    use_workspace_runtime_state(workspace.root_path)
    unlock_runtime(
        "LOCAL",
        None,
        None,
        workspace.root_path,
        reason="local PC workspace ready",
    )
    if development_workspace_bypass_enabled():
        passphrase = os.environ.get("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE")
        if not passphrase:
            raise RuntimeError("development workspace bypass requires SOULDRIVE_DEV_WORKSPACE_PASSPHRASE")
        try:
            if not workspace.keystore_path or not Path(workspace.keystore_path).exists():
                initialize_keystore(workspace, passphrase)
            set_workspace_keys(workspace.root_path, unlock_keystore(workspace, passphrase))
        except (KeystoreNotInitializedError, IncorrectPassphraseError) as exc:
            raise RuntimeError("development workspace bypass could not unlock the local keystore") from exc
        mark_software_unlocked("local development workspace auto-unlocked")
        startup_log("local development workspace auto-unlocked")
    return workspace


def parent_process_is_alive(parent_pid: int):
    try:
        import psutil

        process = psutil.Process(parent_pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except Exception as exc:
        startup_log(f"parent monitor probe skipped: {exc}")
        return True


def parent_exit_grace_seconds() -> float:
    raw = os.environ.get("SOULDRIVE_PARENT_EXIT_GRACE_SECONDS", "15")
    try:
        return max(1.0, min(float(raw), 300.0))
    except ValueError:
        return 15.0


def start_parent_exit_monitor(shutdown_event: threading.Event, on_parent_exit):
    parent_pid = parent_pid_from_env()
    if parent_pid is None:
        return None

    def monitor_parent():
        startup_log(f"parent monitor started pid={parent_pid}")
        while not shutdown_event.wait(1.0):
            if not parent_process_is_alive(parent_pid):
                grace = parent_exit_grace_seconds()
                startup_log(f"parent probe failed pid={parent_pid}; waiting {grace:.0f}s grace")
                if shutdown_event.wait(grace):
                    return
                if not parent_process_is_alive(parent_pid):
                    startup_log(f"parent process exited pid={parent_pid}")
                    on_parent_exit()
                    return
                startup_log(f"parent process recovered pid={parent_pid}")

    thread = threading.Thread(target=monitor_parent, daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None):
    os.environ.setdefault("SOULDRIVE_LOG_LEVEL", "INFO")
    configure_runtime_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    startup_log(f"sidecar argv={argv}")
    if argv and argv[0] == "indexer":
        from core.indexer_worker import main as indexer_worker_main

        indexer_shutdown_event = threading.Event()
        start_parent_exit_monitor(indexer_shutdown_event, lambda: os._exit(0))
        exit_code = 1
        try:
            exit_code = indexer_worker_main(argv[1:])
            return exit_code
        except Exception as exc:
            startup_log(f"indexer exception: {exc}")
            startup_log(traceback.format_exc())
            if not getattr(sys, "frozen", False):
                raise
            return exit_code
        finally:
            indexer_shutdown_event.set()
            if getattr(sys, "frozen", False):
                os._exit(exit_code)

    shutdown_event = threading.Event()
    audit_logger = default_audit_logger
    watcher = None
    try:
        host = api_host()
        port = api_port()
        startup_log(f"config host={host} port={port}")
        mode = workspace_mode()
        if mode == "local":
            workspace = initialize_local_workspace()
            startup_log(
                "local PC workspace initialized; development auto-unlock active"
                if development_workspace_bypass_enabled()
                else "local PC workspace initialized; waiting for passphrase unlock"
            )
            append_audit_event_best_effort(audit_logger, "sidecar.started", {
                "host": host,
                "port": port,
                "workspace": "local-pc",
            })
        elif removable_watch_enabled():
            lock_runtime("waiting for removable SoulDrive workspace")
            from core.hardware_watcher import UDriveWatcher

            watcher = UDriveWatcher()
            watcher.start()
            startup_log("removable workspace watcher started")
            append_audit_event_best_effort(audit_logger, "sidecar.started", {
                "host": host,
                "port": port,
                "workspace": "removable-watch",
            })
        else:
            lock_runtime("waiting for removable SoulDrive workspace")
            startup_log("removable workspace watcher disabled; API remains locked without a mounted workspace")
            append_audit_event_best_effort(audit_logger, "sidecar.started", {
                "host": host,
                "port": port,
                "workspace": "locked-no-removable-watch",
            })

        from core import mcp_server

        startup_log("uvicorn config creating")
        config = uvicorn.Config(
            mcp_server.app,
            host=host,
            port=port,
            log_level="info",
            log_config=uvicorn_log_config(),
            access_log=False,
        )
        startup_log("uvicorn server creating")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None

        def request_server_shutdown(reason: str):
            startup_log(f"server shutdown requested: {reason}")
            shutdown_event.set()
            server.should_exit = True

        mcp_server.app.state.shutdown_handler = request_server_shutdown

        def request_shutdown(signum, frame):
            _ = frame
            append_audit_event_best_effort(audit_logger, "sidecar.signal", {"signal": signum})
            request_server_shutdown(f"signal {signum}")

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        start_parent_exit_monitor(
            shutdown_event,
            lambda: request_server_shutdown("parent process exited"),
        )
        startup_log("uvicorn starting")
        server.run()
    except Exception as exc:
        startup_log(f"sidecar exception: {exc}")
        startup_log(traceback.format_exc())
        raise
    finally:
        shutdown_event.set()
        if watcher is not None:
            watcher.stop()
        startup_log("sidecar stopped")
        append_audit_event_best_effort(audit_logger, "sidecar.stopped", {})


if __name__ == "__main__":
    main()
