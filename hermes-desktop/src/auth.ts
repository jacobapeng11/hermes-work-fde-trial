import type { AuthPrincipalSummary, AuthTokenResponse } from "@hermes-cloud/schemas";
import { camelize } from "./api/types";
import { oauth2LoginUrl, readLoginMethod, restoreBusinessPath } from "./authLogin";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const authMode = import.meta.env.VITE_AUTH_MODE || "enterprise_idpAES";
if (authMode === "enterprise_idp") throw new Error("旧登录配置 enterprise_idp 已移除，请使用 enterprise_idpAES");
const LEGACY_SSO_LOGIN_BASE_URL = import.meta.env.VITE_LEGACY_SSO_LOGIN_BASE_URL;
const LEGACY_SSO_APP_NAME = import.meta.env.VITE_LEGACY_SSO_APP_NAME;
const USER_ENTRY_PATH = "/user/chat";
const ACCESS_TOKEN_STORAGE_KEY = "hermes_platform_access_token";
const ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY = "hermes_platform_access_token_expires_at";
const POST_LOGIN_PATH_STORAGE_KEY = "hermes_platform_post_login_path";
const REAUTHENTICATION_FLOW_STORAGE_KEY = "hermes_platform_reauthentication_flow";
const REAUTHENTICATION_FLOW_VERSION = 1;
const REAUTHENTICATION_FLOW_TTL_MS = 10 * 60_000;
const MAX_AUTOMATIC_REAUTHENTICATION_ATTEMPTS = 2;
let accessToken: string | null = null;
let accessTokenExpiresAt: number | null = null;
let principal: AuthPrincipalSummary | null = null;
let sessionEpoch = 0;
let completionPromise: Promise<void> | null = null;

type PlatformAuthTokenResponse = AuthTokenResponse & {
  expiresIn?: number | null;
};

type ReauthenticationFlow = {
  version: typeof REAUTHENTICATION_FLOW_VERSION;
  startedAt: number;
  automaticAttempts: number;
  mode: "automatic" | "manual";
};

let refreshPromise: Promise<PlatformAuthTokenResponse> | null = null;
// React StrictMode remounts the authentication gate in development.  Keep the
// one-time EnterpriseIdp callback exchange in this browser tab single-flight so the
// second mount cannot interpret the just-consumed callback as a missing session
// and start another enterprise-login redirect.
let enterprise_idpCallbackBootstrapPromise: Promise<AuthPrincipalSummary | null> | null = null;

export type AuthSessionState = "active" | "reauth_required" | "service_unavailable";
type AuthSessionListener = (state: AuthSessionState) => void;
const authSessionListeners = new Set<AuthSessionListener>();
let currentAuthSessionState: AuthSessionState = "active";

export function getLegacySsoLoginUrl(): string {
  if (!LEGACY_SSO_LOGIN_BASE_URL || !LEGACY_SSO_APP_NAME) {
    throw new Error(
      "Enterprise SSO 登录配置缺失：请设置 VITE_LEGACY_SSO_LOGIN_BASE_URL 和 VITE_LEGACY_SSO_APP_NAME。"
    );
  }
  const url = new URL(LEGACY_SSO_LOGIN_BASE_URL);
  url.searchParams.set("name", LEGACY_SSO_APP_NAME);
  return url.toString();
}

export class AuthRefreshError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly reauthRequired: boolean
  ) {
    super(message);
    this.name = "AuthRefreshError";
  }
}

export function isDevAuthMode(): boolean { return authMode === "dev"; }

export function getAccessToken(): string | null {
  return accessToken ?? window.sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
}

export function getPrincipal(): AuthPrincipalSummary | null {
  return principal;
}

export function getAccessTokenExpiresAt(): number | null {
  if (accessTokenExpiresAt !== null) return accessTokenExpiresAt;
  const stored = Number(
    window.sessionStorage.getItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY)
  );
  return Number.isFinite(stored) && stored > 0 ? stored : null;
}

export function subscribeAuthSession(listener: AuthSessionListener): () => void {
  authSessionListeners.add(listener);
  return () => authSessionListeners.delete(listener);
}

function publishAuthSession(state: AuthSessionState): void {
  currentAuthSessionState = state;
  for (const listener of authSessionListeners) listener(state);
}

