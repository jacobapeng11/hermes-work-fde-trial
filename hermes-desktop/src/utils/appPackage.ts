const MAX_ZIP_TEXT_ENTRY_BYTES = 16 * 1024 * 1024;

interface ZipEntry {
  name: string;
  flags: number;
  compressionMethod: number;
  compressedSize: number;
  uncompressedSize: number;
  localHeaderOffset: number;
}

export async function readZipTextEntry(
  file: Blob,
  wantedName = "index.html",
  maxBytes = MAX_ZIP_TEXT_ENTRY_BYTES
): Promise<string> {
  const entry = (await zipEntries(file)).find((item) => item.name === wantedName);
  if (!entry) throw new Error(`ZIP 包中未找到 ${wantedName}`);
  if (entry.flags & 0x1) throw new Error("ZIP 条目不支持加密内容");
  if (entry.uncompressedSize > maxBytes) throw new Error("ZIP 内 HTML 内容超过 16 MB");

  const localHeader = new DataView(
    await sliceArrayBuffer(file, entry.localHeaderOffset, entry.localHeaderOffset + 30)
  );
  if (localHeader.getUint32(0, true) !== 0x04034b50) {
    throw new Error("ZIP 包结构无效");
  }
  const localNameLength = localHeader.getUint16(26, true);
  const localExtraLength = localHeader.getUint16(28, true);
  const dataStart = entry.localHeaderOffset + 30 + localNameLength + localExtraLength;
  const compressed = file.slice(dataStart, dataStart + entry.compressedSize);

  if (entry.compressionMethod === 0) {
    const text = new TextDecoder().decode(await sliceArrayBuffer(compressed, 0, compressed.size));
    if (new TextEncoder().encode(text).length > maxBytes) {
      throw new Error("ZIP 内 HTML 内容超过 16 MB");
    }
    return text;
  }
  if (entry.compressionMethod !== 8) throw new Error("ZIP 条目使用不支持的压缩格式");

  const DecompressionStreamCtor = (
    globalThis as {
      DecompressionStream?: new (format: "deflate-raw") => TransformStream<Uint8Array, Uint8Array>;
    }
  ).DecompressionStream;
  if (!DecompressionStreamCtor) throw new Error("当前浏览器不支持读取该 ZIP 包");
  const text = await new Response(
    compressed.slice().stream().pipeThrough(new DecompressionStreamCtor("deflate-raw"))
  ).text();
  if (new TextEncoder().encode(text).length > maxBytes) {
    throw new Error("ZIP 内 HTML 内容超过 16 MB");
  }
  return text;
}

async function zipEntries(file: Blob): Promise<ZipEntry[]> {
  const tailStart = Math.max(0, file.size - 66_000);
  const tail = new Uint8Array(await sliceArrayBuffer(file, tailStart, file.size));
  let endRecord = -1;
  for (let index = tail.length - 22; index >= 0; index -= 1) {
    if (
      tail[index] === 0x50
      && tail[index + 1] === 0x4b
      && tail[index + 2] === 0x05
      && tail[index + 3] === 0x06
    ) {
      endRecord = index;
      break;
    }
  }
  if (endRecord < 0) throw new Error("ZIP 包结构无效");
  const view = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
  const entryCount = view.getUint16(endRecord + 10, true);
  let offset = view.getUint32(endRecord + 16, true);
  const entries: ZipEntry[] = [];
  for (let index = 0; index < entryCount; index += 1) {
    const header = new DataView(await sliceArrayBuffer(file, offset, offset + 46));
    if (header.getUint32(0, true) !== 0x02014b50) throw new Error("ZIP 包结构无效");
    const flags = header.getUint16(8, true);
    const compressionMethod = header.getUint16(10, true);
    const compressedSize = header.getUint32(20, true);
    const uncompressedSize = header.getUint32(24, true);
    const nameLength = header.getUint16(28, true);
    const extraLength = header.getUint16(30, true);
    const commentLength = header.getUint16(32, true);
    const localHeaderOffset = header.getUint32(42, true);
    const name = new TextDecoder().decode(
      await sliceArrayBuffer(file, offset + 46, offset + 46 + nameLength)
    );
    const normalizedName = name.replace(/\\/g, "/").replace(/^\.\//, "");
    if (
      normalizedName.startsWith("/")
      || normalizedName.startsWith("../")
      || normalizedName.includes("/../")
      || normalizedName === ".."
    ) {
      throw new Error("ZIP 包包含不安全的路径");
    }
    entries.push({
      name: normalizedName,
      flags,
      compressionMethod,
      compressedSize,
      uncompressedSize,
      localHeaderOffset
    });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

async function sliceArrayBuffer(
  blob: Blob,
  start: number,
  end: number
): Promise<ArrayBuffer> {
  const slice = blob.slice(start, end);
  if (typeof slice.arrayBuffer === "function") return slice.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      resolve(reader.result as ArrayBuffer);
    });
    reader.addEventListener("error", () => reject(reader.error));
    reader.readAsArrayBuffer(slice);
  });
}
