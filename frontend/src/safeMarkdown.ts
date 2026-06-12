// Hardened markdown rendering for operator-supplied content-pack nuggets (P0-8).
//
// Nugget bodies are rendered with dangerouslySetInnerHTML, so a malicious or
// garbled content pack could otherwise execute script in every attendee's
// browser. DOMPurify is the usual tool, but it cannot be installed/built in
// the current environment; instead we harden `marked` itself, which is the
// complete XSS vector set for a markdown -> HTML pipeline we fully control:
//
//   1. Raw HTML passthrough (<script>, <img onerror>, <svg onload>, …) — the
//      renderer drops all raw HTML, so it can never reach the DOM as markup.
//   2. Dangerous URL schemes in links/images (javascript:, data:, vbscript:) —
//      href/src are scheme-checked against a strict allowlist.
//
// Markdown formatting (bold, lists, code, headings, safe links/images) is
// preserved. This module is pure and exported so it can be unit-checked
// without a browser.
import { Marked } from "marked";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Allow only http(s) and mailto absolute URLs; relative/anchor links (no
// scheme) are also allowed. Anything with another scheme is rejected.
export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  const trimmed = href.trim();
  const hasScheme = /^[a-z][a-z0-9+.-]*:/i.test(trimmed);
  if (hasScheme && !/^(https?:|mailto:)/i.test(trimmed)) return null;
  return trimmed;
}

const safeMarked = new Marked({ gfm: true, breaks: true });

safeMarked.use({
  renderer: {
    // Strip raw HTML blocks and inline HTML entirely.
    html(): string {
      return "";
    },
    link(token: { href: string; title?: string | null; tokens: unknown[] }): string {
      // `this` is the renderer; parseInline renders the visible link text.
      // @ts-expect-error marked binds `this.parser` on the renderer at runtime
      const text = this.parser.parseInline(token.tokens);
      const safe = safeHref(token.href);
      if (!safe) return text; // drop the link, keep its text
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer nofollow"${title}>${text}</a>`;
    },
    image(token: { href: string; title?: string | null; text: string }): string {
      const safe = safeHref(token.href);
      const alt = escapeHtml(token.text ?? "");
      if (!safe) return alt; // drop the image, keep its alt text
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<img src="${escapeHtml(safe)}" alt="${alt}"${title} />`;
    },
  },
});

/** Render content-pack markdown to HTML safe to inject (no script, no unsafe URLs). */
export function renderNuggetMarkdown(markdown: string): string {
  return safeMarked.parse(markdown ?? "", { async: false }) as string;
}
