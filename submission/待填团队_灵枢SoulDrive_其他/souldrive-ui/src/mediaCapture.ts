const WAV_HEADER_BYTES = 44;
const PCM_SAMPLE_RATE = 16_000;
export const MAX_DISPLAY_FRAME_EDGE = 1_920;

export interface NormalizedImageRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function hasMediaPerceptionInput(
  audio: Blob | null | undefined,
  image: Blob | null | undefined,
  text: string,
): boolean {
  return Boolean(audio || image || text.trim());
}

export function shouldRunMultimodalDecision({
  audio,
  image,
  selectedRegion,
  perceptionEventCount = 0,
  hasUiAutomation = false,
  hasImageDocumentContext = false,
}: {
  audio?: Blob | null;
  image?: Blob | null;
  selectedRegion?: NormalizedImageRegion | null;
  perceptionEventCount?: number;
  hasUiAutomation?: boolean;
  hasImageDocumentContext?: boolean;
}): boolean {
  return Boolean(
    audio
      || image
      || selectedRegion
      || perceptionEventCount > 0
      || hasUiAutomation
      || hasImageDocumentContext,
  );
}

const UI_AUTOMATION_REFERENCE_HINTS = [
  "焦点",
  "控件",
  "按钮",
  "窗口",
  "界面",
  "屏幕",
  "截图",
  "选区",
  "选中",
  "所选",
  "这段",
  "这个区域",
  "当前控件",
  "当前窗口",
  "当前界面",
  "current control",
  "focused control",
  "selected text",
  "screen",
  "window",
  "button",
] as const;
const UI_AUTOMATION_SELECTION_ACTION_HINTS = [
  "总结",
  "概括",
  "解释",
  "分析",
  "描述",
  "改写",
  "润色",
  "重写",
  "selected",
  "summarize",
  "rewrite",
  "explain",
] as const;

/**
 * A fresh UIA snapshot is ambient context, not a multimodal request by itself.
 * Attach it only when the query explicitly refers to the focused/visible
 * target; otherwise a normal knowledge question must go straight to RAG.
 */
export function shouldUseUiAutomationContext({
  query,
  selectedText = "",
}: {
  query?: string | null;
  selectedText?: string | null;
}): boolean {
  const normalized = String(query || "").trim().toLowerCase();
  if (!normalized) return false;
  const hasReference = UI_AUTOMATION_REFERENCE_HINTS.some((hint) => normalized.includes(hint));
  if (hasReference) return true;
  return Boolean(String(selectedText || "").trim())
    && UI_AUTOMATION_SELECTION_ACTION_HINTS.some((hint) => normalized.includes(hint));
}

export function hasUsableVisualEvidence(
  events: Array<{
    modality?: string;
    text?: string | null;
    bbox?: unknown;
    selected_region?: unknown;
    target_candidates?: unknown[] | null;
  }>,
): boolean {
  const hasRegion = (event: { bbox?: unknown; selected_region?: unknown }) => (
    event.bbox !== undefined && event.bbox !== null || event.selected_region !== undefined && event.selected_region !== null
  );
  return events.some((event) => (
    event.modality === "image" || event.modality === "screen" || event.modality === "ui_selection" || event.modality === "image_region" || event.modality === "ocr_region"
  ) && (
    Boolean(event.text?.trim()) || hasRegion(event) || Boolean(event.target_candidates?.length)
  ));
}

export function canUseSelectedRegionWithoutClarification(
  action: string,
  reasonCodes: string[] | null | undefined,
  selectedRegion: NormalizedImageRegion | null | undefined,
): boolean {
  if (!selectedRegion || action !== "clarify") return false;
  const reasons = new Set(reasonCodes || []);
  return [
    "ambiguous_reference",
    "target_candidate_missing",
    "target_or_scope_missing",
    "target_outside_spatial_region",
    "low_confidence_target",
  ].some((reason) => reasons.has(reason));
}

const AUTOMATIC_SLOW_VISUAL_HINTS = [
  "icon",
  "chart",
  "figure",
  "layout",
  "ambiguous",
  "non_text",
  "图表",
  "趋势",
  "曲线",
  "柱状",
  "折线",
  "图标",
  "布局",
  "颜色",
  "结构",
  "位置关系",
  "变化",
  "异常",
  "可见信息",
  "界面状态",
] as const;

