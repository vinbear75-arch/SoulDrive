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
  assert.match(app, /\/reflection\/reviews\/\$\{activeReview\.review_id\}\/decide/);
  assert.match(app, /onCloseRequested/);
  assert.match(app, /closeAfterSubmit/);
  assert.match(app, /确认记忆并关闭/);
});
