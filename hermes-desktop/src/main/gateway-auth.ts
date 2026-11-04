// Gateway admission precedes the platform's OAuth2 login. An HTML challenge
// must never be mounted at the API URL: its relative scripts/images break.
export async function isLegacySsoGatewayPage(response: Response): Promise<boolean> {
  if (!response.headers.get("content-type")?.toLowerCase().includes("text/html")) return false;
  const body = await response.clone().text();
  return body.length <= 128 * 1024 && body.includes("/public/manifest.js")
    && body.includes("old/shortcut_main.js") && /<title>\s*Loading\.\.\.\s*<\/title>/i.test(body);
}

export async function isPlatformLoginConfiguration(response: Response): Promise<boolean> {
  if (!response.ok || !response.headers.get("content-type")?.toLowerCase().includes("application/json")) return false;
  try {
    const body = await response.clone().json() as { login_method?: unknown };
    return body.login_method === "oauth2" || body.login_method === "enterprise_idpAES";
  } catch { return false; }
}

export function allowedGatewayNavigation(raw: string, platformOrigin: string): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && !url.username && !url.password
      && [platformOrigin, "https://gateway.example.com:6440", "https://login.example.com",
        "https://identity.example.com"].includes(url.origin);
  } catch { return false; }
}

export function transferableGatewayCookie(name: string): boolean {
  // Platform sessions and one-use OAuth2 transaction/completion cookies stay
  // in their own partition; gateway admission alone is shared.
  return !name.toLowerCase().startsWith("hermes_");
}

export async function recoverGatewayConfiguration(
  requestUrl: string, method: string, platformOrigin: string, response: Response,
  authenticate: () => Promise<void>, retry: () => Promise<Response>,
): Promise<Response> {
  const url = new URL(requestUrl);
  if (method !== "GET" || url.origin !== platformOrigin || url.pathname !== "/api/v1/auth/config"
    || !await isLegacySsoGatewayPage(response)) return response;
  await authenticate();
  const recovered = await retry();
  if (!await isPlatformLoginConfiguration(recovered)) throw new Error("网关认证尚未完成，请重新登录");
  return recovered;
}
