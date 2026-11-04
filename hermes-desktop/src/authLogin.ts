// Standard OAuth2 (currently enterprise_idp OAuth2) and custom enterprise_idpAES share platform sessions.
export type LoginMethod = "enterprise_idpAES" | "oauth2";
const POST_LOGIN_PATH = "hermes_platform_post_login_path";

export async function readLoginMethod(apiBase: string): Promise<LoginMethod> {
  const response = await fetch(`${apiBase}/auth/config`, { credentials: "include", cache: "no-store" });
  if (!response.ok) throw new Error("登录配置暂时不可用，请重试");
  const body = await response.json() as { login_method?: unknown };
  if (body.login_method !== "enterprise_idpAES" && body.login_method !== "oauth2") {
    throw new Error("当前登录方式不受支持，请检查部署配置");
  }
  return body.login_method;
}

export function validBusinessPath(raw: string): string | null {
  try {
    const decoded = decodeURIComponent(raw);
    if (!raw.startsWith("/") || decoded.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(decoded)) return null;
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin || !(url.pathname.startsWith("/user/") || url.pathname.startsWith("/admin/") || url.pathname === "/admin")) return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch { return null; }
}

export function oauth2LoginUrl(apiBase: string, rawPath: string): string {
  const path = validBusinessPath(rawPath) || "/user/chat";
  // Business tickets (WeCom and application authorization state) stay in this tab.
  // They never become OAuth2 state or pass to the external identity provider.
  window.sessionStorage.setItem(POST_LOGIN_PATH, path);
  const destination = new URL(path, window.location.origin);
  return `${apiBase}/auth/oauth2/login?return_to=${encodeURIComponent(destination.pathname)}`;
}

export function restoreBusinessPath(serverPath: string): string {
  const safeServer = validBusinessPath(serverPath) || "/user/chat";
  const stored = validBusinessPath(window.sessionStorage.getItem(POST_LOGIN_PATH) || "");
  window.sessionStorage.removeItem(POST_LOGIN_PATH);
  return stored && new URL(stored, window.location.origin).pathname === new URL(safeServer, window.location.origin).pathname
    ? stored : safeServer;
}

export const authErrorMessages: Record<string, string> = {
  login_config_unavailable: "登录配置暂时不可用，请重试。",
  oauth2_not_selected: "当前部署未选择 OAuth2，请重新发起登录。",
  oauth2_invalid_request: "登录请求无效，请重新发起登录。",
  oauth2_transaction_invalid: "登录事务已过期或已使用，请重新登录。",
  oauth2_access_denied: "登录授权未完成，请重试。",
  oauth2_upstream_failed: "认证服务暂时不可用，请重新登录。",
  oauth2_identity_invalid: "企业身份或可信工号无效，请联系管理员。",
  oauth2_store_unavailable: "认证会话存储暂时不可用，请稍后重试。",
};