export function requireReauthentication(): void {
  clearSession();
  publishAuthSession("reauth_required");
}

export function markAuthSessionActive(): void {
  if (currentAuthSessionState !== "reauth_required") {
    publishAuthSession("active");
  }
}

function tokenExpiration(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const parsed = JSON.parse(window.atob(normalized.padEnd(
      normalized.length + ((4 - normalized.length % 4) % 4),
      "="
    )));
    return typeof parsed.exp === "number" ? parsed.exp * 1000 : null;
  } catch {
    return null;
  }
}

export function setSession(session: PlatformAuthTokenResponse): void {
  accessToken = session.accessToken;
  accessTokenExpiresAt = session.expiresIn
    ? Date.now() + session.expiresIn * 1000
    : tokenExpiration(session.accessToken);
  principal = session.principal;
  window.sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, session.accessToken);
  if (accessTokenExpiresAt !== null) {
    window.sessionStorage.setItem(
      ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY,
      String(accessTokenExpiresAt)
    );
  } else {
    window.sessionStorage.removeItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY);
  }
  clearReauthenticationFlow();
  publishAuthSession("active");
}

export function clearSession(): void {
  sessionEpoch++;
  refreshPromise = null;
  accessToken = null;
  accessTokenExpiresAt = null;
  principal = null;
  window.sessionStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  window.sessionStorage.removeItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY);
}

function readReauthenticationFlow(): ReauthenticationFlow | null {
  const raw = window.sessionStorage.getItem(REAUTHENTICATION_FLOW_STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<ReauthenticationFlow>;
    const now = Date.now();
    if (
      parsed.version !== REAUTHENTICATION_FLOW_VERSION
      || typeof parsed.startedAt !== "number"
      || !Number.isFinite(parsed.startedAt)
      || parsed.startedAt > now
      || now - parsed.startedAt > REAUTHENTICATION_FLOW_TTL_MS
      || typeof parsed.automaticAttempts !== "number"
      || !Number.isInteger(parsed.automaticAttempts)
      || parsed.automaticAttempts < 0
      || parsed.automaticAttempts > MAX_AUTOMATIC_REAUTHENTICATION_ATTEMPTS
      || (parsed.mode !== "automatic" && parsed.mode !== "manual")
    ) {
      clearReauthenticationFlow();
      return null;
    }
    return parsed as ReauthenticationFlow;
  } catch {
    clearReauthenticationFlow();
    return null;
  }
}

function writeReauthenticationFlow(flow: ReauthenticationFlow): void {
  window.sessionStorage.setItem(
    REAUTHENTICATION_FLOW_STORAGE_KEY,
    JSON.stringify(flow)
  );
}

export function hasPendingReauthentication(): boolean {
  return readReauthenticationFlow() !== null;
}

export function hasAdminPostLoginIntent(): boolean {
  const path = window.sessionStorage.getItem(POST_LOGIN_PATH_STORAGE_KEY);
  if (!path) return false;
  try {
    const target = new URL(path, window.location.origin);
    return target.origin === window.location.origin
      && (target.pathname === "/admin" || target.pathname.startsWith("/admin/"));
  } catch {
    return false;
  }
}

export function reserveAutomaticReauthenticationAttempt(returnTo?: string): boolean {
  const current = readReauthenticationFlow();
  if (
    current?.mode === "manual"
    || (current?.automaticAttempts ?? 0) >= MAX_AUTOMATIC_REAUTHENTICATION_ATTEMPTS
  ) {
    return false;
  }
  if (!current) rememberPostLoginPath(returnTo);
  writeReauthenticationFlow({
    version: REAUTHENTICATION_FLOW_VERSION,
    startedAt: current?.startedAt ?? Date.now(),
    automaticAttempts: (current?.automaticAttempts ?? 0) + 1,
    mode: "automatic"
  });
  return true;
}

export function markReauthenticationManual(returnTo?: string): void {
  const current = readReauthenticationFlow();
  if (!current) rememberPostLoginPath(returnTo);
  writeReauthenticationFlow({
    version: REAUTHENTICATION_FLOW_VERSION,
    startedAt: current?.startedAt ?? Date.now(),
    automaticAttempts: current?.automaticAttempts ?? MAX_AUTOMATIC_REAUTHENTICATION_ATTEMPTS,
    mode: "manual"
  });
}

