import test from "node:test";
import assert from "node:assert/strict";

// utils.js reads window.t at module scope, so the global must exist before import.
globalThis.window = {};

const { escapeHtml, safePath } =
  await import("../../../../src/astinuslab/web/static/volatility/utils.js");

test("escapeHtml neutralises the characters that end an attribute", () => {
  // Several call sites interpolate into href="...", data-job="...", title="...".
  // A bare double quote there closes the attribute and the remainder becomes markup.
  assert.equal(escapeHtml('" onmouseover="alert(1)'), "&quot; onmouseover=&quot;alert(1)");
  assert.equal(escapeHtml("' onfocus='alert(1)"), "&#39; onfocus=&#39;alert(1)");
  assert.equal(escapeHtml("<script>"), "&lt;script&gt;");
  assert.equal(escapeHtml("a & b"), "a &amp; b");
});

test("escapeHtml escapes the ampersand before the entities it introduces", () => {
  // & must be replaced first or "&quot;" would itself be re-escaped.
  assert.equal(escapeHtml('&"'), "&amp;&quot;");
});

test("escapeHtml renders null and undefined as empty, not as the word", () => {
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
});

test("safePath accepts the server-generated absolute paths and nothing else", () => {
  assert.equal(safePath("/jobs/abc123"), "/jobs/abc123");
  assert.equal(safePath("/jobs/abc?tab=vol3"), "/jobs/abc?tab=vol3");
});

test("safePath refuses scheme and protocol-relative URLs", () => {
  for (const hostile of [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.example/jobs/1",
    "https://evil.example/jobs/1",
    "jobs/relative",
    "",
    null,
    undefined,
  ]) {
    assert.equal(safePath(hostile), "", `expected refusal for ${String(hostile)}`);
  }
});
