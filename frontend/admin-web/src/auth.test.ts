import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  bootstrapSession,
  clearSession,
  getAccessToken,
  getLegacySsoLoginUrl,
  rememberAdminPostLoginPath
} from "./auth";

const TOKEN_STORAGE_KEY = "hermes_platform_access_token";
const POST_LOGIN_PATH_STORAGE_KEY = "hermes_platform_post_login_path";

describe("admin shared platform session recovery", () => {
  beforeEach(() => {
    clearSession();
    window.sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("builds the Enterprise SSO login URL from the v2 build configuration", () => {
    expect(getLegacySsoLoginUrl()).toBe(
      "https://login.example.com/sd/index.php/api/clue/login_mobile?name=agent-uat-v2"
    );
  });

  it("restores a new admin tab from the HttpOnly platform session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      access_token: "restored-admin-jwt",
      principal: { org_id: 1, user_id: 10001, name: "admin", user_code: "10001" }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const principal = await bootstrapSession();

    expect(principal?.userId).toBe(10001);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: { "X-Hermes-Requested-With": "admin-web" }
    });
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("restored-admin-jwt");
    expect(getAccessToken()).toBe("restored-admin-jwt");
  });

  it("uses the platform session when a stored JWT has expired", async () => {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, "expired-jwt");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: "renewed-admin-jwt",
        principal: { org_id: 1, user_id: 10001, name: "admin", user_code: "10001" }
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(bootstrapSession()).resolves.toEqual(
      expect.objectContaining({ userId: 10001 })
    );
    expect(fetchMock.mock.calls[0]).toEqual([
      "/api/v1/auth/me",
      { credentials: "include", headers: { Authorization: "Bearer expired-jwt" } }
    ]);
    expect(fetchMock.mock.calls[1]).toEqual([
      "/api/v1/auth/refresh",
      {
        method: "POST",
        credentials: "include",
        headers: { "X-Hermes-Requested-With": "admin-web" }
      }
    ]);
  });

  it("does not fabricate an admin session when the platform cookie has expired", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));

    await expect(bootstrapSession()).resolves.toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  it("preserves an admin deep link for enterprise-login return", () => {
    window.history.replaceState(null, "", "/admin?source=reauth#target");

    const path = rememberAdminPostLoginPath();

    expect(window.sessionStorage.getItem(POST_LOGIN_PATH_STORAGE_KEY)).toBe(
      "/admin?source=reauth#target"
    );
    expect(path).toBe("/admin?source=reauth#target");
  });

  it("falls back to the admin entry instead of persisting a non-admin path", () => {
    window.history.replaceState(null, "", "/user/chat?source=reauth");
    const path = rememberAdminPostLoginPath();

    expect(window.sessionStorage.getItem(POST_LOGIN_PATH_STORAGE_KEY)).toBe("/admin/");
    expect(path).toBe("/admin/");
  });
});
