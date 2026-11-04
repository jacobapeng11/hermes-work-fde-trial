import type { AuthPrincipalSummary, AuthTokenResponse } from "@hermes-cloud/schemas";
import { camelize } from "./api/types";
import { oauth2LoginUrl, readLoginMethod } from "./authLogin";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const LEGACY_SSO_LOGIN_BASE_URL = import.meta.env.VITE_LEGACY_SSO_LOGIN_BASE_URL;
const LEGACY_SSO_APP_NAME = import.meta.env.VITE_LEGACY_SSO_APP_NAME;
const ADMIN_ENTRY_PATH = "/admin/";
const ACCESS_TOKEN_STORAGE_KEY = "hermes_platform_access_token";
const ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY = "hermes_platform_access_token_expires_at";
const POST_LOGIN_PATH_STORAGE_KEY = "hermes_platform_post_login_path";
let accessToken: string | null = null;
let accessTokenExpiresAt: number | null = null;
let principal: AuthPrincipalSummary | null = null;
let platformSessionRefreshPromise: Promise<AuthTokenResponse> | null = null;
export const getAdminAuthMode = (): string => import.meta.env.VITE_AUTH_MODE || "inherit";
export const isDevAuthMode = (): boolean => getAdminAuthMode() === "dev";
export const isPasswordAuthMode = (): boolean => getAdminAuthMode() === "password";

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

export function getAccessToken(): string | null { return accessToken ?? window.sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY); }
export function getAccessTokenExpiresAt(): number | null {
  if (accessTokenExpiresAt !== null) return accessTokenExpiresAt;
  const stored = Number(window.sessionStorage.getItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY));
  return Number.isFinite(stored) && stored > 0 ? stored : null;
}
export function getPrincipal(): AuthPrincipalSummary | null { return principal; }
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

export function setSession(session: AuthTokenResponse): void {
  accessToken = session.accessToken;
  accessTokenExpiresAt = session.expiresIn
    ? Date.now() + session.expiresIn * 1000
    : tokenExpiration(session.accessToken);
  principal = session.principal;
  window.sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, session.accessToken);
  if (accessTokenExpiresAt !== null) {
    window.sessionStorage.setItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY, String(accessTokenExpiresAt));
  } else {
    window.sessionStorage.removeItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY);
  }
}
export function clearSession(): void {
  accessToken = null;
  accessTokenExpiresAt = null;
  principal = null;
  window.sessionStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
  window.sessionStorage.removeItem(ACCESS_TOKEN_EXPIRES_AT_STORAGE_KEY);
}

export function rememberAdminPostLoginPath(): string {
  const { pathname, search, hash } = window.location;
  const path = pathname === "/admin" || pathname.startsWith("/admin/")
    ? `${pathname}${search}${hash}`
    : ADMIN_ENTRY_PATH;
  window.sessionStorage.setItem(POST_LOGIN_PATH_STORAGE_KEY, path);
  return path;
}

export async function bootstrapSession(): Promise<AuthPrincipalSummary | null> {
  if (isDevAuthMode()) return null;
  return restoreStoredSession(!isPasswordAuthMode());
}

async function restoreStoredSession(
  allowPlatformSessionRefresh: boolean
): Promise<AuthPrincipalSummary | null> {
  const storedToken = getAccessToken();
  if (!storedToken) {
    return allowPlatformSessionRefresh ? refreshPlatformSession() : null;
  }
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    credentials: "include",
    headers: { Authorization: `Bearer ${storedToken}` }
  });
  if (response.status === 401) {
    clearSession();
    return allowPlatformSessionRefresh ? refreshPlatformSession() : null;
  }
  if (!response.ok) throw new Error(`认证初始化失败：${response.status}`);
  accessToken = storedToken;
  principal = camelize<AuthPrincipalSummary>(await response.json());
  return principal;
}

async function refreshPlatformSession(): Promise<AuthPrincipalSummary | null> {
  try {
    return (await refreshAccessToken()).principal;
  } catch (reason) {
    if (reason instanceof AuthRefreshError && reason.reauthRequired) return null;
    throw reason;
  }
}

export async function refreshAccessToken(): Promise<AuthTokenResponse> {
  if (platformSessionRefreshPromise) return platformSessionRefreshPromise;
  platformSessionRefreshPromise = (async () => {
    const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "X-Hermes-Requested-With": "admin-web" }
    });
    if (response.status === 401) {
      clearSession();
      throw new AuthRefreshError("认证已失效，请重新认证", 401, true);
    }
    if (!response.ok) {
      throw new AuthRefreshError(`认证服务暂时不可用：${response.status}`, response.status, false);
    }
    const session = camelize<AuthTokenResponse>(await response.json());
    setSession(session);
    return session;
  })().finally(() => {
    platformSessionRefreshPromise = null;
  });
  return platformSessionRefreshPromise;
}

export function redirectToLogin(): void {
  if (isPasswordAuthMode()) {
    window.location.assign(ADMIN_ENTRY_PATH);
    return;
  }
  // EnterpriseIdp callback ownership belongs exclusively to the user application.
  // It receives /?token=..., exchanges it, then returns this same tab to the
  // stored Admin path after the server reports an active work-number grant.
  const path = rememberAdminPostLoginPath();
  void readLoginMethod(API_BASE_URL).then((method) => {
    window.location.assign(method === "oauth2" ? oauth2LoginUrl(API_BASE_URL, path) : getLegacySsoLoginUrl());
  }).catch(() => window.location.assign("/auth/error?reason=login_config_unavailable"));
}

export async function logout(): Promise<void> {
  clearSession();
  if (isPasswordAuthMode()) {
    window.location.assign(ADMIN_ENTRY_PATH);
    return;
  }
  redirectToLogin();
}
