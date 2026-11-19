import assert from "node:assert/strict";
import test from "node:test";
import { phoneUrl, qrPath } from "./qr.ts";

test("the phone opens the app's home on the same origin", () => {
  assert.equal(phoneUrl({ origin: "https://wt-seat-12.aws.databricksapps.com" }), "https://wt-seat-12.aws.databricksapps.com/");
});

test("a long app URL encodes as one square path with finder patterns", () => {
  const { size, d } = qrPath("https://workshop-terminal-seat-42-1234567890123456.aws.databricksapps.com/");
  assert.ok(size >= 21 && (size - 17) % 4 === 0, `QR sizes are 17 + 4n, got ${size}`);
  // The top-left finder pattern's first row is seven dark modules.
  assert.ok(d.startsWith("M0 0h7v1h-7z"), d.slice(0, 40));
  assert.equal(qrPath("https://example.com/").d, qrPath("https://example.com/").d);
});
