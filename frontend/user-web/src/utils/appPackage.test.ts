import { describe, expect, it } from "vitest";

import { readZipTextEntry } from "./appPackage";

function storedZip(name: string, content: string): Uint8Array<ArrayBuffer> {
  const nameBytes = new TextEncoder().encode(name);
  const contentBytes = new TextEncoder().encode(content);
  const localSize = 30 + nameBytes.length + contentBytes.length;
  const centralSize = 46 + nameBytes.length;
  const output = new Uint8Array(localSize + centralSize + 22);
  const view = new DataView(output.buffer);
  const setHeader = (offset: number, signature: number, central = false) => {
    view.setUint32(offset, signature, true);
    view.setUint16(offset + 4, 20, true);
    if (central) view.setUint16(offset + 6, 20, true);
    view.setUint16(offset + 8, 0, true);
    view.setUint16(offset + 10, 0, true);
    view.setUint16(offset + 12, 0, true);
    view.setUint16(offset + 14, 0, true);
    view.setUint32(offset + 16, 0, true);
    view.setUint32(offset + 20, contentBytes.length, true);
    view.setUint32(offset + 24, contentBytes.length, true);
    view.setUint16(offset + 28, nameBytes.length, true);
    if (central) {
      view.setUint16(offset + 30, 0, true);
      view.setUint16(offset + 32, 0, true);
      view.setUint16(offset + 34, 0, true);
      view.setUint16(offset + 36, 0, true);
      view.setUint32(offset + 38, 0, true);
      view.setUint32(offset + 42, 0, true);
    }
  };
  setHeader(0, 0x04034b50);
  output.set(nameBytes, 30);
  output.set(contentBytes, 30 + nameBytes.length);
  const centralOffset = localSize;
  setHeader(centralOffset, 0x02014b50, true);
  output.set(nameBytes, centralOffset + 46);
  const endOffset = centralOffset + centralSize;
  view.setUint32(endOffset, 0x06054b50, true);
  view.setUint16(endOffset + 8, 1, true);
  view.setUint16(endOffset + 10, 1, true);
  view.setUint32(endOffset + 12, centralSize, true);
  view.setUint32(endOffset + 16, centralOffset, true);
  return output;
}

describe("readZipTextEntry", () => {
  it("reads the root index entry from a stored zip", async () => {
    const file = new File(
      [storedZip("index.html", "<!doctype html><html><body>zip preview</body></html>")],
      "app.zip",
      { type: "application/zip" }
    );

    await expect(readZipTextEntry(file)).resolves.toBe(
      "<!doctype html><html><body>zip preview</body></html>"
    );
  });
});