export function clearReauthenticationFlow(): void {
  window.sessionStorage.removeItem(REAUTHENTICATION_FLOW_STORAGE_KEY);
}

function refreshErrorDetail(body: unknown): string {
  if (!body || typeof body !== "object") return "";
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    const code = (detail as { code?: unknown }).code;
    if (typeof message === "string" && message) return message;
    if (typeof code === "string" && code) return code;
  }
  return "";
}

export async function refreshAccessToken(): Promise<PlatformAuthTokenResponse> {
  if (refreshPromise) return refreshPromise;
  const epoch = sessionEpoch;
  const pending = (async () => {
    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "X-Hermes-Requested-With": "user-web" }
      });
    } catch {
      if (epoch !== sessionEpoch) throw new AuthRefreshError("认证状态已变化，请重试", 409, false);
      publishAuthSession("service_unavailable");
      throw new AuthRefreshError("认证服务暂时不可用，正在重试", 0, false);
    }
    if (epoch !== sessionEpoch) throw new AuthRefreshError("认证状态已变化，请重试", 409, false);
    if (!response.ok) {
      let detail = "";
      try {
        detail = refreshErrorDetail(await response.json());
      } catch {
        // Keep the stable user-facing fallback for malformed proxy errors.
      }
      const reauthRequired = response.status === 401;
      if (reauthRequired) {
        requireReauthentication();
      } else {
        publishAuthSession("service_unavailable");
      }
      throw new AuthRefreshError(
        detail || (reauthRequired ? "认证已失效，请重新认证" : "认证服务暂时不可用，正在重试"),
        response.status,
        reauthRequired
      );
    }
    const session = camelize<PlatformAuthTokenResponse>(await response.json());
    if (epoch !== sessionEpoch) throw new AuthRefreshError("认证状态已变化，请重试", 409, false);
    setSession(session);
    return session;
  })();
  const tracked = pending.finally(() => {
    if (refreshPromise === tracked) refreshPromise = null;
  });
  refreshPromise = tracked;
  return tracked;
}

export async function bootstrapSession(): Promise<AuthPrincipalSummary | null> {
  if (authMode === "dev") {
    return null;
  }
  if (new URL(window.location.href).searchParams.has("auth_refresh")) {
    const url = new URL(window.location.href);
    url.searchParams.delete("auth_refresh");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    clearSession();
    return (await refreshAccessToken()).principal;
  }
  if (enterprise_idpCallbackBootstrapPromise) return enterprise_idpCallbackBootstrapPromise;
  const callback = new URL(window.location.href);
  const callbackToken = callback.searchParams.get("token");
  if (callbackToken) {
    const exchangePromise = exchangeEnterpriseIdpCallback(callbackToken);
    enterprise_idpCallbackBootstrapPromise = exchangePromise;
    void exchangePromise.then(() => {
      if (enterprise_idpCallbackBootstrapPromise === exchangePromise) {
        enterprise_idpCallbackBootstrapPromise = null;
      }
    }, () => {
      if (enterprise_idpCallbackBootstrapPromise === exchangePromise) {
        enterprise_idpCallbackBootstrapPromise = null;
      }
    });
    return exchangePromise;
  }

  const storedToken = getAccessToken();
  if (!storedToken) {
    try {
      return (await refreshAccessToken()).principal;
    } catch (reason) {
      if (reason instanceof AuthRefreshError && reason.reauthRequired) return null;
      throw reason;
    }
  }
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${storedToken}` }
  });
  if (response.status === 401) {
    try {
      return (await refreshAccessToken()).principal;
    } catch (reason) {
      if (reason instanceof AuthRefreshError && reason.reauthRequired) return null;
      throw reason;
    }
  }
  if (!response.ok) throw new Error(`认证初始化失败：${response.status}`);
  accessToken = storedToken;
  principal = camelize<AuthPrincipalSummary>(await response.json());
  return principal;
}

async function exchangeEnterpriseIdpCallback(callbackToken: string): Promise<AuthPrincipalSummary> {
  const callback = new URL(window.location.href);
  callback.searchParams.delete("token");
  window.history.replaceState(null, document.title, `${callback.pathname}${callback.search}${callback.hash}`);
  const response = await fetch(`${API_BASE_URL}/auth/enterprise_idp/exchange`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: callbackToken.replace(/ /g, "+") })
  });
  if (!response.ok) {
    clearSession();
    throw new Error(`企业身份认证失败：${response.status}`);
  }
  const session = camelize<AuthTokenResponse>(await response.json());
  setSession(session);
  const pendingQwToken = window.sessionStorage.getItem("pending_qw_token");
  if (pendingQwToken) {
    window.sessionStorage.removeItem("pending_qw_token");
    window.sessionStorage.removeItem(POST_LOGIN_PATH_STORAGE_KEY);
    clearReauthenticationFlow();
    window.location.replace(`/user/wecom-bind?qw_token=${encodeURIComponent(pendingQwToken)}`);
    return session.principal;
  }
  const postLoginPath = window.sessionStorage.getItem(POST_LOGIN_PATH_STORAGE_KEY) || USER_ENTRY_PATH;
  clearReauthenticationFlow();
  window.sessionStorage.removeItem(POST_LOGIN_PATH_STORAGE_KEY);
  const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (currentPath !== postLoginPath) {
    window.location.replace(postLoginPath);
  }
  return session.principal;
}

export async function refreshPrincipal(): Promise<AuthPrincipalSummary | null> {
  if (authMode === "dev") {
    return principal;
  }
  const storedToken = getAccessToken();
  if (!storedToken) {
    try {
      return (await refreshAccessToken()).principal;
    } catch (reason) {
      if (reason instanceof AuthRefreshError && reason.reauthRequired) return null;
      throw reason;
    }
  }
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${storedToken}` }
  });
  if (response.status === 401) {
    try {
      return (await refreshAccessToken()).principal;
    } catch (reason) {
      if (reason instanceof AuthRefreshError && reason.reauthRequired) return null;
      throw reason;
    }
  }
  if (!response.ok) throw new Error(`认证刷新失败：${response.status}`);
  principal = camelize<AuthPrincipalSummary>(await response.json());
  return principal;
}

