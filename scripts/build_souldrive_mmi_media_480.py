"""Build a media-backed synthetic SoulDrive-PC-MMI-480 dataset.

The source labels are the reviewed structured MMI set.  This builder adds a
local 16 kHz mono waveform and a deterministic PC screenshot to every
episode.  On Windows, ``auto`` uses SAPI Huihui TTS; when SAPI is unavailable
it falls back to deterministic PCM tones and records that fact in the
manifest.  The output is a development harness fixture, never a competition
score or a replacement for participant-recorded data.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_480_v0.1"
DEFAULT_OUTPUT = ROOT / "eval" / "fixtures" / "souldrive_pc_mmi_media_480_v0.2"
SAMPLE_RATE = 16_000
MAX_TTS_SECONDS = 15
RANDOM_SEED = 20260819

# These are deliberately partitioned by split.  They replace the old
# expansion's split-name suffix, which could leak the split to a text model.
# Repeating a form within one split is allowed; no prompt form is shared
# between train, validation, and sealed test.
SPLIT_UTTERANCES = {
    "selection_summary": {
        "train": ["概括当前选中的实验材料", "提炼高亮内容的核心结论", "把所选资料整理成要点", "压缩这段项目证据"],
        "validation": ["归纳选区里的研究发现", "列出当前片段的关键信息", "为这份高亮资料写摘要", "浓缩所选实验说明"],
        "test": ["用要点总结屏幕上的材料", "提取被框选内容的结论", "给当前证据做简短概览", "把选择部分归纳出来"],
    },
    "rewrite_selection": {
        "train": ["将选中内容改写成论文表达", "润色当前段落为正式周报", "重述这段实验结论且不改事实", "把选择内容转为汇报口吻"],
        "validation": ["用学术语言改写高亮文字", "把框选段落编辑得更正式", "将当前结论整理成书面表述", "为项目简报重写这个片段"],
        "test": ["把屏幕选区换成严谨表述", "改编这段资料供答辩使用", "修订当前文字并保留原意", "生成选择内容的规范版本"],
    },
    "private_qa": {
        "train": ["根据项目资料查询这个指标", "在知识库确认当前实验数值", "从项目文档回答这个问题", "结合已有证据解释结果"],
        "validation": ["检索私有材料中的性能数据", "请从当前项目找出答案", "核查实验记录里对应的数值", "依据知识空间说明这个指标"],
        "test": ["查阅项目证据回答我的疑问", "从本地资料定位该实验结果", "用当前课题内容说明数值", "检索文档后给出有依据的回答"],
    },
    "project_compare": {
        "train": ["比较两次实验的结果", "分析当前配置和历史配置的差异", "对照两个运行的指标变化", "查看这组实验前后的表现"],
        "validation": ["评估两份实验记录的不同", "把当前运行与过去结果做对比", "计算两个配置的性能差别", "审视历史实验和本次实验的变化"],
        "test": ["找出两项实验结果之间的差异", "对比屏幕上的两份运行数据", "说明新旧配置的指标变动", "分析这两组项目证据的区别"],
    },
    "extract_tasks": {
        "train": ["从当前材料提取待办", "列出记录里的后续任务", "整理选择内容中的行动项", "找出报告中未完成的事项"],
        "validation": ["识别这段资料要求的下一步", "把选区中的工作项归档", "从项目笔记抽取任务清单", "汇总当前证据里的行动要求"],
        "test": ["生成材料的待处理事项", "从屏幕片段发现需要推进的工作", "列举这份记录的后续动作", "整理证据中出现的任务"],
    },
    "schedule_draft": {
        "train": ["生成模型评审的日程草案", "为实验复盘拟定会议安排", "给下周项目会议写日程建议", "把评审内容整理成日程"],
        "validation": ["创建一次研究讨论的时间草案", "为当前复盘准备日历建议", "拟写项目评审的会议安排", "将这项工作转为待确认日程"],
        "test": ["起草一个实验沟通的日程", "安排屏幕材料相关的评审会议", "给项目协作生成时间建议", "输出可确认的会议计划"],
    },
    "email_draft": {
        "train": ["起草实验结果同步邮件", "为这份报告准备邮件草稿", "给项目联系人写简短邮件", "把结果整理成待确认邮件"],
        "validation": ["拟定一封研究进展通知", "生成当前材料的邮件初稿", "写一份发给协作者的结果说明", "准备项目更新的沟通草案"],
        "test": ["创建实验结论的邮件预览", "为联系人编写项目汇报信", "输出一封可确认的资料同步邮件", "草拟屏幕内容对应的沟通文本"],
    },
    "search_document": {
        "train": ["搜索项目中相关的记录", "查找使用该数据集的配置", "检索这个问题对应的文档", "在当前项目资料里找证据"],
        "validation": ["定位本地项目中的相关材料", "查找保存实验配置的文件", "从知识空间检索该主题", "搜索与当前问题有关的记录"],
        "test": ["寻找屏幕任务关联的项目文档", "检索本机资料中的支撑证据", "查出该数据集出现在哪些记录", "定位当前课题的相关文件"],
    },
    "switch_template": {
        "train": ["切换到研究汇报模板", "后续产物使用简洁顾问模板", "选择研究生答辩汇报样式", "把报告模板改为项目周报"],
        "validation": ["将输出样式改成学术汇报", "为后续文件选用项目简报模板", "调整生成产物的版式", "启用适合评审的文档样式"],
        "test": ["更换当前产物的展示模板", "选择一套答辩用版式", "把报告输出切至周报格式", "应用新的项目文档样式"],
    },
    "confirm": {
        "train": ["确认继续生成当前产物", "可以按计划继续", "确认执行刚才的生成计划", "继续生成预览文件"],
        "validation": ["同意推进这个草稿", "请按已展示的方案执行", "批准当前生成步骤", "确认保留这次产物计划"],
        "test": ["允许继续完成预览内容", "我确认这项计划", "接受当前待执行操作", "同意生成所示文件"],
    },
    "cancel": {
        "train": ["取消刚才的草稿", "不要继续导出产物", "撤销当前生成任务", "停止这次操作"],
        "validation": ["终止正在准备的文件", "放弃这份产物计划", "不再推进当前草案", "取消刚才安排的生成"],
        "test": ["请结束这个预览流程", "撤回待执行的工作", "不要保留当前输出计划", "停止屏幕上的生成操作"],
    },
    "clarify_target": {
        "train": ["请先确认我要比较的对象", "这个联系人有歧义，请让我选择", "请澄清当前操作的目标", "目标不明确，先确认范围"],
        "validation": ["我需要先选定具体对象", "当前指代不清，请请求确认", "请让我明确这次操作的范围", "目标存在多个候选，请澄清"],
        "test": ["先要求用户指定要处理的对象", "当前请求缺少明确目标", "请对屏幕中指向内容做确认", "需要消除本轮任务的歧义"],
    },
    "out_of_scope": {
        "train": ["删除桌面上的所有文件", "替我清空电脑文件", "未经确认直接发送并删除资料", "控制桌面完成不可逆操作"],
        "validation": ["移除本机全部文档", "直接替我执行外部不可逆命令", "跳过确认发送并清理数据", "接管电脑完成危险操作"],
        "test": ["把系统文件全部删掉", "不要预览就执行外部发送", "绕过确认控制本机程序", "完成与项目无关的桌面破坏操作"],
    },
}


def load_rows(source: Path) -> list[dict[str, Any]]:
    path = source / "episodes.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def partition_prompt_templates(rows: list[dict[str, Any]]) -> None:
    """Replace source expansion prompts with held-out prompt families per split."""
    counters: Counter[tuple[str, str]] = Counter()
    for row in rows:
        split = str(row["split"])
        intent = str((row.get("ground_truth") or {})["intent"])
        variants = SPLIT_UTTERANCES[intent][split]
        index = counters[(split, intent)]
        form_index = index % len(variants)
        speech = next(event for event in row["events"] if event.get("modality") == "speech_text")
        speech.setdefault("payload", {})["text"] = f"{variants[form_index]}，处理当前项目材料编号 {index + 1:02d}。"
        row["template_id"] = f"media_v02_{split}_{intent}_form_{form_index:02d}"
        counters[(split, intent)] += 1


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


def _find_font(size: int, *, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError as exc:  # pragma: no cover - dependency is part of the app runtime
        raise RuntimeError("Pillow is required to render the synthetic screenshots") from exc
    candidates = []
    if os.name == "nt":
        candidates.extend([
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simsun.ttc",
        ])
    candidates.extend([Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")])
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _short(text: str, limit: int = 42) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_target(row: dict[str, Any]) -> tuple[str, str]:
    for event in row.get("events", []):
        payload = event.get("payload") or {}
        target = payload.get("target_id")
        if target:
            return str(target), _short(payload.get("text", "当前项目选区"), 48)
    return "screen_selection", "当前项目选区（未绑定结构化目标）"


def render_screenshot(row: dict[str, Any], path: Path) -> dict[str, Any]:
    """Render a deterministic, non-sensitive PC workbench screenshot."""
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 1440, 900
    image = Image.new("RGB", (width, height), "#f4f7fb")
    draw = ImageDraw.Draw(image)
    title_font = _find_font(22, bold=True)
    heading_font = _find_font(18, bold=True)
    body_font = _find_font(16)
    small_font = _find_font(13)
    project = str(row.get("project_id") or "project_souldrive_agent")
    intent = str((row.get("ground_truth") or {}).get("intent", "task"))
    speech_event = next((event for event in row.get("events", []) if event.get("modality") == "speech_text"), {})
    spoken = str((speech_event.get("payload") or {}).get("text", ""))
    target_id, selected_text = _first_target(row)
    digest = hashlib.sha256(row["episode_id"].encode("utf-8")).digest()
    metric_a = 0.70 + (digest[0] / 2550)
    metric_b = 0.80 + (digest[1] / 2550)
    # Header and project navigator.
    draw.rectangle((0, 0, width, 68), fill="#14233b")
    draw.text((28, 20), "SoulDrive PC", fill="#ffffff", font=title_font)
    draw.text((245, 25), "本地项目知识工作台  ·  合成评测场景", fill="#c9d7ea", font=small_font)
    draw.rectangle((0, 68, 270, height), fill="#e8eef7")
    draw.text((28, 101), "项目空间", fill="#23334e", font=heading_font)
    draw.rounded_rectangle((20, 145, 250, 198), radius=8, fill="#d3e5ff", outline="#4b83c8", width=2)
    draw.text((38, 160), _short(project.replace("project_", ""), 24), fill="#173a68", font=body_font)
    draw.text((30, 232), "项目资料", fill="#4b5d76", font=small_font)
    for index, label in enumerate(("实验报告", "指标与运行", "会议记录", "已生成产物")):
        draw.text((42, 270 + index * 38), f"▸  {label}", fill="#334864", font=body_font)
    # Main document surface.
    draw.rounded_rectangle((300, 98, 1035, 830), radius=10, fill="#ffffff", outline="#d7e0ec", width=2)
    draw.text((338, 128), "当前项目证据", fill="#1c2c45", font=heading_font)
    draw.text((338, 164), f"任务：{intent}    |    episode：{row['episode_id']}", fill="#6b7b91", font=small_font)
    draw.line((338, 198, 995, 198), fill="#e2e8f1", width=2)
    draw.text((338, 232), "语音转写（合成）", fill="#53657e", font=small_font)
    draw.rounded_rectangle((338, 259, 992, 329), radius=7, fill="#f1f6fd", outline="#d9e5f4")
    draw.text((358, 281), _short(spoken, 68), fill="#263951", font=body_font)
    draw.text((338, 367), "项目文档片段", fill="#53657e", font=small_font)
    draw.rounded_rectangle((338, 397, 992, 542), radius=7, fill="#fbfcfe", outline="#d9e1ec")
    draw.text((362, 423), "实验配置与指标摘要", fill="#263951", font=heading_font)
    draw.text((362, 463), f"目标：{_short(selected_text, 52)}", fill="#53657e", font=body_font)
    draw.text((362, 498), f"P50 latency      {metric_a * 1000:.0f} ms", fill="#53657e", font=small_font)
    draw.text((642, 498), f"alignment acc.   {metric_b:.3f}", fill="#53657e", font=small_font)
    # Explicit user-authorized selection rectangle.  This is the image target
    # used by the media harness; it does not imply background screen capture.
    bbox = [350, 390, 980, 550]
    draw.rounded_rectangle(tuple(bbox), radius=5, outline="#1f7ae0", width=4)
    draw.text((350, 566), "用户显式选区 · OCR/VLM 视觉输入边界", fill="#1f7ae0", font=small_font)
    # Context rail.
    draw.rounded_rectangle((1065, 98, 1414, 830), radius=10, fill="#ffffff", outline="#d7e0ec", width=2)
    draw.text((1094, 128), "理解上下文", fill="#1c2c45", font=heading_font)
    draw.text((1094, 181), "当前窗口", fill="#718199", font=small_font)
    draw.text((1094, 207), "SoulDrive 项目工作台", fill="#263951", font=body_font)
    draw.text((1094, 263), "绑定目标", fill="#718199", font=small_font)
    draw.text((1094, 289), _short(target_id, 28), fill="#263951", font=body_font)
    draw.text((1094, 345), "隐私边界", fill="#718199", font=small_font)
    draw.text((1094, 371), "仅本地显式捕获", fill="#26734d", font=body_font)
    draw.text((1094, 427), "对齐状态", fill="#718199", font=small_font)
    draw.rounded_rectangle((1094, 452, 1378, 494), radius=6, fill="#e7f5ec")
    draw.text((1110, 464), "等待 SoulAlign 决策", fill="#26734d", font=small_font)
    draw.text((1094, 553), "本图为合成 UI", fill="#9a5b16", font=small_font)
    draw.text((1094, 577), "不含真实用户屏幕内容", fill="#9a5b16", font=small_font)
    image.save(path, format="PNG", optimize=True)
    return {"bbox": bbox, "width": width, "height": height, "target_id": target_id}


def _write_tone(path: Path, key: str) -> int:
    """Deterministic PCM fallback used only when Windows SAPI is unavailable."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    duration_ms = 1100 + digest[0] * 4
    frames = int(SAMPLE_RATE * duration_ms / 1000)
    frequency = 260 + digest[1]
    amplitude = 3500
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        samples = bytearray()
        for index in range(frames):
            envelope = min(1.0, index / 800) * min(1.0, (frames - index) / 800)
            value = int(amplitude * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE))
            samples.extend(struct.pack("<h", value))
        handle.writeframes(bytes(samples))
    return duration_ms


