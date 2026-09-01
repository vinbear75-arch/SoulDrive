import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { resolve } from "node:path";

import {
  canUseSelectedRegionWithoutClarification,
  encodeMonoPcmWav,
  fitFrameWithinMaxEdge,
  hasMediaPerceptionInput,
  hasUsableVisualEvidence,
  imageRegionFromPointer,
  shouldRunMultimodalDecision,
  shouldUseUiAutomationContext,
  shouldPrewarmVisualModel,
} from "../../build/media-capture-test/mediaCapture.js";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");

test("hasMediaPerceptionInput allows image-only perception but rejects an empty request", () => {
  assert.equal(hasMediaPerceptionInput(null, new Blob(["png"]), ""), true);
  assert.equal(hasMediaPerceptionInput(new Blob(["wav"]), null, ""), true);
  assert.equal(hasMediaPerceptionInput(null, null, "  "), false);
});

test("plain text does not enter the multimodal decision gate", () => {
  assert.equal(shouldRunMultimodalDecision({}), false);
  assert.equal(shouldRunMultimodalDecision({ hasImageDocumentContext: false, perceptionEventCount: 0 }), false);
  assert.equal(shouldRunMultimodalDecision({ image: new Blob(["png"]) }), true);
  assert.equal(shouldRunMultimodalDecision({ selectedRegion: { x: 0, y: 0, width: 0.5, height: 0.5 } }), true);
  assert.equal(shouldRunMultimodalDecision({ perceptionEventCount: 1 }), true);
});

test("ambient UI Automation does not hijack normal RAG questions", () => {
  assert.equal(shouldUseUiAutomationContext({ query: "你是谁?", selectedText: "SoulDrive" }), false);
  assert.equal(shouldUseUiAutomationContext({ query: "RAG 的核心思想是什么？", selectedText: "按钮" }), false);
  assert.equal(shouldUseUiAutomationContext({ query: "请总结这段内容", selectedText: "保存" }), true);
  assert.equal(shouldUseUiAutomationContext({ query: "请总结当前控件里的选中文字", selectedText: "保存" }), true);
  assert.equal(shouldUseUiAutomationContext({ query: "这个按钮有什么作用？" }), true);
  assert.match(app, /const shouldAttachAmbientUiAutomation = shouldUseUiAutomationContext/);
  assert.match(app, /runtimePerceptionIsFresh && \(hasExplicitMediaContext \|\| shouldAttachAmbientUiAutomation\)/);
});

test("hasUsableVisualEvidence accepts structured OCR/VLM regions and rejects non-visual events", () => {
  assert.equal(hasUsableVisualEvidence([{ modality: "ocr_region", text: "保存" }]), true);
  assert.equal(hasUsableVisualEvidence([{ modality: "image_region", bbox: { x: 0, y: 0, width: 1, height: 1 } }]), true);
  assert.equal(hasUsableVisualEvidence([{ modality: "image_region", target_candidates: [{ target_id: "button-save" }] }]), true);
  assert.equal(hasUsableVisualEvidence([{ modality: "ui_automation", text: "保存" }]), false);
  assert.equal(hasUsableVisualEvidence([{ modality: "screen", text: "保存" }]), true);
});

test("an explicit image selection bypasses only target-related clarification", () => {
  const region = { x: 0.1, y: 0.2, width: 0.3, height: 0.4 };
  assert.equal(canUseSelectedRegionWithoutClarification("clarify", ["ambiguous_reference"], region), true);
  assert.equal(canUseSelectedRegionWithoutClarification("clarify", ["target_candidate_missing"], region), true);
  assert.equal(canUseSelectedRegionWithoutClarification("clarify", ["project_mismatch"], region), false);
  assert.equal(canUseSelectedRegionWithoutClarification("refuse", ["target_candidate_missing"], region), false);
  assert.equal(canUseSelectedRegionWithoutClarification("clarify", ["target_candidate_missing"], null), false);
});

test("shouldPrewarmVisualModel mirrors known automatic slow-route hints", () => {
  assert.equal(shouldPrewarmVisualModel({ visualMode: "auto", targetGrounding: false, query: "请分析这张图表趋势" }), true);
  assert.equal(shouldPrewarmVisualModel({ visualMode: "auto", targetGrounding: false, uiTargetKind: "icon" }), true);
  assert.equal(shouldPrewarmVisualModel({ visualMode: "auto", targetGrounding: false, workflowVisualMode: "slow" }), true);
  assert.equal(shouldPrewarmVisualModel({ visualMode: "auto", targetGrounding: false, query: "请读取截图中的文字" }), false);
  assert.equal(shouldPrewarmVisualModel({ visualMode: "fast", targetGrounding: false, query: "请分析图表" }), false);
});

