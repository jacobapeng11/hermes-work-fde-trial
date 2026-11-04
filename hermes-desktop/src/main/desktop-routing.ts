import { resolve, sep } from "node:path";

export const DEFAULT_SERVER_ORIGIN = "https://app.example.com";

export function serverOrigin(raw = DEFAULT_SERVER_ORIGIN): string {
  const url = new URL(raw);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("COWORK_SERVER_ORIGIN must be an HTTPS origin without a path or credentials");
  }
  return url.origin;
}

// Only the document routes owned by the copied User Web are local.
// API callbacks, Runtime Edge, attachments and Office preview stay remote.
export function isLocalDocument(pathname: string): boolean {
  return pathname === "/" || pathname === "/index.html" || pathname === "/user"
    || ["/auth/error", "/auth/complete", "/auth/desktop-complete"].includes(pathname)
    || (pathname.startsWith("/user/") && !pathname.startsWith("/user/assets/") && !pathname.startsWith("/user/fonts/"));
}

export function localAssetPath(pathname: string, rendererRoot: string): string | null {
  let decoded: string;
  try { decoded = decodeURIComponent(pathname); } catch { return null; }
  if (decoded.includes("\\") || decoded.includes("\0")) return null;
  // The packaged HTML uses relative asset URLs (./assets/...). When it is
  // served at /user/chat, Chromium resolves those to /user/assets/...
  const assetPath = decoded.startsWith("/user/assets/") || decoded.startsWith("/user/fonts/")
    ? decoded.slice("/user".length)
    : decoded;
  if (!assetPath.startsWith("/assets/") && !assetPath.startsWith("/fonts/")) return null;
  const root = resolve(rendererRoot);
  const target = resolve(root, `.${assetPath}`);
  return target.startsWith(root + sep) ? target : null;
}