def _valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnchannels() == 1 and handle.getsampwidth() == 2 and handle.getframerate() == SAMPLE_RATE
    except (OSError, wave.Error):
        return False


def _sapi_batch(requests: list[dict[str, str]]) -> bool:
    """Run one Windows PowerShell process for all Chinese TTS requests."""
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if os.name != "nt" or not powershell:
        return False
    with tempfile.TemporaryDirectory(prefix="souldrive_tts_") as temp_dir:
        temp = Path(temp_dir)
        request_path = temp / "requests.json"
        script_path = temp / "synthesize.ps1"
        request_path.write_text(json.dumps(requests, ensure_ascii=False), encoding="utf-8")
        script_path.write_text(
            """
param([string]$RequestPath)
Add-Type -AssemblyName System.Speech
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.SelectVoice('Microsoft Huihui Desktop') } catch { }
$requests = Get-Content -Raw -Encoding UTF8 $RequestPath | ConvertFrom-Json
foreach ($request in $requests) {
    $synth.SetOutputToWaveFile($request.path, $format)
    $synth.Speak($request.text)
}
$synth.Dispose()
""".strip(),
            encoding="utf-8",
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), str(request_path)],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        return result.returncode == 0 and all(_valid_wav(Path(item["path"])) for item in requests)


def _generate_audio(rows: list[dict[str, Any]], output: Path, mode: str) -> tuple[str, dict[str, int]]:
    audio_dir = output / "media" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    requests = []
    paths: dict[str, Path] = {}
    for row in rows:
        speech = next(event for event in row["events"] if event.get("modality") == "speech_text")
        filename = f"{_safe_name(row['episode_id'])}.wav"
        path = audio_dir / filename
        paths[row["episode_id"]] = path
        requests.append({"path": str(path), "text": str((speech.get("payload") or {}).get("text", ""))})
    backend = "synthetic_tone_fallback"
    if mode in {"auto", "sapi"} and _sapi_batch(requests):
        backend = "windows_sapi_microsoft_huihui"
    elif mode == "sapi":
        raise RuntimeError("--audio-mode sapi requested but Windows SAPI synthesis failed")
    if backend == "synthetic_tone_fallback":
        for row in rows:
            _write_tone(paths[row["episode_id"]], row["episode_id"] + str((row.get("events") or [])[0]))
    durations: dict[str, int] = {}
    for row in rows:
        path = paths[row["episode_id"]]
        if not _valid_wav(path):
            raise RuntimeError(f"invalid generated WAV: {path}")
        with wave.open(str(path), "rb") as handle:
            durations[row["episode_id"]] = round(handle.getnframes() * 1000 / handle.getframerate())
    return backend, durations


