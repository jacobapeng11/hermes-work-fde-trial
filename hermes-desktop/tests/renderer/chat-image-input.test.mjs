import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_IMAGE_ATTACHMENT_BYTES,
  acceptAttachmentFiles,
  normalizePastedImage
} from "../../src/pages/chatImageInput.ts";

function imageFile({
  name = "screenshot.png",
  type = "image/png",
  size = 8
} = {}) {
  return new File([new Uint8Array(size)], name, { type });
}

test("chat image input accepts supported images and non-image attachments", () => {
  const image = imageFile({ name: "capture.png", type: "image/png" });
  const documentFile = new File(["text"], "note.txt", { type: "text/plain" });

  const result = acceptAttachmentFiles([documentFile, image], true);

  assert.deepEqual(result.errors, []);
  assert.equal(result.accepted.length, 2);
  assert.equal(result.accepted[0], documentFile);
  assert.equal(result.accepted[1], image);
});

test("chat image input rejects unsupported models, MIME types and oversized images", () => {
  const unsupportedModel = acceptAttachmentFiles([imageFile()], false);
  const unsupportedMime = acceptAttachmentFiles(
    [imageFile({ name: "capture.gif", type: "image/gif" })],
    true
  );
  const oversized = acceptAttachmentFiles(
    [imageFile({ size: MAX_IMAGE_ATTACHMENT_BYTES + 1 })],
    true
  );

  assert.deepEqual(unsupportedModel, {
    accepted: [],
    errors: ["当前模型不支持图片识别，请切换模型后重试"]
  });
  assert.deepEqual(unsupportedMime, {
    accepted: [],
    errors: ["仅支持粘贴 PNG、JPEG 或 WebP 图片"]
  });
  assert.deepEqual(oversized, {
    accepted: [],
    errors: ["单张图片不能超过 20 MB"]
  });
});

test("pasted clipboard images receive stable display names", () => {
  const png = normalizePastedImage(imageFile({ name: "image.png" }), 2);
  const jpeg = normalizePastedImage(
    imageFile({ name: "image.png", type: "image/jpeg" }),
    3
  );

  assert.match(png.name, /^pasted-image-\d{8}\d{6}-2\.png$/);
  assert.equal(png.type, "image/png");
  assert.match(jpeg.name, /^pasted-image-\d{8}\d{6}-3\.jpg$/);
  assert.equal(jpeg.type, "image/jpeg");
});