export function rememberPostLoginPath(returnTo?: string): string {
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const path = returnTo || current || USER_ENTRY_PATH;
  window.sessionStorage.setItem(
    POST_LOGIN_PATH_STORAGE_KEY,
    path
  );
  return path;
}

export function redirectToLogin(returnTo?: string): void {
  // A pending reauthentication flow already owns the original in-app URL.
  // Do not replace it with the callback landing page on the second attempt.
  if (returnTo !== undefined || (!hasPendingReauthentication() && !window.location.pathname.startsWith("/auth/"))) {
    rememberPostLoginPath(returnTo);
  }
  const path = window.sessionStorage.getItem(POST_LOGIN_PATH_STORAGE_KEY) || USER_ENTRY_PATH;
  void readLoginMethod(API_BASE_URL).then((method) => {
    window.location.assign(method === "oauth2" ? oauth2LoginUrl(API_BASE_URL, path) : getLegacySsoLoginUrl());
  }).catch(() => window.location.assign("/auth/error?reason=login_config_unavailable"));
}

export function completeOAuth2Login(): Promise<void> {
  if (completionPromise) return completionPromise;
  completionPromise = (async () => {
    clearSession();
    const response = await fetch(`${API_BASE_URL}/auth/oauth2/complete`, {
      method: "POST", credentials: "include", headers: { "X-Hermes-Requested-With": "user-web" }
    });
    if (!response.ok) throw new Error("登录完成事务无效");
    const completed = await response.json() as { return_to: string };
    await refreshAccessToken();
    clearReauthenticationFlow();
    const target = restoreBusinessPath(completed.return_to);
    const qwToken = window.sessionStorage.getItem("pending_qw_token");
    if (qwToken) window.sessionStorage.removeItem("pending_qw_token");
    window.location.replace(qwToken ? `/user/wecom-bind?qw_token=${encodeURIComponent(qwToken)}` : target);
  })();
  return completionPromise;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE_URL}/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: { "X-Hermes-Requested-With": "user-web" }
    });
  } catch {
    // Local credentials are still discarded even if the server is offline.
  }
  clearSession();
  clearReauthenticationFlow();
  redirectToLogin(USER_ENTRY_PATH);
}
