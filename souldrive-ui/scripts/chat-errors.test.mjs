import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatChatHttpFailure,
  formatChatTransportFailure,
} from "../../build/chat-errors-test/chatErrors.js";

test("transport errors distinguish an offline sidecar", () => {
  assert.match(
    formatChatTransportFailure({ timedOut: false, sidecarReachable: false, receivedOutput: false }),
    /^后端未启动：/,
  );
});

test("transport errors distinguish a model timeout", () => {
  assert.match(
    formatChatTransportFailure({ timedOut: true, sidecarReachable: true, receivedOutput: false }),
    /^模型超时：/,
  );
});

test("transport errors report an interrupted generation as backend busy", () => {
  assert.match(
    formatChatTransportFailure({ timedOut: false, sidecarReachable: true, receivedOutput: true }),
    /^后端忙：/,
  );
});

test("HTTP errors retain their status category", () => {
  assert.match(formatChatHttpFailure(503, "warming up"), /^后端忙：warming up/);
  assert.match(formatChatHttpFailure(422), /^请求失败：本轮输入超过服务限制/);
  assert.match(formatChatHttpFailure(500), /^请求失败：sidecar 返回服务端错误/);
});
