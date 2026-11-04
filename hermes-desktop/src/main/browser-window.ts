import { shell } from "electron";

function validUrl(raw: unknown): URL {
  if (typeof raw !== "string") throw new Error("浏览器地址无效");
  const url = new URL(raw.trim());
  if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("只允许 HTTP 或 HTTPS 地址");
  return url;
}

/** Open a URL in the operating system's configured default browser. */
export async function openBrowser(rawUrl: unknown): Promise<boolean> {
  const url = validUrl(rawUrl);
  await shell.openExternal(url.toString());
  return true;
}
