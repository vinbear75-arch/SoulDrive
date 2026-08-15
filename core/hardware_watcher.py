import psutil
import time
import threading
import json
import os
import subprocess
import sys
import urllib.request

from core.hardware_security import HardwareAuthenticator
from core.license import authorization_from_hardware_and_license
from core.logging_config import get_logger
from core.model_assets import sync_workspace_models
from core.runtime_config import api_base_url
from core.runtime_state import lock_runtime, unlock_runtime, use_workspace_runtime_state
from core.security_context import WORKSPACE_DATA_KEY_ENV, export_workspace_data_key, get_workspace_keys
from core.workspace import SoulDriveWorkspace, is_souldrive_workspace

logger = get_logger(__name__)


def build_indexer_worker_command(drive_path: str, auth_level: str):
    if getattr(sys, "frozen", False):
        return [sys.executable, "indexer", drive_path, auth_level]
    return [sys.executable, "-m", "core.indexer_worker", drive_path, auth_level]


def subprocess_creation_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)

class UDriveWatcher:
    def __init__(self):
        """
        初始化硬件监听哨兵 (Watcher)
        维护当前设备的挂载状态和鉴权级别，充当一个轻量级的有限状态机 (FSM)。
        """
        self.current_drive = None  # 记录当前被接管的驱动器盘符 (如 "X:\\")
        self.auth_level = "NONE"   # 当前系统的授权级别
        self.is_running = False    # 控制监听循环的开关
        self.authenticator = HardwareAuthenticator() # 实例化底层的硬件鉴权器
        self.indexer_process: subprocess.Popen | None = None

    def _notify_runtime_api(self, path: str, payload: dict):
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{api_base_url()}{path}",
                data=data,
                headers=self._runtime_headers(),
                method="POST",
            )
            urllib.request.urlopen(request, timeout=1).read()
        except Exception as e:
            logger.warning("[System] FastAPI 运行态通知失败，已使用本地状态文件兜底: %s", e)

    def _runtime_headers(self):
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("SOULDRIVE_API_TOKEN")
        if token:
            headers["X-SoulDrive-Token"] = token
        return headers

    def get_removable_drives(self):
        """
        获取当前系统中所有被标记为“可移动”的逻辑驱动器路径。

        【跨平台考量】：
        使用 psutil.disk_partitions() 比调用 Windows API 更具移植性。
        在 Windows 下它能识别 U 盘，在 Linux/macOS 下也能识别挂载的外部存储卷。
        """
        drives = []
        for partition in psutil.disk_partitions():
            # 过滤条件：底层设备选项中包含 'removable'
            if 'removable' in partition.opts.lower():
                drive_path = partition.device

                # 【路径标准化】：
                # Windows 环境下，根目录必须带尾部斜杠（例如 "X:\\" 而不是 "X:"），
                # 否则后续的文件系统操作（如 os.walk）可能会抛出路径无法解析的异常。
                if not drive_path.endswith('\\'):
                    drive_path += '\\'

                drives.append(drive_path)
        return drives

    def choose_souldrive(self, drives: list[str]) -> str | None:
        if not drives:
            return None
        for drive in drives:
            if is_souldrive_workspace(drive):
                return drive
        return drives[0]

    def prepare_workspace(self, drive_path: str) -> SoulDriveWorkspace:
        workspace = SoulDriveWorkspace.from_drive(drive_path).ensure()
        synced_models = sync_workspace_models(workspace)
        copied_models = [item for item in synced_models if item["status"] in {"copied", "updated"}]
        missing_models = [item["name"] for item in synced_models if item["status"] == "missing_source"]
        if copied_models:
            logger.info("[Workspace] Synced %s model asset(s) into removable workspace.", len(copied_models))
        if missing_models:
            logger.warning("[Workspace] Missing model source asset(s): %s", ", ".join(missing_models))
        return workspace

    def monitor_loop(self):
        """
        核心监听循环（运行在独立的 Daemon 线程中）。

        【设计模式】：轮询 (Polling) 机制。
        相较于向操作系统注册底层 USB 拔插事件（如 Windows 的 WM_DEVICECHANGE 或 Linux 的 udev），
        每隔2秒轮询一次是最简单、最鲁棒且跨平台兼容性最好的实现方案，性能开销也极小。
        """
        logger.info("[System] 硬件雷达已上线，正在监听任意 USB 存储设备接入...")

        while self.is_running:
            drives = self.get_removable_drives()
            self._reap_indexer_worker()

            # ==========================================================
            # 逻辑分支 1：上升沿触发 (Detect Insertion)
            # 状态机：之前没插盘，现在检测到了盘
            # ==========================================================
            if drives and not self.current_drive:
                # 【防并发限制】：默认接管第一个已标记的 SoulDrive 盘；首次使用时初始化第一个可移动盘。
                self.current_drive = self.choose_souldrive(drives)
                if not self.current_drive:
                    time.sleep(2)
                    continue

                logger.info("==================================================")
                logger.info("[Hardware] 硬件握手成功！物理挂载点: %s", self.current_drive)

                # 步骤 A：触发底层安全鉴权，决定是 PRO 还是 LITE
                # 这里调用的就是之前 HardwareAuthenticator 里的逻辑
                hardware_level, sn = self.authenticator.verify_environment()
                workspace = self.prepare_workspace(self.current_drive)
                use_workspace_runtime_state(workspace.root_path)
                self.auth_level, sn, license_status = authorization_from_hardware_and_license(
                    hardware_level=hardware_level,
                    hardware_sn=sn,
                    workspace_path=workspace.root_path,
                )
                unlock_runtime(self.auth_level, sn, self.current_drive, workspace.root_path)
                self._notify_runtime_api("/runtime/unlock", {
                    "reason": "storage device connected",
                    "auth_level": self.auth_level,
                    "hardware_sn": sn,
                    "active_drive": self.current_drive,
                })
                logger.info("[Security] License status: %s (%s)", license_status.reason, license_status.level)

                logger.info("[System] 正在拉起 MCP Server，当前运行模式: [%s]", self.auth_level)
                logger.info("==================================================")

            # ==========================================================
            # 逻辑分支 2：下降沿触发 (Detect Removal)
            # 状态机：之前有盘在系统中，但现在检测不到了（被物理拔出）
            # ==========================================================
            elif not drives and self.current_drive:
                logger.info("[Hardware] 存储设备已物理断开连接！")
                # 【安全熔断机制】：
                # 当承载机密数据/凭证的物理设备拔出时，立刻进行“清场”。
                # 这在机密计算和企业级安全软件中是极重要的防泄露 (DLP) 措施。
                logger.info("[System] 正在执行防泄漏清场并挂起 API 服务...")
                self._stop_indexer_worker()
                lock_runtime("storage device removed")
                self._notify_runtime_api("/runtime/lock", {
                    "reason": "storage device removed",
                    "auth_level": "NONE",
                    "hardware_sn": None,
                    "active_drive": None,
                })

                # 状态重置
                self.current_drive = None
                self.auth_level = "NONE"

            # 节流控制：休眠 2 秒，防止 CPU 占用率飙升 (Busy Waiting)
            time.sleep(2)

    def start(self):
        """
        启动监听服务。

        【线程隔离】：
        将无限循环放入后台 Daemon（守护）线程中。
        daemon=True 意味着当主程序（如 UI 或 FastAPI 服务）退出时，
        这个线程会被操作系统强制安全回收，不会变成僵尸进程。
        """
        self.is_running = True
        thread = threading.Thread(target=self.monitor_loop, daemon=True)
        thread.start()

    def stop(self):
        self.is_running = False
        self._stop_indexer_worker()

    def _start_indexer_worker(self, drive_path: str, auth_level: str):
        self._stop_indexer_worker()
        command = build_indexer_worker_command(drive_path, auth_level)
        env = os.environ.copy()
        workspace = SoulDriveWorkspace.from_drive(drive_path).ensure()
        workspace_keys = get_workspace_keys(workspace.root_path)
        if workspace_keys is not None:
            env[WORKSPACE_DATA_KEY_ENV] = export_workspace_data_key(workspace_keys)
        try:
            self.indexer_process = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess_creation_flags(),
            )
            logger.info("[Indexer] 已启动独立索引 worker，PID=%s。", self.indexer_process.pid)
        except Exception as e:
            self.indexer_process = None
            logger.warning("[Indexer] 独立索引 worker 启动失败: %s", e)

    def _stop_indexer_worker(self):
        process = self.indexer_process
        self.indexer_process = None
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _reap_indexer_worker(self):
        if self.indexer_process is not None and self.indexer_process.poll() is not None:
            logger.info("[Indexer] 独立索引 worker 已退出，退出码=%s。", self.indexer_process.returncode)
            self.indexer_process = None

# ==========================================
# 独立测试入口
# ==========================================
if __name__ == "__main__":
    watcher = UDriveWatcher()
    watcher.start()

    try:
        # 阻塞主线程，保持程序持续运行
        # 因为守护线程在主线程结束时会自动死亡，所以主线程必须保持存活
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # 优雅退出：捕捉 Ctrl+C
        logger.info("[System] 收到终止信号，服务退出。")
        watcher.is_running = False
        watcher.stop()
