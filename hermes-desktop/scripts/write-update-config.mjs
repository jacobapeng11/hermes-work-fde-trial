import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

const rawUrl = (process.env.COWORK_DESKTOP_UPDATE_URL ?? "").trim();
let updateUrl = "";
if (rawUrl) {
  const parsed = new URL(rawUrl);
  if (parsed.protocol !== "https:" && parsed.hostname !== "localhost" && parsed.hostname !== "127.0.0.1") {
    throw new Error("COWORK_DESKTOP_UPDATE_URL 必须使用 HTTPS（本地测试可使用 localhost）");
  }
  updateUrl = parsed.toString().replace(/\/$/, "");
}

const output = resolve("build/update-config.json");
await mkdir(dirname(output), { recursive: true });
await writeFile(output, `${JSON.stringify({ updateUrl }, null, 2)}\n`, "utf8");
// electron-builder's extraResources requires build/python to exist on every
// packaging platform. The Windows build script fills it with the embeddable
// CPython; elsewhere this stages an empty directory (macOS relies on the
// system python3 — the builtin is Windows-only at runtime).
const builtinPython = resolve("build/python");
await mkdir(builtinPython, { recursive: true });
console.log(updateUrl ? `desktop update source: ${updateUrl}` : "desktop update source: disabled");
