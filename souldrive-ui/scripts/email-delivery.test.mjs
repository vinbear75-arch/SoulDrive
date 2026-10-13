import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");

test("email delivery distinguishes a successful replay from a fresh SMTP send", () => {
  assert.match(app, /idempotent\?: boolean/);
  assert.match(app, /delivery_status\?: string/);
  assert.match(app, /warning\?: string/);
  assert.match(app, /邮件发送请求已回放，未重复调用 SMTP/);
  assert.match(app, /邮件已通过已配置的 SMTP 服务发送，发送记录已写入本地审计日志/);
  assert.match(app, /payload\.warning \? ` 注意：\$\{payload\.warning\}`/);
});