test("capture-triggered perception warmup is shared and awaited before inference", () => {
  assert.match(app, /perceptionWarmupPromiseRef/);
  assert.match(app, /warmup-perception/);
  assert.match(app, /void requestPerceptionWarmup\(\);/);
  assert.match(app, /if \(perceptionWarmup\) await perceptionWarmup;/);
});

test("accepted media sends release capture UI and keep diagnostics collapsed", () => {
  assert.match(app, /if \(runtimeOverride !== undefined\) clearCapturedMedia\(\);/);
  assert.match(app, /<details className="multimodal-details">\s*<summary>查看详情<\/summary>/);
  assert.doesNotMatch(app, /当前窗口未读取到 UI Automation 控件/);
});

test("locking an image region collapses the large screenshot preview", () => {
  assert.match(app, /setSelectedImageRegion\(region\);\s*setMediaPreviewOpen\(false\);/);
  assert.match(app, /mediaPreviewOpen \? "" : "collapsed"/);
  assert.match(app, /显示\/修改选区/);
});

test("multimodal event forwarding preserves capture timing and project metadata", () => {
  assert.match(app, /const mediaEventTimeBasis = "capture_relative_ms" as const;/);
  assert.match(app, /timestamp_ms: 400,\s+start_ms: 400,\s+end_ms: 1400,\s+event_time_basis: mediaEventTimeBasis/);
  assert.match(app, /timestamp_ms: snapshot\.captured_at_ms,\s+start_ms: snapshot\.captured_at_ms/);
  assert.match(app, /event_time_basis: "unix_epoch_ms"/);
  assert.match(app, /const runtimeEventTimeBasis = perceptionForQuery\?\.event_time_basis \|\| "capture_relative_ms";/);
  assert.match(app, /source: activeDocumentName \? "document_selection" : "screen_capture"/);
  assert.match(app, /\(activeProjectId \? \{ project_id: activeProjectId \} : \{\}\)/);
  assert.match(app, /const startMs = event\.start_ms \?\? event\.timestamp_ms;/);
  assert.match(app, /const endMs = event\.end_ms \?\? startMs;/);
  assert.match(app, /event\.event_time_origin_ms !== undefined && event\.event_time_origin_ms !== null/);
  assert.doesNotMatch(app, /event_id: `\$\{event\.event_id\}-\$\{queryTimestampMs\}`/);
  assert.doesNotMatch(app, /timestamp_ms: queryTimestampMs,\s+\(\.\.\.event\)/);
  assert.doesNotMatch(app, /captured_at_ms: queryTimestampMs/);
});

test("encodeMonoPcmWav writes a 16 kHz mono PCM WAV header and clips samples", async () => {
  const wav = encodeMonoPcmWav(new Float32Array([-2, 0, 2]));
  const view = new DataView(await wav.arrayBuffer());

  assert.equal(new TextDecoder().decode(new Uint8Array(view.buffer, 0, 4)), "RIFF");
  assert.equal(new TextDecoder().decode(new Uint8Array(view.buffer, 8, 4)), "WAVE");
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16_000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getInt16(44, true), -32_768);
  assert.equal(view.getInt16(46, true), 0);
  assert.equal(view.getInt16(48, true), 32_767);
});

test("fitFrameWithinMaxEdge preserves aspect ratio without upscaling", () => {
  assert.deepEqual(fitFrameWithinMaxEdge(3_840, 2_160), { width: 1_920, height: 1_080 });
  assert.deepEqual(fitFrameWithinMaxEdge(1_080, 2_160), { width: 960, height: 1_920 });
  assert.deepEqual(fitFrameWithinMaxEdge(1_280, 720), { width: 1_280, height: 720 });
});

test("imageRegionFromPointer clamps a drag to the image and normalizes its coordinates", () => {
  assert.deepEqual(
    imageRegionFromPointer(
      { left: 100, top: 50, width: 800, height: 400 },
      { x: 900, y: 450 },
      { x: 200, y: 100 },
    ),
    { x: 0.125, y: 0.125, width: 0.875, height: 0.875 },
  );
  assert.equal(
    imageRegionFromPointer(
      { left: 0, top: 0, width: 100, height: 100 },
      { x: 10, y: 10 },
      { x: 10, y: 10 },
    ),
    null,
  );
});
