import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");

test("learning review UI is wired to reflection APIs", () => {
  assert.match(app, /ReflectionCandidate/);
  assert.match(app, /requestReflectionDraft/);
  assert.match(app, /submitReflectionDecisions/);
  assert.match(app, /learning-review-dialog/);
  assert.match(app, /\/reflection\/draft/);
  assert.match(app, /reflectionDraftIdempotencyKeyRef/);
  assert.match(app, /request_id: idempotencyKey/);
  assert.match(app, /\/reflection\/reviews\/\$\{activeReview\.review_id\}\/decide/);
  assert.match(app, /\/learning\/memories\/runtime/);
  assert.match(app, /\/learning\/memories\/\$\{encodeURIComponent\(notice\.memory_id\)\}\/revoke/);
  assert.match(app, /saveRuntimeMemory/);
  assert.match(app, /interface LearningMemory/);
  assert.match(app, /interface MemoryHealth/);
  assert.match(app, /setMemoryHealth/);
  assert.match(app, /记忆健康检查/);
  assert.match(app, /openMemoryManager/);
  assert.match(app, /learning\/memories\$\{query\}/);
  assert.match(app, /method: "PATCH"/);
  assert.match(app, /method: "DELETE"/);
  assert.match(app, /confirm=true/);
  assert.match(app, /Idempotency-Key/);
  assert.match(app, /globalThis\.crypto\.randomUUID\(\)/);
  assert.match(app, /isLearningStorageSyncDegraded/);
  assert.match(app, /归档\/检索同步待恢复/);
  assert.match(app, /后续打开记忆管理器或提问时会自动重试/);
  assert.match(app, /warning\?: string/);
  assert.match(app, /候选记忆已生成，但\$\{review\.warning\}/);
  assert.match(app, /payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
  assert.match(app, /learning-memory-dialog/);
  assert.match(app, /window\.confirm/);
  assert.match(app, /saveRetrievalFeedback/);
  assert.match(app, /revokeRetrievalFeedback/);
  assert.match(app, /diagnostics\/retrieval\/feedback/);
  assert.match(app, /workflow_id: selectedWorkflowId \|\| undefined/);
  assert.match(app, /workflow_id: retrievalTestResult\?\.workflow_id \|\| selectedWorkflowId \|\| undefined/);
  assert.match(app, /feedback\.workflow_id/);
  assert.match(app, /const projectQuery = feedback\.project_id/);
  assert.match(app, /feedback\/\$\{encodeURIComponent\(feedback\.feedback_id\)\}\?confirm=true\$\{projectQuery\}/);
  assert.match(app, /const projectQuery = draftArtifact\.project_id/);
  assert.match(app, /\/status\$\{projectQuery\}/);
  assert.match(app, /\/send\$\{projectQuery\}/);
  assert.match(app, /记住全局/);
  assert.match(app, /记住当前项目/);
  assert.doesNotMatch(app, /onCloseRequested/);
  assert.doesNotMatch(app, /closeAfterSubmit/);
});

test("skill requests use the Tauri-owned sidecar channel", () => {
  assert.match(app, /async function requestSkillApi/);
  assert.match(app, /invoke<SkillApiResponse>\("skill_api_request"/);
  assert.match(app, /if \(!runtimeConfigLoaded\) return;/);
  assert.match(app, /requestSkillApi<\{ skills\?: LearningSkill\[\]; warning\?: string \}>\("DELETE"/);
  assert.match(app, /payload\.warning \? `；\$\{payload\.warning\}`/);
  assert.match(app, /payload\.replayed \? /);
  assert.match(app, /回放/);
  assert.match(app, /skillInstallTarget/);
  assert.match(app, /target: skillInstallTarget/);
  assert.match(app, /project_id: activeProjectId/);
  assert.match(app, /\/learning\/skills\$\{projectQuery\}/);
  assert.match(app, /\/learning\/skills\/inbox\$\{projectQuery\}/);
  assert.match(app, /\/learning\/skills\/\$\{encodeURIComponent\(skill\.skill_id\)\}\$\{projectQuery\}/);
  assert.doesNotMatch(app, /testProjectSkill/);
  assert.doesNotMatch(app, /测试脚本/);
  assert.doesNotMatch(app, /编辑 PPT 样式/);
  assert.doesNotMatch(app, /setSkillMessage\("Failed to fetch"\)/);
});

test("skill manager deduplicates copies and limits PPT actions to presentation skills", () => {
  assert.match(app, /const content = skill\.content\?\.trim\(\)/);
  assert.match(app, /skill\.target === "presentation" && skill\.compatibility_status === "verified"/);
  assert.match(app, /对话 Skills/);
  assert.match(app, /PPT Skills/);
  assert.match(app, /window\.setTimeout\(\(\) => setSkillMessage\(""\), 4500\)/);
  assert.match(app, /aria-label="关闭提示"/);
  assert.doesNotMatch(app, /演示\|幻灯\|汇报/);
});

test("durable replay outcomes remain visible in project workflows", () => {
  assert.match(app, /replayed\?: boolean; error\?: string/);
  assert.match(app, /项目创建请求已回放/);
  assert.match(app, /会话创建请求已回放/);
  assert.match(app, /本地联系人请求已回放/);
  assert.match(app, /请求已回放已有本地草稿/);
  assert.match(app, /已回放已有索引任务/);
  assert.match(app, /索引任务已提交/);
});
