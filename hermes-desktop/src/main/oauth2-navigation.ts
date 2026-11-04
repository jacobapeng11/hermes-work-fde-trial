export function parseOAuth2Origins(raw: string): string[] {
  const values: unknown = JSON.parse(raw || "[]");
  if (!Array.isArray(values) || values.length > 32) throw new Error("OAuth2 桌面域名配置无效");
  return values.map((value: unknown) => {
    if (typeof value !== "string") throw new Error("OAuth2 桌面域名配置无效");
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/" || url.hostname.includes("*")) {
      throw new Error("OAuth2 桌面域名必须是精确的 HTTPS Origin");
    }
    return url.origin;
  });
}

export function allowedOAuth2Navigation(raw: string, platformOrigin: string, origins: string[]): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && !url.username && !url.password
      && (url.origin === platformOrigin || origins.includes(url.origin));
  } catch { return false; }
}

export function isOAuth2Login(raw: string, platformOrigin: string): boolean {
  try {
    const url = new URL(raw);
    return url.origin === platformOrigin && url.pathname === "/api/v1/auth/oauth2/login";
  } catch { return false; }
}
