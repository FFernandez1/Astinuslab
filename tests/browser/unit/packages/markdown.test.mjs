import test from "node:test";
import assert from "node:assert/strict";

const { renderMarkdown, sanitizeMarkdown } =
  await import("../../../../src/astinuslab/web/static/packages/render-markdown.js");

test("passive Markdown escapes active HTML and unsafe links", () => {
  const source =
    "<script>alert(1)</script>\n\n[run](javascript:alert(1))\n\n![x](data:text/html,evil)";
  const html = renderMarkdown(source);

  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|onerror|javascript:|data:text/);
  assert.match(html, /run/);
});

test("sanitizeMarkdown removes control characters without changing plain content", () => {
  assert.equal(sanitizeMarkdown("title\u0000\nbody"), "title\nbody");
  assert.match(renderMarkdown("# Title\n\n- one\n- two"), /<h2>Title<\/h2>/);
});

test("images stay passive and links retain URL underscores", () => {
  const html = renderMarkdown(
    "![evidence](https://example.com/evidence.png) [case](https://example.com/case_file)",
  );

  assert.match(html, /evidence/);
  assert.doesNotMatch(html, /<img|!\[|evidence\.png/);
  assert.match(html, /href="https:\/\/example\.com\/case_file"/);
  assert.doesNotMatch(html, /case<em>file|case_file<\/em>/);
});

test("emphasis does not rewrite snake_case words", () => {
  const html = renderMarkdown("snake_case_word **bold** *italic* _underlined_");

  assert.match(html, /snake_case_word/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<em>italic<\/em>/);
  assert.match(html, /<em>underlined<\/em>/);
  assert.doesNotMatch(html, /snake<em>/);
});
