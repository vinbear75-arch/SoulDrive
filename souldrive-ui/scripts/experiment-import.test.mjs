import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("experiment windows expose a project-bound metric preview and confirmation entry", () => {
  assert.match(app, /<ExperimentMetricImportPanel/);
  assert.match(app, /key=\{activeProjectId\}[\s\S]*?projectId=\{activeProjectId\}/);
  assert.match(app, /onImported=\{\(\) => setExperimentRunsRefresh\(\(value\) => value \+ 1\)\}/);
});

const { emptyMetricFields, isProjectMetricSource, metricPreviewRequest, metricConfirmationRequest } = await import("../../build/experiment-import-test/experimentImport.js");
const source = { name: "metrics.csv", document_id: "doc-a", scope: "project", project_id: "a" };
const fields = { ...emptyMetricFields, experiment_name: "engineering-alignment" };
const result = {
  status: "confirmation_required", source_document_id: "doc-a", source_version_id: "version-a",
  plan: { ready: true, requires_confirmation: true, writes: false, confirmed: false, row_count: 1, rows: [{ metric_name: "accuracy", value: 0.81, unit: "%", source_locator: "row:1" }], missing_fields: [] },
};
const bound = () => ({ projectId: "a", request: metricPreviewRequest("a", source, fields), result: structuredClone(result) });

test("preview requests never write or infer missing metric conditions", () => {
  const request = metricPreviewRequest("a", source, fields);
  assert.equal(request.confirmed, false);
  for (const key of Object.keys(emptyMetricFields).filter((key) => key !== "experiment_name")) assert.equal(request[key], "");
  assert.equal(request.source_document_id, "doc-a");
  assert.equal(request.source_version_id, undefined);
});

test("confirmation binds the exact previewed file, project and version", () => {
  assert.deepEqual(metricConfirmationRequest(bound(), "a", source, fields), { ...bound().request, source_version_id: "version-a", confirmed: true });
});

for (const changedSource of [
  { ...source, scope: "global" }, { ...source, project_id: "b" },
  { ...source, scope: "ambiguous" }, { ...source, document_id: undefined }, { ...source, name: "slides.pptx" },
]) test(`unsupported or unbound source is excluded: ${JSON.stringify(changedSource)}`, () => {
  assert.equal(isProjectMetricSource(changedSource, "a"), false);
  assert.throws(() => metricPreviewRequest("a", changedSource, fields));
});

test("changing the project or source identity invalidates confirmation", () => {
  assert.throws(() => metricConfirmationRequest(bound(), "b", { ...source, project_id: "b" }, fields));
  assert.throws(() => metricConfirmationRequest(bound(), "a", { ...source, document_id: "doc-new" }, fields));
  assert.throws(() => metricConfirmationRequest(bound(), "a", { ...source, name: "renamed.csv" }, fields));
});

test("changing any field invalidates confirmation, including explicit unit and time", () => {
  for (const key of Object.keys(fields)) assert.throws(() => metricConfirmationRequest(bound(), "a", source, { ...fields, [key]: "changed" }), key);
});

for (const change of [
  { source_version_id: "" }, { source_document_id: "other" }, { status: "imported" },
  { plan: { ...result.plan, ready: false } }, { plan: { ...result.plan, writes: true } },
  { plan: { ...result.plan, confirmed: true } }, { plan: { ...result.plan, missing_fields: ["unit"] } },
]) test(`incomplete or mismatched preview cannot authorize writes: ${JSON.stringify(change)}`, () => {
  const preview = bound(); Object.assign(preview.result, change);
  assert.throws(() => metricConfirmationRequest(preview, "a", source, fields));
});

test("UI clears previews on field/source/auth changes and aborts old in-flight requests", () => {
  const panel = readFileSync(new URL("../src/ExperimentMetricImportPanel.tsx", import.meta.url), "utf8");
  assert.match(panel, /requestRef\.current\?\.abort\(\)/);
  assert.match(panel, /\[projectId, locked, apiBaseUrl, apiToken\]/);
  assert.match(panel, /if \(controller\.signal\.aborted\) return/);
  assert.match(panel, /setSourceId\(event\.target\.value\); setPreview\(null\)/);
  assert.match(panel, /const changeField[\s\S]*?setPreview\(null\)/);
  assert.match(panel, /disabled=\{!canConfirm\}/);
  assert.doesNotMatch(panel, /new Date\(|Date\.now\(/);
});

test("an empty or partial preview cannot authorize unseen metric rows", () => {
  for (const plan of [{ ...result.plan, rows: [] }, { ...result.plan, row_count: 2 }]) {
    const preview = bound(); preview.result.plan = plan;
    assert.throws(() => metricConfirmationRequest(preview, "a", source, fields));
  }
});
