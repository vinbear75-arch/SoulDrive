import subprocess
import json
import re
from core.logging_config import get_logger

logger = get_logger(__name__)

class HardwareAuthenticator:
    def __init__(self, target_vid_pid="152D:0583"):
        """
        初始化硬件鉴权器
        :param target_vid_pid: 目标硬件的 Vendor ID (VID) 和 Product ID (PID)。
                               这是 USB 设备的身份证，比单纯的名称匹配更难伪造。
                               默认值 "152D:0583" 通常对应特定的 USB 桥接芯片或特定型号的 U 盘。
        """
        self.target_vid_pid = target_vid_pid

    def get_physical_usb_devices(self):
        """
        核心方法：使用 PowerShell WMI 接口安全提取物理硬件指纹

        【工程考量】：
        1. 为什么不用 Python 原生库？原生库（如 psutil）通常只能读到逻辑分区（C盘、D盘），
           很难稳定穿透系统读取 USB 物理控制器的底层序列号。调用 WMI 是 Windows 下最可靠的方案。
        2. 防闪烁处理：底层调用 PowerShell 容易弹出黑框，这里做了隐藏窗口处理。
        """
        authorized_drives = []

        # 构造 PowerShell 命令：
        # 1. 查 Win32_DiskDrive 获取物理磁盘
        # 2. 过滤只保留 USB 接口的设备（排除内置 NVMe/SATA 硬盘）
        # 3. 提取型号、序列号、即插即用设备ID（包含 VID/PID 信息）
        # 4. 压缩输出为 JSON 格式，方便 Python 解析
        ps_command = (
            "Get-WmiObject Win32_DiskDrive | "
            "Where-Object {$_.InterfaceType -eq 'USB'} | "
            "Select-Object Model, SerialNumber, PNPDeviceID | "
            "ConvertTo-Json -Compress"
        )

        try:
            # 【UX 优化】：配置 STARTUPINFO 以隐藏子进程窗口
            # 防止在打包成 GUI 桌面软件运行时，频繁闪烁黑色的 CMD 控制台窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # 执行命令并捕获输出
            result = subprocess.run(
                ["powershell", "-Command", ps_command],
                capture_output=True,
                text=True,
                startupinfo=startupinfo
            )

            # 清理输出空白符
            output = result.stdout.strip()
            if not output:
                return authorized_drives # 如果没有查到 USB 设备，安全返回空列表

            # 解析 PowerShell 返回的 JSON
            data = json.loads(output)

            # 【防御性编程】：PowerShell 的 ConvertTo-Json 有个坑：
            # 如果只有一个结果，它返回字典 Dict；如果有多个结果，返回列表 List。
            # 这里强制将字典转为列表，统一后续的遍历逻辑。
            if isinstance(data, dict):
                data = [data]

            for drive in data:
                # 提取原始序列号
                raw_sn = drive.get("SerialNumber", "")

                # 【数据清洗】：硬件厂商写入的 SN 极其不规范，常包含不可见字符、空格或特殊符号（如 \x00）
                # 这里使用正则白名单，只保留字母、数字、下划线和连字符，防止后续作为解密 Key 时出现编码崩溃
                clean_sn = re.sub(r'[^a-zA-Z0-9_-]', '', raw_sn) if raw_sn else ""

                device_info = {
                    "Model": drive.get("Model", "Unknown"),
                    "SerialNumber": clean_sn,
                    "PNPDeviceID": drive.get("PNPDeviceID", "")
                }
                authorized_drives.append(device_info)

        except Exception as e:
            # 异常兜底：即使 WMI 服务损坏或权限不足，也不能让主程序崩溃，只做降级处理
            logger.warning("[Security] 底层硬件扫描异常: %s", e)

        return authorized_drives

    def verify_environment(self):
        """
        【商业级动态鉴权引擎 (Hardware-as-a-License)】
        根据插入的物理硬件，返回对应的授权级别：PRO (闪迪尊享版) / LITE (普通体验版) / NONE
        """
        devices = self.get_physical_usb_devices()

        # 拔出状态：无任何 USB 存储设备
        if not devices:
            return "NONE", None

        # ==========================================
        # 1. 满血验证逻辑 (PRO 级别)
        # ==========================================
        # 优先扫描是否存在指定硬件（此处为 SanDisk 闪迪）。一旦命中，激活最高权限。
        for dev in devices:
            sn = dev['SerialNumber']
            model = dev['Model']

            # 鉴权条件 (双重校验)：
            # a. 型号包含 SanDisk，或者底层硬件 ID 匹配目标 VID/PID (防止改名伪装)
            # b. 序列号存在且长度合理 (防止某些劣质主控返回空 SN)
            if ("SanDisk" in model or self.target_vid_pid in dev['PNPDeviceID']) and (sn and len(sn) >= 5):
                logger.info("[Security] 检测到闪迪 (SanDisk) 官方物理主控。")
                logger.info("[Security] PRO 尊享版协议已解封，核心加密与 GraphRAG 权限已激活。")
                # 返回 PRO 级别，并将清洗后的 SN 作为设备指纹（可用于后续解密本地数据库）
                return "PRO", sn

        # ==========================================
        # 2. 降级体验逻辑 (LITE 级别)
        # ==========================================
        # 如果没有合规的闪迪设备，但插了普通 U 盘，则发放降级版 License
        fallback_sn = devices[0]['SerialNumber']
        fallback_model = devices[0]['Model']

        # 虚拟化 SN：很多便宜的杂牌 U 盘没有烧录独立的 SN，为了防止代码后续需要 SN 运算时报错，
        # 如果长度不足，强制分配一个统一的虚拟体验 SN。
        safe_sn = fallback_sn if (fallback_sn and len(fallback_sn) >= 5) else "LITE_GENERIC_TRIAL_MODE"

        logger.info("[Security] 检测到第三方存储设备 (%s)。", fallback_model)
        logger.info("[Security] 已降级为 LITE 基础体验版，高级 AI 推理与图谱生成功能受限。")
        logger.info("[Security] 提示：购买闪迪 (SanDisk) 联名存储以解锁 SoulDrive 完整形态。")

        return "LITE", safe_sn

# ==========================================
# 独立测试入口
# ==========================================
if __name__ == "__main__":
    authenticator = HardwareAuthenticator()
    auth_level, sn = authenticator.verify_environment()
    logger.info("当前系统授权级别: [%s] | 绑定硬件指纹: %s", auth_level, sn)
