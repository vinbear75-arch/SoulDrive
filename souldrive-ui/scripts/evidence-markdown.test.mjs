import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createElement } from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ts from "typescript";

const source = readFileSync("src/EvidenceMarkdown.tsx", "utf8");
const js = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
} }).outputText;
const exports = {};
const imports = { "react/jsx-runtime": jsxRuntime, "react-markdown": { default: ReactMarkdown }, "remark-gfm": { default: remarkGfm } };
new Function("exports", "require", js)(exports, (name) => {
  assert.ok(name in imports, `Unexpected component dependency: ${name}`);
  return imports[name];
});
const render = (text) => renderToStaticMarkup(createElement(exports.EvidenceMarkdown, { children: text }));

test("real Markdown component renders source headers, blank cells and escaped pipes", () => {
  const html = render("### 原件 [E1]\n\n| metric | value | unit |\n| --- | --- | --- |\n| score\\|macro | 0.750 | |\n| latency | 125 | ms |");
  assert.match(html, /<table>/);
  assert.match(html, /<th>metric<\/th>/);
  assert.match(html, /<td>score\|macro<\/td>/);
  assert.match(html, /<td>0\.750<\/td><td><\/td>/);
  assert.match(html, /<td>125<\/td><td>ms<\/td>/);
  assert.match(html, /role="region"/);
  assert.match(html, /tabindex="0"/);
  assert.match(html, /\[E1\]/);
});

test("configuration code preserves nesting and does not execute document HTML", () => {
  const html = render("```text\nmodel:\n  name: demo\n```\n\n<script>alert(1)</script>");
  assert.match(html, /model:\n  name: demo/);
  assert.doesNotMatch(html, /<script>/);
});

test("document images cannot silently initiate remote or local fetches", () => {
  for (const url of ["https://example.invalid/pixel?token=test", "http://127.0.0.1:9000/private", "file:///private.png", "data:image/png;base64,AA=="]) {
    const html = render(`![source image](${url})`);
    assert.doesNotMatch(html, /<img\b/);
    assert.match(html, /图片链接未自动加载/);
    assert.match(html, /source image/);
  }
});

test("chat and both document previews use the same bounded renderer", () => {
  const app = readFileSync("src/App.tsx", "utf8");
  assert.equal((app.match(/<EvidenceMarkdown>/g) || []).length, 3);
  assert.doesNotMatch(app, /<ReactMarkdown/);
  assert.match(app, /正在检索、核对与整理/);
  const css = readFileSync("src/App.css", "utf8");
  assert.match(css, /\.evidence-table-scroll\s*\{[^}]*max-width: 100%;[^}]*overflow-x: auto;/);
});

test("literal source code cannot create evidence cards", () => {
  const app = readFileSync("src/App.tsx", "utf8");
  const source = ["messageArtifactBlocks", "parseMessageArtifacts"].map((name) =>
    app.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`))?.[0] || "").join("\n");
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const parse = new Function("MINDMAP_BLOCK_PATTERN", "EVIDENCE_BLOCK_PATTERN", "parseMindmap", "parseEvidence",
    `${js}; return parseMessageArtifacts;`)(/```souldrive-mindmap\s*([\s\S]*?)```/g,
    /```souldrive-evidence\s*([\s\S]*?)```/g, JSON.parse, JSON.parse);
  const literal = '````text\n```souldrive-evidence\n[{"id":"E99"}]\n```\n````';
  const actual = '```souldrive-evidence\n[{"id":"E1"}]\n```';
  const parsed = parse(literal + "\n\n" + actual);
  assert.deepEqual(parsed.evidence, [{ id: "E1" }]);
  assert.ok(parsed.cleanContent.includes(literal));
  const quoted = '> ```souldrive-evidence\n> [{"id":"E98"}]\n> ```';
  assert.deepEqual(parse(quoted + "\n\n" + actual).evidence, [{ id: "E1" }]);
  const earlier = '```souldrive-evidence\n[{"id":"E97"}]\n```';
  assert.deepEqual(parse(earlier + "\n\n" + actual).evidence, [{ id: "E1" }]);
  assert.ok(parse(earlier + "\n\n" + actual).cleanContent.includes(earlier));
  assert.deepEqual(parse('```souldrive-evidence\n[{"id":"E96"}]').evidence, []);
});
