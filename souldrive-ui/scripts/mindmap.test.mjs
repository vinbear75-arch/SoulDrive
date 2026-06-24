import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatMindmapBranch,
  parseMindmap,
} from "../../build/mindmap-test/mindmap.js";

test("parseMindmap nests bullet content under the preceding heading", () => {
  const artifact = parseMindmap(`
# 生成式人工智能服务安全基本要求
## 安全措施
### 适用边界
- 对服务提供者的要求如下：明确适用人群、场合、用途。
`);

  const safetyMeasures = artifact.nodes[0].children[0];
  const scopeBoundary = safetyMeasures.children[0];

  assert.equal(safetyMeasures.title, "安全措施");
  assert.equal(scopeBoundary.title, "适用边界");
  assert.equal(scopeBoundary.children[0].title, "对服务提供者的要求如下：明确适用人群、场合、用途。");
});

test("parseMindmap keeps the question as level one and answer sections as level two", () => {
  const artifact = parseMindmap(`
# 生成式人工智能服务安全基本要求主要覆盖哪些方面？
## 语料安全
- 训练数据质量、安全性和合规性是基础。
  - 数据来源要可追溯。
## 模型安全
- 重点关注对抗攻击、越狱和提示注入。
`);

  assert.equal(artifact.nodes[0].title, "生成式人工智能服务安全基本要求主要覆盖哪些方面？");
  assert.equal(artifact.nodes[0].children[0].title, "语料安全");
  assert.equal(artifact.nodes[0].children[0].children[0].title, "训练数据质量、安全性和合规性是基础。");
  assert.equal(artifact.nodes[0].children[0].children[0].children[0].title, "数据来源要可追溯。");
  assert.equal(artifact.nodes[0].children[1].title, "模型安全");
});

test("parseMindmap keeps same-indent bullets as siblings", () => {
  const artifact = parseMindmap(`
# 生成式人工智能服务安全基本要求主要覆盖哪些方面？
## 语料安全
- 训练数据质量、安全性和合规性是基础。
  - 数据来源要可追溯。
  - 敏感内容要清洗。
`);

  const detail = artifact.nodes[0].children[0].children[0];

  assert.equal(detail.children.length, 2);
  assert.equal(detail.children[0].title, "数据来源要可追溯。");
  assert.equal(detail.children[1].title, "敏感内容要清洗。");
  assert.equal(detail.children[0].children.length, 0);
});

test("parseMindmap keeps final summary heading at level two", () => {
  const artifact = parseMindmap(`
# 数据要素治理目标
## “供得出” 指向数据供给的增加和提升
## “流得动” 指的是数据流通顺畅
## “用得好” 强调的是高效利用数据
## “保安全” 是指保障数据的安全性
## 归纳总结
- 虽然用户请求以导图形式展示，但上述归纳可以作为理解四个目标的基础框架。
`);

  const root = artifact.nodes[0];

  assert.equal(root.children.length, 5);
  assert.equal(root.children[3].title, "“保安全” 是指保障数据的安全性");
  assert.equal(root.children[3].children.length, 0);
  assert.equal(root.children[4].title, "归纳总结");
  assert.equal(root.children[4].children[0].title, "虽然用户请求以导图形式展示，但上述归纳可以作为理解四个目标的基础框架。");
});

test("formatMindmapBranch includes full nested branch text", () => {
  const artifact = parseMindmap(`
# 生成式人工智能服务安全基本要求
## 安全措施
### 适用边界
- 对服务提供者的要求如下：明确适用人群、场合、用途。
`);

  const branch = formatMindmapBranch(artifact.nodes[0].children[0]);

  assert.match(branch, /安全措施/);
  assert.match(branch, /适用边界/);
  assert.match(branch, /明确适用人群、场合、用途/);
});