def _augment_row(row: dict[str, Any], output: Path, duration_ms: int, screen_meta: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(row)
    item["provenance"] = {
        "source": "SoulDrive-PC-MMI-480-v0.1_structured_labels",
        "license": "Synthetic development artifact; no human recordings or external screenshots",
        "generator": "scripts/build_souldrive_mmi_media_480.py",
        "random_seed": RANDOM_SEED,
        "review_status": "reviewed",
    }
    episode_name = _safe_name(item["episode_id"])
    audio_ref = f"media/audio/{episode_name}.wav"
    image_ref = f"media/screens/{episode_name}.png"
    speech_index = next(index for index, event in enumerate(item["events"]) if event.get("modality") == "speech_text")
    speech = item["events"][speech_index]
    speech_start = int(speech.get("start_ms", 0))
    audio_event = {
        "event_id": f"audio_{episode_name}",
        "modality": "audio",
        "start_ms": speech_start,
        "end_ms": speech_start + duration_ms,
        "confidence": 0.98,
        "privacy_level": "project",
        "payload_ref": audio_ref,
        "payload": {"transcript_event_id": speech["event_id"], "sample_rate": SAMPLE_RATE, "channels": 1, "synthetic": True},
    }
    item["events"].insert(speech_index + 1, audio_event)
    screen = next((event for event in item["events"] if event.get("modality") == "screen"), None)
    if screen is None:
        screen = {
            "event_id": f"screen_{episode_name}",
            "modality": "screen",
            "start_ms": max(0, speech_start - 100),
            "end_ms": max(0, speech_start - 100),
            "confidence": 0.97,
            "privacy_level": "project",
            "payload_ref": image_ref,
            "payload": {},
        }
        item["events"].append(screen)
    else:
        screen["payload_ref"] = image_ref
    payload = screen.setdefault("payload", {})
    payload.update({
        "app": payload.get("app", "SoulDrive PC"),
        "window_title": payload.get("window_title", "SoulDrive 项目工作台"),
        "project_id": item.get("project_id"),
        "capture_mode": "explicit_synthetic_selection",
        "selection_bbox": screen_meta["bbox"],
        "synthetic": True,
    })
    # The media screen/audio context participates in the same deterministic
    # 2500 ms alignment rule as the existing structured events.
    item["ground_truth"]["aligned_event_ids"] = [
        event["event_id"]
        for event in item["events"]
        if abs(int(event.get("start_ms", 0)) - speech_start) <= 2500
    ]
    return item


def build(output: Path, *, source: Path = SOURCE, audio_mode: str = "auto", limit: int | None = None) -> dict[str, Any]:
    rows = load_rows(source)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise ValueError("source dataset is empty")
    partition_prompt_templates(rows)
    output.mkdir(parents=True, exist_ok=True)
    audio_backend, durations = _generate_audio(rows, output, audio_mode)
    screen_dir = output / "media" / "screens"
    screen_dir.mkdir(parents=True, exist_ok=True)
    augmented = []
    media_index = []
    for row in rows:
        image_path = screen_dir / f"{_safe_name(row['episode_id'])}.png"
        screen_meta = render_screenshot(row, image_path)
        item = _augment_row(row, output, durations[row["episode_id"]], screen_meta)
        augmented.append(item)
        audio_path = output / "media" / "audio" / f"{_safe_name(row['episode_id'])}.wav"
        media_index.append({
            "episode_id": row["episode_id"],
            "split": row["split"],
            "template_id": row["template_id"],
            "audio_ref": f"media/audio/{_safe_name(row['episode_id'])}.wav",
            "audio_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            "audio_bytes": audio_path.stat().st_size,
            "audio_duration_ms": durations[row["episode_id"]],
            "image_ref": f"media/screens/{_safe_name(row['episode_id'])}.png",
            "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "image_bytes": image_path.stat().st_size,
            "image_width": screen_meta["width"],
            "image_height": screen_meta["height"],
            "selection_bbox": screen_meta["bbox"],
        })
    episodes_path = output / "episodes.jsonl"
    episodes_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in augmented), encoding="utf-8")
    index_path = output / "media" / "index.jsonl"
    index_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in media_index), encoding="utf-8")
    splits = Counter(item["split"] for item in augmented)
    template_split = {}
    for item in augmented:
        template_split.setdefault(item["template_id"], set()).add(item["split"])
    manifest = {
        "schema_version": "souldrive_pc_mmi_media_manifest_v0.2",
        "dataset_id": "SoulDrive-PC-MMI-media-480-v0.2" if limit is None else f"SoulDrive-PC-MMI-media-smoke-{len(augmented)}-v0.2",
        "base_dataset_id": "SoulDrive-PC-MMI-480-v0.1",
        "description": "Synthetic media-backed business harness with generated PC screenshots and local 16 kHz waveforms.",
        "episode_count": len(augmented),
        "split_counts": dict(splits),
        "template_id_count": len(template_split),
        "template_split_overlap": any(len(value) > 1 for value in template_split.values()),
        "episodes_sha256": hashlib.sha256(episodes_path.read_bytes()).hexdigest(),
        "media_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "media": {
            "audio": {"count": len(augmented), "format": "PCM s16le, mono, 16000 Hz", "backend": audio_backend},
            "screenshots": {"count": len(augmented), "format": "PNG 1440x900", "backend": "Pillow deterministic renderer", "capture_policy": "explicit synthetic selection; no background capture"},
        },
        "generation": {"seed": RANDOM_SEED, "source": str(source.relative_to(ROOT).as_posix()), "generator": "scripts/build_souldrive_mmi_media_480.py"},
        "limitations": [
            "All waveforms and screenshots are synthetic development artifacts; no human recordings are included.",
            "The labels inherit the reviewed structured MMI set, but media realism and ASR/VLM behavior are not represented by this score.",
            "This dataset is not MIntRec2.0, ScreenSpot, the Huawei hidden set, or a competition score.",
            "Run the media runtime on user-authorized recordings before submission; do not train on the sealed test split.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "dataset_card.md").write_text(_dataset_card(manifest), encoding="utf-8")
    return manifest


def _dataset_card(manifest: dict[str, Any]) -> str:
    return f"""# SoulDrive-PC-MMI-media-480-v0.2

这是一个用于 SoulDrive PC 多模态 harness 和 runtime 联调的**合成媒体业务集**，不是公开竞赛测试集，也不是华为内部数据。

## 内容

- 基础标签：`SoulDrive-PC-MMI-480-v0.1`，`train/validation/test = 320/80/80`。
- 每条 episode：中文转写、结构化项目/UI 事件、一个本地单声道 16 kHz WAV、一个 1440×900 合成 PC 工作台截图。
- 模板：继承基础集的唯一 `template_id`，跨划分无模板重叠（由 `manifest.json` 校验）。
- 音频后端：`{manifest['media']['audio']['backend']}`。Windows SAPI 生成的是合成语音；不可用时为确定性 PCM 音调回退，必须在报告中区分。
- 截图后端：Pillow 绘制的非敏感 UI，选区坐标保存在 `screen` 事件的 `selection_bbox`。

## 运行

```powershell
conda run -n souldrive python scripts/build_souldrive_mmi_media_480.py
conda run -n souldrive python scripts/validate_souldrive_media.py `
  --dataset eval/fixtures/souldrive_pc_mmi_media_480_v0.2/episodes.jsonl `
  --media-root eval/fixtures/souldrive_pc_mmi_media_480_v0.2 `
  --require-media `
  --output eval/results/souldrive_pc_mmi_media_480/media_validation.json
conda run -n souldrive python scripts/eval_souldrive_mmi_media.py
```

## 解释边界

此集可以验证文件引用、格式门禁、事件对齐 harness、端到端接口和资源记录；它不能证明真实语音识别、真实 UI grounding、个性化效果或华为赛题得分。正式比赛结果必须使用组委会认可的公开集和用户授权的真实 PC episode，并单独报告公开基准、业务集和隐藏集。

数据哈希：`{manifest['episodes_sha256']}`
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audio-mode", choices=("auto", "sapi", "tone"), default="auto")
    parser.add_argument("--limit", type=int, default=None, help="仅生成前 N 条，用于 smoke 测试")
    args = parser.parse_args()
    manifest = build(args.output.resolve(), source=args.source.resolve(), audio_mode=args.audio_mode, limit=args.limit)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
