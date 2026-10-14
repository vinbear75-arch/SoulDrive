import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import ts from "typescript";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");
const css = readFileSync(resolve("src", "App.css"), "utf8");

test("workflow readiness rejection is visible beside the text input", () => {
  assert.match(app, /const \[workflowMessage, setWorkflowMessage\] = useState\(""\)/);
  assert.match(app, /className="workflow-readiness-notice" role="alert"[\s\S]*?\{workflowMessage\}/);
  assert.match(app, /const \[workflowBusy, setWorkflowBusy\] = useState\(false\)/);
});

test("workflow review explicitly binds one project and the displayed version", () => {
  const review = app.match(/const approveCurrentWorkflow = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(review, /if \(!activeProjectId \|\| !selectedWorkflow \|\| locked \|\| workflowBusy\) return/);
  assert.ok(review.includes("/projects/${encodeURIComponent(activeProjectId)}/playbooks/${selectedWorkflow.workflow_id}/approval"));
  assert.match(review, /playbook_version: selectedWorkflow\.playbook_version, approved: true/);
  assert.match(review, /if \(!response\.ok\)/);
  assert.match(app, /确认当前模式用途/);
  assert.match(app, /selectedWorkflow\.input_contract\.map/);
  assert.match(app, /selectedWorkflow\.side_effects\.map/);
});

test("stale workflow requests cannot update or submit another session", () => {
  assert.match(app, /workflowScopeRef\.current = JSON\.stringify\(\[activeProjectId, activeSessionId, selectedWorkflowId, apiBaseUrl, apiToken, locked\]\)/);
  const plan = app.match(/const requestWorkflowPlan = async \([\s\S]*?\n  \};/)?.[0] || "";
  const ready = app.match(/const ensureWorkflowReady = async \([\s\S]*?\n  \};/)?.[0] || "";
  const review = app.match(/const approveCurrentWorkflow = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  for (const body of [plan, ready, review]) {
    assert.match(body, /const scope = workflowScopeRef\.current/);
    assert.match(body, /scope !== workflowScopeRef\.current/);
  }
});

function workflowHarness(fetchImpl) {
  const names = ["requestWorkflowPlan", "approveCurrentWorkflow", "ensureWorkflowReady"];
  const functions = names.map((name) => app.match(new RegExp(`const ${name} = async \\([\\s\\S]*?\\n  \\};`))?.[0]).join("\n");
  const js = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const calls = [];
  const scope = { current: "project-a/session-a" };
  const values = {
    fetch: fetchImpl, workflowScopeRef: scope, activeProjectId: "a", selectedWorkflowId: "experiment_compare",
    selectedWorkflow: { workflow_id: "experiment_compare", playbook_version: "1.0" }, locked: false, workflowBusy: false,
    apiBaseUrl: "http://local-test", activeDocumentNames: [], activeDocumentIds: [], mediaFiles: {},
    speechDraftRef: { current: "" }, uiAutomationSnapshot: null, baselineExperimentRunId: "baseline-a",
    currentExperimentRunId: "current-a", authHeaders: () => ({}),
    ...Object.fromEntries(["setWorkflowBusy", "setWorkflowMessage", "setWorkflowPlan", "setWorkflowSpecs"].map((name) => [name, (value) => calls.push([name, value])])),
  };
  const functionsApi = new Function(...Object.keys(values), `${js}\nreturn {${names.join(",")}};`)(...Object.values(values));
  return { ...functionsApi, calls, scope };
}

test("workflow preflight rejection keeps the actual reason without submitting chat", async () => {
  const requests = [];
  const harness = workflowHarness(async (url, options) => {
    requests.push([url, JSON.parse(options.body)]);
    return { ok: true, json: async () => ({ ready: false, next_action: "请先审核通过当前项目的 Playbook" }) };
  });
  assert.equal(await harness.ensureWorkflowReady("fixed question"), false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0][1].project_id, "a");
  assert.ok(harness.calls.some(([name, value]) => name === "setWorkflowMessage" && value.includes("审核")));
});

test("explicit approval sends one version-bound request and does not auto-execute", async () => {
  const requests = [];
  const harness = workflowHarness(async (url, options) => {
    requests.push([url, options.method, JSON.parse(options.body)]);
    return { ok: true, json: async () => ({ approval: { status: "approved", playbook_version: "1.0" } }) };
  });
  await harness.approveCurrentWorkflow();
  assert.deepEqual(requests, [["http://local-test/projects/a/playbooks/experiment_compare/approval", "POST", { playbook_version: "1.0", approved: true }]]);
  const update = harness.calls.find(([name]) => name === "setWorkflowSpecs")[1];
  const specs = update([{ workflow_id: "experiment_compare" }, { workflow_id: "paper_review" }]);
  assert.equal(specs[0].approval.status, "approved");
  assert.equal(specs[1].approval, undefined);
});

for (const method of ["approveCurrentWorkflow", "ensureWorkflowReady"]) {
  test(`pending ${method} cannot change or execute a different session`, async () => {
    let release;
    const harness = workflowHarness(() => new Promise((resolve) => { release = resolve; }));
    const pending = harness[method]("fixed question");
    harness.calls.length = 0;
    harness.scope.current = "project-b/session-b";
    release({ ok: true, json: async () => ({ ready: true, approval: { status: "approved", playbook_version: "1.0" } }) });
    const result = await pending;
    assert.equal(result, method === "ensureWorkflowReady" ? false : undefined);
    assert.deepEqual(harness.calls, []);
  });
}

test("stale displayed version is not locally marked approved", async () => {
  const harness = workflowHarness(async () => ({ ok: false, json: async () => ({ error: "playbook version is stale" }) }));
  await harness.approveCurrentWorkflow();
  assert.ok(harness.calls.some(([name, value]) => name === "setWorkflowMessage" && value === "playbook version is stale"));
  assert.ok(!harness.calls.some(([name]) => name === "setWorkflowSpecs"));
});

test("PPT source choices exclude another project's same-name file", () => {
  const source = app.match(/function isPresentationDocumentInProject\([\s\S]*?\n\}/)?.[0];
  assert.ok(source, "PPT must scope choices before auto-selection, not just reject on the server");
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const matches = new Function(`${js}\nreturn isPresentationDocumentInProject;`)();
  assert.equal(matches({ scope: "project", project_id: "a" }, "a"), true);
  assert.equal(matches({ scope: "project", project_id: "b" }, "a"), false);
  assert.equal(matches({ scope: "global" }, "a"), true);
  assert.equal(matches({ scope: "ambiguous", project_ids: ["a", "b"] }, "a"), false);
  assert.equal(matches({ scope: "project", project_ids: ["a"] }, "a"), true);
  assert.equal(matches({}, "a"), false);
  assert.equal(matches({ scope: "global" }, null), false);
  assert.match(app, /const presentationDocuments = useMemo/);
  assert.match(app, /presentationDocuments\.map\(\(document\)/);
  const builder = app.match(/const openPresentationBuilder = \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(builder, /presentationDocuments\.slice\(0, 3\)/);
  const recent = app.match(/const selectRecentPresentationSources = \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(recent, /const recent = presentationDocuments/);
});

test("PPT dialog can reload project-scoped durable tasks without starting a worker", () => {
  assert.ok(app.includes('className="presentation-task-list"'), "missing durable task list");
  assert.ok(app.includes('/presentations/tasks?project_id=${encodeURIComponent(activeProjectId)}&limit=30'));
  assert.match(app, /payload\.tasks\.filter\(\(task\) => task\.project_id === activeProjectId\)/);
  assert.match(app, /onClick=\{\(\) => selectPresentationTask\(task\)\}/);
  const select = app.match(/const selectPresentationTask = \(task: PresentationTask\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(select, /if \(task\.project_id !== activeProjectId\) return/);
  assert.doesNotMatch(select, /fetch\(|confirmPresentation\(|resumePresentation\(|generatePresentation\(/);
  assert.match(app, /定位结果文件/);
});

test("same-task resume restarts bounded polling and waits for a new lease", () => {
  const source = app.match(/function shouldPollPresentationTask\([\s\S]*?\n\}/)?.[0];
  assert.ok(source, "same task id must be able to restart polling");
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const poll = new Function(`${js}\nreturn shouldPollPresentationTask;`)();
  assert.equal(poll({ status: "interrupted", lease_epoch: 3 }, 3, 0), true);
  assert.equal(poll({ status: "interrupted", lease_epoch: 4 }, 3, 1), false);
  assert.equal(poll({ status: "interrupted", lease_epoch: 3 }, 3, 10), false);
  assert.equal(poll({ status: "completed", lease_epoch: 4 }, 3, 1), false);
  assert.equal(poll({ status: "cancelled", lease_epoch: 3 }, 3, 1), false);
  assert.equal(poll({ status: "running", lease_epoch: 4 }, 3, 11), true);
  assert.equal(poll({ status: "failed", lease_epoch: 3 }, undefined, 0), false);
  const resume = app.match(/const resumePresentation = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(resume, /setPresentationPollRefresh\(\(value\) => value \+ 1\)/);
  assert.match(app, /presentationTask\?\.task_id, presentationPollRefresh\]/);
});

test("PPT outline and result remain scrollable without overlapping options", () => {
  const options = css.match(/\.presentation-options \{[\s\S]*?\n\}/)?.[0] || "";
  const state = css.match(/\.presentation-task-state \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(options, /overflow-y: auto/);
  assert.match(options, /grid-auto-rows: max-content/);
  assert.match(state, /align-content: start/);
});

test("ledger-grounded PPT is labelled as ledger evidence, not a custom model outline", () => {
  assert.ok(app.includes('outline_source === "experiment_ledger_extractive" ? "账本测量提纲"'));
});

test("saved PPT tasks do not show unrelated editable default parameters", () => {
  const options = app.slice(app.indexOf('<div className="presentation-options">'), app.indexOf('<div className={`presentation-task-state'));
  assert.match(options, /presentationTask \? \(/);
  assert.match(options, /当前查看已保存任务，使用原计划参数/);
  assert.match(options, /\) : \(<>/);
  assert.match(app, /onClick=\{selectRecentPresentationSources\} disabled=\{Boolean\(presentationTask\)\}/);
  assert.match(app, /disabled=\{Boolean\(presentationTask\) \|\| \(!isPresentationSource/);
  assert.match(app, /onClick=\{resetPresentationPlan\}>新建大纲/);
});

test("desktop background polling leaves the default API budget for user actions", () => {
  const background = app.match(/const refreshBackgroundData = \(\) => \{([\s\S]*?)\n    \};/);
  assert.ok(background, "protected data must not share the 3-second public status timer");
  const names = [...background[1].matchAll(/void (refresh\w+)\(\);/g)].map((match) => match[1]);
  assert.equal(names.length, 7);
  const calls = [];
  const refresh = new Function("locked", "apiToken", ...names, background[1]);
  const callbacks = names.map((name) => () => calls.push(name));
  refresh(true, "test-token", ...callbacks);
  refresh(false, "", ...callbacks);
  assert.equal(calls.length, 0, "locked or unauthenticated polling must not consume the budget");
  refresh(false, "test-token", ...callbacks);
  assert.equal(calls.length, 7);
  const interval = Number(app.match(/setInterval\(refreshBackgroundData,\s*([\d_]+)\)/)?.[1].replaceAll("_", ""));
  const backupInterval = Number(app.match(/setInterval\(\(\) => void refreshBackupPreflight\(\),\s*([\d_]+)\)/)?.[1].replaceAll("_", ""));
  // Include the existing 30-second schedule and 1.2-second active task polls.
  const requestsPerMinute = names.length * 60_000 / interval + 60_000 / backupInterval + 2 + 50;
  assert.ok(requestsPerMinute <= 100, `steady polling uses ${requestsPerMinute}/120 requests`);
  assert.match(app, /setInterval\(refreshFastStatus, 3000\)/);
});

test("project sessions wait for unlock and reload when lock state changes", () => {
  const historyIndex = app.indexOf("const loadHistory = async (session: ProjectSession)");
  const loader = app.slice(app.lastIndexOf("  useEffect(() => {", historyIndex), app.indexOf("\n  useEffect(", historyIndex));
  assert.match(loader, /if \(!runtimeConfigLoaded \|\| !apiToken \|\| !activeProjectId \|\| locked\)/);
  assert.match(loader, /\}, \[activeProjectId, apiBaseUrl, apiToken, locked, runtimeConfigLoaded\]\);/);
});

test("experiment ISO dates are converted to the document formatter's seconds once", () => {
  const body = app.match(/function formatDocumentTime\(timestamp\?: number\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(body);
  const format = new Function("timestamp", body);
  const argumentsInUse = [...app.matchAll(/formatDocumentTime\((Date\.parse\(run\.performed_at\)(?:\s*\/\s*1000)?)\)/g)];
  assert.equal(argumentsInUse.length, 3, "overview and both run choices use the same conversion");
  for (const match of argumentsInUse) {
    const timestamp = new Function("run", `return ${match[1]};`);
    for (const performed_at of ["2026-09-12T02:00:00+00:00", "2026-10-12T02:00:00+00:00"]) {
      const expected = new Date(performed_at).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
      assert.equal(format(timestamp({ performed_at })), expected);
    }
  }
});

test("experiment windows expose explicit baseline and current run choices", () => {
  assert.match(app, /const \[experimentRuns, setExperimentRuns\]/);
  assert.match(app, /aria-label="基线实验运行"[\s\S]*?value=\{baselineExperimentRunId\}[\s\S]*?onChange=\{\(event\) => setBaselineExperimentRunId\(event\.target\.value\)\}/);
  assert.match(app, /aria-label="当前实验运行"[\s\S]*?value=\{currentExperimentRunId\}[\s\S]*?onChange=\{\(event\) => setCurrentExperimentRunId\(event\.target\.value\)\}/);
  assert.match(app, /experimentRuns\.map\(\(run\) =>/);
  assert.match(app, /disabled=\{run\.run_id === baselineExperimentRunId\}/);
  assert.doesNotMatch(app, /setBaselineExperimentRunId\(experimentRuns\[0\]/);
  const runLoader = app.slice(app.indexOf("void fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/experiments/runs"));
  assert.match(runLoader.slice(0, runLoader.indexOf("\n  useEffect(")), /runtimeConfigLoaded, experimentRunsRefresh\]/);
});

test("comparison PPT requests carry the same explicitly selected run pair", () => {
  const generator = app.match(/const generatePresentation = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(generator, /selectedWorkflowId === "experiment_compare"/);
  assert.match(generator, /baseline_run_id: baselineExperimentRunId \|\| undefined/);
  assert.match(generator, /current_run_id: currentExperimentRunId \|\| undefined/);
});

test("document import exposes explicit global and project scopes", () => {
  assert.match(app, /useState<"global" \| "project">\("global"\)/);
  assert.match(app, /新导入文档范围/);
  assert.match(app, /全局知识库（所有项目可检索）/);
  assert.match(app, /\/documents\/globalize/);
  assert.match(app, /归为全局/);
});

test("document library exposes per-file indexing status and failure details", () => {
  assert.match(app, /index_status\?: "indexed" \| "failed" \| "processing" \| "pending" \| "changed"/);
  assert.match(app, /索引失败/);
  assert.match(app, /正在处理当前文档/);
  assert.match(app, /原文已变化/);
  assert.match(app, /原文件内容已变化，请重新启动索引/);
  assert.match(app, /等待手动启动索引/);
  assert.match(app, /document-library-row-diagnostic/);
  assert.match(css, /\.document-library-row-meta span\.failed/);
  assert.match(css, /\.document-library-row-meta span\.processing/);
  assert.match(css, /\.document-library-row-meta span\.changed/);
});

test("document library exposes a read-only index manifest preview", () => {
  assert.match(app, /\/index\/preview/);
  assert.match(app, /检查变更/);
  assert.match(app, /索引变更预览/);
  assert.match(app, /只读检查 · 不会修改文档或索引/);
  assert.match(app, /indexPreview\.counts\.modified/);
  assert.match(css, /\.index-preview-summary/);
});

test("runtime details expose read-only backup preflight without a backup action", () => {
  assert.match(app, /diagnostics\/backup/);
  assert.match(app, /备份预检/);
  assert.match(app, /可进入人工备份/);
  assert.doesNotMatch(app, /backup_workspace|restore_workspace/);
});

test("index rebuild retries reuse an input-bound idempotency key", () => {
  assert.match(app, /const indexRequestIdempotencyKeyRef = useRef\(""\);/);
  const startIndexing = app.match(/const startIndexing = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(startIndexing, /crypto\.randomUUID\(\)/);
  assert.match(startIndexing, /"Idempotency-Key": indexRequestIdempotencyKeyRef\.current/);
  assert.match(app, /indexRequestRunIdRef\.current/);
});

test("project creation retries reuse an input-bound idempotency key", () => {
  assert.match(app, /const projectCreateFingerprintRef = useRef\(""\);/);
  assert.match(app, /const projectCreateIdempotencyKeyRef = useRef\(""\);/);
  const createProject = app.match(/const createProject = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(createProject, /crypto\.randomUUID\(\)/);
  assert.match(createProject, /"Idempotency-Key": projectCreateIdempotencyKeyRef\.current/);
  assert.match(createProject, /projectCreateFingerprintRef\.current/);
});

test("document preview carries the active project scope", () => {
  assert.match(app, /const previewQuery = new URLSearchParams\(\{ name: document\.name \}\);/);
  assert.match(app, /if \(document\.document_id\) previewQuery\.set\("document_id", document\.document_id\);/);
  assert.match(app, /previewQuery\.set\("project_id", activeProjectId\);/);
});

test("document mutations carry the explicit document identity", () => {
  assert.match(app, /document_id\?: string;/);
  assert.match(app, /document_id: documentDeleteTarget\.document_id/);
  assert.match(app, /body: JSON\.stringify\(\{ name: document\.name, document_id: document\.document_id \}\)/);
});

test("PPT source selection carries display labels and exact document identities", () => {
  assert.match(app, /interface PresentationSourceRef/);
  assert.match(app, /const \[presentationSources, setPresentationSources\] = useState<PresentationSourceRef\[\]>\(\[\]\);/);
  assert.match(app, /source_document_ids: presentationSourceIds/);
  assert.match(app, /document\.document_id \|\| document\.relative_path/);
  assert.match(app, /const isPresentationSource = \(document: DocumentItem\)/);
});

test("PPT evidence status exposes ambiguous display-label attribution", () => {
  assert.match(app, /selected_identity_count\?: number/);
  assert.match(app, /source_identity_coverage_status\?:/);
  assert.match(app, /同名展示标签无法精确归因/);
  assert.match(app, /formatPresentationEvidenceCoverage/);
  assert.match(app, /来源标签/);
});

test("PPT mixed identified and legacy sources fail closed before submission", () => {
  assert.match(app, /hasIdentifiedPresentationSource/);
  assert.match(app, /hasLegacyPresentationSource/);
  assert.match(app, /安全对象与普通文件不能混合生成 PPT/);
});

test("project workspace exposes deletion and preserves an empty main conversation", () => {
  assert.match(app, /project-delete-button/);
  assert.match(app, /session-delete-button/);
  assert.match(app, /当前会话暂无消息，可直接输入问题/);
  assert.match(app, /if \(remaining\[0\]\) \{[\s\S]*?selectProjectSession\(remaining\[0\]\)[\s\S]*?\} else \{[\s\S]*?body: JSON\.stringify\(\{ title: "主对话" \}\)[\s\S]*?setProjectSessions\(\[createdPayload\.session\]\)[\s\S]*?setActiveSessionId\(createdPayload\.session\.session_id\)[\s\S]*?setMessages\(\[\]\);/);
  assert.match(app, /恢复主对话失败/);
  assert.match(app, /requestId !== sessionLoadRequestRef\.current/);
});

test("project deletion keeps a post-commit audit warning visible", () => {
  assert.match(app, /warning\?: string/);
  assert.match(app, /项目“\$\{project\.name\}”及其原始项目文件夹已删除。/);
  assert.match(app, /注意：\$\{payload\.warning\}/);
});

test("document deletion keeps a post-commit audit warning visible", () => {
  assert.match(app, /const payload = await response\.json\(\) as \{ error\?: string; warning\?: string; replayed\?: boolean \};/);
  assert.match(app, /文档及其本地索引已删除/);
  assert.match(app, /payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
});

test("document deletion retries reuse an input-bound idempotency key", () => {
  assert.match(app, /const \[documentDeleteIdempotencyKey, setDocumentDeleteIdempotencyKey\]/);
  assert.match(app, /document-delete-\$\{crypto\.randomUUID\(\)\}/);
  const deleteDocument = app.match(/const deleteDocument = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(deleteDocument, /"Idempotency-Key": documentDeleteIdempotencyKey/);
  assert.match(deleteDocument, /replayed\?: boolean/);
});

test("session and contact deletion keep post-commit audit warnings visible", () => {
  assert.match(app, /会话已删除。注意：\$\{payload\.warning\}/);
  assert.match(app, /联系人“\$\{contact\.display_name\}”已删除。\$\{payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
});

test("document globalization keeps a post-commit audit warning visible", () => {
  assert.match(app, /reindex_required\?: boolean; warning\?: string/);
  assert.match(app, /已归为全局知识库，正在重建知识索引与 GraphRAG…\$\{payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
});

test("project space header has actions but no project count", () => {
  assert.match(app, /<div className="panel-block project-space-block">[\s\S]*?<div className="panel-title">[\s\S]*?项目空间[\s\S]*?<span className="project-space-actions">/);
  assert.doesNotMatch(app, /项目空间[\s\S]{0,240}(?:项目数量|\{projects\.length\}|projects\.length)/);
});

test("switching projects clears stale workflow state before loading the new session", () => {
  assert.match(app, /setSelectedWorkflowId\(""\);/);
  const openSessionDialog = app.match(/const openProjectSessionDialog = \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(openSessionDialog, /setSessionCreateWorkflowId\(selectedWorkflowId\);/);
  assert.match(app, /setPresentationSources\(\[\]\);/);
  assert.match(app, /setPresentationTask\(null\);/);
});

test("project session history requests carry the active project scope", () => {
  assert.match(
    app,
    /sessions\/\$\{encodeURIComponent\(session\.session_id\)\}\/history\?limit=32&project_id=\$\{encodeURIComponent\(activeProjectId\)\}/,
  );
});

test("plain-text session messages use the same workflow readiness gate as media messages", () => {
  const handleSubmit = app.match(/const handleSubmit = \(event: React\.FormEvent\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(handleSubmit, /if \(mediaFiles\.audio \|\| mediaFiles\.image\)/);
  assert.match(handleSubmit, /const query = input\.trim\(\);/);
  assert.match(handleSubmit, /await ensureWorkflowReady\(query\)/);
  assert.match(handleSubmit, /await submitQuery\(query\);/);
});

test("answer bubble actions occupy the full bottom row and align to the right", () => {
  const actionsRule = css.match(/\.message-actions \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(actionsRule, /align-self: stretch;/);
  assert.match(actionsRule, /width: 100%;/);
  assert.match(actionsRule, /justify-content: flex-end;/);
  assert.match(actionsRule, /flex-wrap: nowrap;/);
  assert.doesNotMatch(actionsRule, /width: max-content;/);
});

test("multiple selected filenames cannot expand the chat column or hide the send control", () => {
  const dialogRule = css.match(/\.dialog-panel \{[\s\S]*?\n\}/)?.[0] || "";
  const contextRule = css.match(/\.active-document-context \{[\s\S]*?\n\}/)?.[0] || "";
  const namesRule = css.match(/\.active-document-context span \{[\s\S]*?\n\}/)?.[0] || "";
  const hintRule = css.match(/\.active-document-context small \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(dialogRule, /grid-template-columns: minmax\(0, 1fr\);/);
  assert.match(contextRule, /flex-wrap: wrap;/);
  assert.match(namesRule, /flex: 1 1 0;/);
  assert.match(namesRule, /min-width: 0;/);
  assert.match(hintRule, /flex: 1 0 100%;/);
  assert.match(hintRule, /order: 1;/);
  assert.ok(app.includes('title={activeDocumentNames.join("、")}'));
});

test("skills are deduplicated by skill_id and PPT only shows presentation skills", () => {
  assert.match(app, /function uniqueSkillsById/);
  assert.match(app, /uniqueSkillsById\(\(payload\.skills \|\| \[\]\)\.filter\(isPresentationSkill\)\)/);
  assert.match(app, /uniqueSkillsById\(learningSkills\)/);
  assert.match(app, /cloudModelConfig\.models\.map\(\(model\) => <option key=\{model\} value=\{model\}>\{model\}<\/option>\)/);
  assert.match(app, /const key = skill\.skill_id\.trim\(\) \|\|/);
  assert.doesNotMatch(app, /const key = skill\.title\.trim\(\)\.toLowerCase\(\) \|\| skill\.skill_id/);
  assert.match(app, /<option value="">不使用 Skills<\/option>/);
  assert.match(app, /setPresentationStyleSkillId\(""\);/);
});

test("project creation stays inside project space before the separate business module", () => {
  const projectSpaceStart = app.indexOf('className="panel-block project-space-block"');
  const createForm = app.indexOf('className="project-create-inline"');
  const businessModule = app.indexOf('aria-label="业务模块"');
  assert.ok(projectSpaceStart >= 0 && createForm > projectSpaceStart);
  assert.ok(businessModule > createForm);
  assert.match(css, /\.workflow-tools-grid button \{[\s\S]*?min-width: 0;[\s\S]*?text-overflow: ellipsis;/);
});

test("PPT and Skill operation status is exposed as a compact live status", () => {
  const presentationStatusMarkup = app.match(/className=\{`presentation-task-state \$\{presentationTask\?\.status \|\| "idle"\}`\}[\s\S]*?aria-live="polite"/)?.[0] || "";
  assert.match(presentationStatusMarkup, /role="status"/);
  assert.match(presentationStatusMarkup, /aria-live="polite"/);
  assert.match(app, /className="skill-status-overlay" role="status" aria-live="polite"/);
  const presentationStatusRule = css.match(/\.presentation-task-state span,\s*\.presentation-task-state p \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(presentationStatusRule, /white-space: normal;/);
  assert.match(app, /className="presentation-attempt-history"/);
  assert.match(app, /attempt_history\.map/);
  assert.match(app, /presentation-repetition-warning/);
  assert.match(css, /\.presentation-attempt-history summary \{/);
  assert.match(css, /\.presentation-repetition-warning \{/);
  assert.match(app, /已回放已完成的 PPT 大纲/);
  assert.match(app, /PPT 任务已提交，等待本地 worker 接管/);
  assert.match(app, /worker_started\?: boolean/);
});

test("PPT generation exposes outline confirmation and cancellation controls", () => {
  assert.ok(app.includes("/presentations/tasks/${presentationTask.task_id}/confirm"));
  assert.match(app, /确认并生成 PPT/);
  assert.ok(app.includes("/presentations/tasks/${presentationTask.task_id}/cancel"));
  assert.match(app, /大纲审核/);
  assert.match(app, /presentationRequestId/);
});

test("PPT review displays the binding boundary without presenting source coverage as fact validation", () => {
  assert.ok(app.includes('presentationTask.result.evidence_validation?.notice'));
  assert.ok(app.includes('<span role="status">{presentationTask.result.evidence_validation.notice}</span>'));
  assert.ok(app.includes("来源标注页"));
});

test("PPT task controls carry the durable project scope", () => {
  assert.match(app, /project_id\?: string \| null;/);
  assert.match(app, /const projectQuery = presentationTask\?\.project_id/);
  assert.match(app, /presentations\/tasks\/\$\{taskId\}\$\{projectQuery\}/);
  assert.match(app, /\/confirm\$\{projectQuery\}/);
  assert.match(app, /\/cancel\$\{projectQuery\}/);
  assert.match(app, /\/resume\$\{projectQuery\}/);
});

test("email draft retries reuse an input-bound idempotency key", () => {
  assert.match(app, /const emailDraftFingerprintRef = useRef\(""\);/);
  assert.match(app, /const emailDraftIdempotencyKeyRef = useRef\(""\);/);
  assert.match(app, /idempotency_key: emailDraftIdempotencyKeyRef\.current/);
  assert.match(app, /emailDraftFingerprintRef\.current = emailFingerprint/);
});

test("project session creation uses a stable retry identity", () => {
  assert.match(app, /const sessionCreateFingerprintRef = useRef\(""\);/);
  assert.match(app, /const sessionCreateIdRef = useRef\(""\);/);
  const createSession = app.match(/const createProjectSession = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(createSession, /session_id: sessionCreateIdRef\.current/);
  assert.match(createSession, /sessionCreateFingerprintRef\.current = sessionFingerprint/);
});

test("post-commit audit warnings stay visible across local mutations", () => {
  assert.match(app, /project_id\?: string; replayed\?: boolean; error\?: string; warning\?: string/);
  assert.match(app, /session\?: ProjectSession; replayed\?: boolean; error\?: string; warning\?: string/);
  assert.match(app, /contact_id\?: string; replayed\?: boolean; error\?: string; warning\?: string/);
  assert.match(app, /artifact\?: DraftArtifact; replayed\?: boolean; error\?: string; warning\?: string/);
  assert.match(app, /payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
  assert.match(app, /日程已删除。\$\{payload\.warning/);
});
