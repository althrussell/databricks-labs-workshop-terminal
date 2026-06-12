// Run: node --test src/safeMarkdown.test.ts   (Node strips TS types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import { renderNuggetMarkdown, safeHref } from "./safeMarkdown.ts";

test("strips raw <script> HTML", () => {
  const html = renderNuggetMarkdown("Hello <script>alert(1)</script> world");
  assert.ok(!/<script/i.test(html), html);
});

test("strips event-handler image injection", () => {
  const html = renderNuggetMarkdown('<img src=x onerror="alert(1)">');
  assert.ok(!/onerror/i.test(html), html);
  assert.ok(!/<img[^>]*onerror/i.test(html), html);
});

test("drops javascript: link href but keeps text", () => {
  const html = renderNuggetMarkdown("[click me](javascript:alert(1))");
  assert.ok(!/javascript:/i.test(html), html);
  assert.ok(/click me/.test(html), html);
});

test("drops data: image src", () => {
  const html = renderNuggetMarkdown("![x](data:text/html;base64,PHN2Zz4=)");
  assert.ok(!/data:/i.test(html), html);
});

test("keeps safe https links with noopener", () => {
  const html = renderNuggetMarkdown("[docs](https://example.com)");
  assert.ok(/href="https:\/\/example\.com"/.test(html), html);
  assert.ok(/rel="noopener noreferrer nofollow"/.test(html), html);
});

test("preserves ordinary markdown formatting", () => {
  const html = renderNuggetMarkdown("**bold** and `code` and\n\n- a\n- b");
  assert.ok(/<strong>bold<\/strong>/.test(html), html);
  assert.ok(/<code>code<\/code>/.test(html), html);
  assert.ok(/<li>a<\/li>/.test(html), html);
});

test("safeHref allowlist", () => {
  assert.equal(safeHref("https://x.com"), "https://x.com");
  assert.equal(safeHref("/relative"), "/relative");
  assert.equal(safeHref("mailto:a@b.com"), "mailto:a@b.com");
  assert.equal(safeHref("javascript:alert(1)"), null);
  assert.equal(safeHref("data:text/html,x"), null);
  assert.equal(safeHref(" vbscript:x"), null);
});