export function shouldPrewarmVisualModel({
  visualMode,
  targetGrounding,
  query,
  workflowVisualMode,
  uiTargetKind,
}: {
  visualMode: "auto" | "fast" | "slow";
  targetGrounding: boolean;
  query?: string | null;
  workflowVisualMode?: "none" | "auto" | "fast" | "slow" | null;
  uiTargetKind?: string | null;
}): boolean {
  if (targetGrounding || visualMode === "slow" || workflowVisualMode === "slow") return true;
  if (visualMode !== "auto") return false;
  const hints = `${String(query || "")} ${String(uiTargetKind || "")}`.toLowerCase();
  return AUTOMATIC_SLOW_VISUAL_HINTS.some((hint) => hints.includes(hint));
}

function writeAscii(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

export function encodeMonoPcmWav(samples: Float32Array, sampleRate = PCM_SAMPLE_RATE): Blob {
  if (!Number.isInteger(sampleRate) || sampleRate <= 0) {
    throw new Error("WAV sample rate must be a positive integer");
  }

  const pcmBytes = samples.length * 2;
  const buffer = new ArrayBuffer(WAV_HEADER_BYTES + pcmBytes);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + pcmBytes, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, pcmBytes, true);

  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(WAV_HEADER_BYTES + index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }

  return new Blob([buffer], { type: "audio/wav" });
}

export function fitFrameWithinMaxEdge(width: number, height: number, maxEdge = MAX_DISPLAY_FRAME_EDGE) {
  if (![width, height, maxEdge].every(Number.isFinite) || width <= 0 || height <= 0 || maxEdge <= 0) {
    throw new Error("frame dimensions must be positive numbers");
  }

  const scale = Math.min(1, maxEdge / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function clampUnit(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function imageRegionFromPointer(
  rect: { left: number; top: number; width: number; height: number },
  start: { x: number; y: number },
  end: { x: number; y: number },
): NormalizedImageRegion | null {
  if (![rect.left, rect.top, rect.width, rect.height, start.x, start.y, end.x, end.y].every(Number.isFinite)) {
    return null;
  }
  if (rect.width <= 0 || rect.height <= 0) return null;

  const startX = clampUnit((start.x - rect.left) / rect.width);
  const startY = clampUnit((start.y - rect.top) / rect.height);
  const endX = clampUnit((end.x - rect.left) / rect.width);
  const endY = clampUnit((end.y - rect.top) / rect.height);
  const x = Math.min(startX, endX);
  const y = Math.min(startY, endY);
  const width = Math.abs(endX - startX);
  const height = Math.abs(endY - startY);
  if (width <= 0 || height <= 0) return null;
  return {
    x: Number(x.toFixed(6)),
    y: Number(y.toFixed(6)),
    width: Number(width.toFixed(6)),
    height: Number(height.toFixed(6)),
  };
}

export async function recordedAudioToWavFile(recording: Blob, filename: string): Promise<File> {
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await recording.arrayBuffer());
    const frameCount = Math.max(1, Math.ceil(decoded.duration * PCM_SAMPLE_RATE));
    const offline = new OfflineAudioContext(1, frameCount, PCM_SAMPLE_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const mono = await offline.startRendering();
    return new File([encodeMonoPcmWav(mono.getChannelData(0))], filename, { type: "audio/wav" });
  } finally {
    await context.close();
  }
}

function waitForVideoFrame(video: HTMLVideoElement): Promise<void> {
  if (video.videoWidth > 0 && video.videoHeight > 0) return Promise.resolve();

  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error("屏幕画面未及时就绪")), 5_000);
    const ready = () => {
      window.clearTimeout(timeout);
      resolve();
    };
    const failed = () => {
      window.clearTimeout(timeout);
      reject(new Error("无法读取授权的屏幕画面"));
    };
    video.addEventListener("loadeddata", ready, { once: true });
    video.addEventListener("error", failed, { once: true });
  });
}

export async function captureDisplayFramePng(filename: string): Promise<File> {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    audio: false,
    video: { frameRate: { ideal: 1, max: 1 } },
  });

  try {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    await video.play();
    await waitForVideoFrame(video);

    const frame = fitFrameWithinMaxEdge(video.videoWidth, video.videoHeight);
    const canvas = document.createElement("canvas");
    canvas.width = frame.width;
    canvas.height = frame.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器不支持屏幕图像编码");
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const png = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("屏幕截图编码失败"))), "image/png");
    });
    return new File([png], filename, { type: "image/png" });
  } finally {
    stream.getTracks().forEach((track) => track.stop());
  }
}
