import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");
const css = readFileSync(resolve("src", "App.css"), "utf8");

test("document import exposes explicit global and project scopes", () => {
  assert.match(app, /useState<"global" \| "project">\("global"\)/);
  assert.match(app, /新导入文档范围/);
  assert.match(app, /全局知识库（所有项目可检索）/);
  assert.match(app, /\/documents\/globalize/);
  assert.match(app, /归为全局/);
});

test("project workspace exposes deletion and preserves an empty main conversation", () => {
  assert.match(app, /project-delete-button/);
  assert.match(app, /session-delete-button/);
  assert.match(app, /当前会话暂无消息，可直接输入问题/);
  assert.match(app, /if \(remaining\[0\]\) \{[\s\S]*?selectProjectSession\(remaining\[0\]\)[\s\S]*?\} else \{[\s\S]*?body: JSON\.stringify\(\{ title: "主对话" \}\)[\s\S]*?setProjectSessions\(\[createdPayload\.session\]\)[\s\S]*?setActiveSessionId\(createdPayload\.session\.session_id\)[\s\S]*?setMessages\(\[\]\);/);
  assert.match(app, /恢复主对话失败/);
  assert.match(app, /requestId !== sessionLoadRequestRef\.current/);
});

test("project space header has actions but no project count", () => {
  assert.match(app, /<div className="panel-block project-space-block">[\s\S]*?<div className="panel-title">[\s\S]*?项目空间[\s\S]*?<span className="project-space-actions">/);
  assert.doesNotMatch(app, /项目空间[\s\S]{0,240}(?:项目数量|\{projects\.length\}|projects\.length)/);
});

test("switching projects clears stale workflow state before loading the new session", () => {
  assert.match(app, /setSelectedWorkflowId\(""\);/);
  const openSessionDialog = app.match(/const openProjectSessionDialog = \(\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.match(openSessionDialog, /setSessionCreateWorkflowId\(""\);/);
});

test("answer bubble actions occupy the full bottom row and align to the right", () => {
  const actionsRule = css.match(/\.message-actions \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(actionsRule, /align-self: stretch;/);
  assert.match(actionsRule, /width: 100%;/);
  assert.match(actionsRule, /justify-content: flex-end;/);
  assert.match(actionsRule, /flex-wrap: nowrap;/);
  assert.doesNotMatch(actionsRule, /width: max-content;/);
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
});
