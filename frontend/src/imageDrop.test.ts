import assert from "node:assert/strict";
import test from "node:test";
import { ATTACHABLE_ACCEPT, dragCarriesFiles, rejectedMessage, splitAttachable } from "./imageDrop.ts";

const file = (type: string) => ({ type });

test("only the image types the server stores are attachable", () => {
  const { images, rejected } = splitAttachable([
    file("image/png"), file("image/jpeg"), file("image/webp"), file("image/gif"),
    file("image/heic"), file("image/svg+xml"), file("application/pdf"), file(""),
  ]);
  assert.deepEqual(images.map((f) => f.type), ["image/png", "image/jpeg", "image/webp", "image/gif"]);
  assert.equal(rejected, 4);
  assert.equal(ATTACHABLE_ACCEPT, "image/png,image/jpeg,image/webp,image/gif");
});

test("a rejected drop says why, and what was still attached", () => {
  assert.equal(rejectedMessage(1, 0), "");
  assert.match(rejectedMessage(0, 1), /^That file isn't an image the agent can read: use PNG, JPG, WebP or GIF\.$/);
  assert.match(rejectedMessage(2, 3), /^3 files aren't .* The 2 images were attached\.$/);
  assert.match(rejectedMessage(1, 1), /The image was attached\.$/);
});

test("only file drags light up the drop zone", () => {
  assert.equal(dragCarriesFiles(["Files"]), true);
  assert.equal(dragCarriesFiles(["text/plain", "Files"]), true);
  assert.equal(dragCarriesFiles(["text/plain", "text/html"]), false);
  assert.equal(dragCarriesFiles(undefined), false);
});
