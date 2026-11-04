import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  bootstrapSession,
  clearReauthenticationFlow,
  clearSession,
  getAccessToken,
  getLegacySsoLoginUrl,
  hasAdminPostLoginIntent,
  hasPendingReauthentication,
  markReauthenticationManual,
  redirectToLogin,
  refreshAccessToken,
  rememberPostLoginPath,
  reserveAutomaticReauthenticationAttempt,
  setSession
} from "./auth";

const TOKEN_STORAGE_KEY = "hermes_platform_access_token";

describe("enterprise authentication callback", () => {
  beforeEach(() => {
    clearSession();
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/user/chat");
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("builds the Enterprise SSO login URL from the v2 build configuration", () => {
    expect(getLegacySsoLoginUrl()).toBe(
      "https://login.example.com/sd/index.php/api/clue/login_mobile?name=agent-uat-v2"
    );
  });

  it("exchanges the callback token, removes it from the URL, and caches the JWT", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        access_token: "platform-jwt",
        principal: { org_id: 1, user_id: 7, name: "employee-7", user_code: "employee-7" }
      })
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState(null, "", "/user/chat?token=base64+token");

    const principal = await bootstrapSession();

    expect(principal?.userId).toBe(7);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/enterprise_idp/exchange",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ token: "base64+token" })
      })
    );
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("platform-jwt");
    expect(getAccessToken()).toBe("platform-jwt");
  });

  it("coalesces StrictMode callback initialization into one token exchange", async () => {
    let finishExchange: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>(
      (resolve) => { finishExchange = resolve; }
    ));
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState(null, "", "/user/chat?token=base64+token");

    const first = bootstrapSession();
    const second = bootstrapSession();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("");
    finishExchange?.(new Response(JSON.stringify({
      access_token: "platform-jwt",
      principal: { org_id: 1, user_id: 7, name: "employee-7", user_code: "employee-7" }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ userId: 7 }),
      expect.objectContaining({ userId: 7 })
    ]);
  });

  it("restores a cached JWT at the user entry before rendering the application", async () => {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, "platform-jwt");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ org_id: 1, user_id: 7, name: "employee-7", user_code: "employee-7" })
    });
    vi.stubGlobal("fetch", fetchMock);

    const principal = await bootstrapSession();

    expect(principal?.userId).toBe(7);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      {
        credentials: "include",
        headers: { Authorization: "Bearer platform-jwt" }
      }
    );
  });

  it("keeps the WeCom binding return path across Enterprise SSO login", () => {
    window.history.replaceState(
      null,
      "",
      "/user/wecom-bind?qw_token=wecom-zhangsan"
    );
    rememberPostLoginPath();
    expect(
      window.sessionStorage.getItem("hermes_platform_post_login_path")
    ).toBe("/user/wecom-bind?qw_token=wecom-zhangsan");
  });

  it("redirects to wecom-bind with pending_qw_token after Enterprise SSO login", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        access_token: "platform-jwt",
        principal: { org_id: 1, user_id: 7, name: "employee-7", user_code: "employee-7" }
      })
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState(null, "", "/?token=base64+token");
    window.sessionStorage.setItem("pending_qw_token", "wecom-zhangsan");
    // Store a post-login path that should be ignored when pending_qw_token is set
    window.sessionStorage.setItem("hermes_platform_post_login_path", "/user/chat");

    await bootstrapSession();

    // pending_qw_token is consumed
    expect(window.sessionStorage.getItem("pending_qw_token")).toBeNull();
    // post-login path is cleared (wecom-bind takes priority)
    expect(window.sessionStorage.getItem("hermes_platform_post_login_path")).toBeNull();
    // JWT was set
    expect(getAccessToken()).toBe("platform-jwt");
  });

  it("recognizes only an admin post-login path as an admin handoff", () => {
    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "/admin/runtime-resources?source=reauth#workspace"
    );
    expect(hasAdminPostLoginIntent()).toBe(true);

    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "/admin?source=reauth#workspace"
    );
    expect(hasAdminPostLoginIntent()).toBe(true);

    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "/user/chat?source=reauth"
    );
    expect(hasAdminPostLoginIntent()).toBe(false);

    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "//example.com/admin"
    );
    expect(hasAdminPostLoginIntent()).toBe(false);
  });

  it("coalesces concurrent silent refreshes into one request", async () => {
    let finishRefresh: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>(
      (resolve) => { finishRefresh = resolve; }
    ));
    vi.stubGlobal("fetch", fetchMock);

    const first = refreshAccessToken();
    const second = refreshAccessToken();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    finishRefresh?.(new Response(JSON.stringify({
      access_token: "renewed-jwt",
      expires_in: 900,
      principal: {
        org_id: 1,
        user_id: 7,
        name: "employee-7",
        user_code: "employee-7"
      }
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ accessToken: "renewed-jwt" }),
      expect.objectContaining({ accessToken: "renewed-jwt" })
    ]);
    expect(getAccessToken()).toBe("renewed-jwt");
  });

  it("marks only a definitive refresh rejection as requiring reauthentication", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        detail: {
          code: "session_expired",
          message: "platform session expired"
        }
      }),
      {
        status: 401,
        headers: { "Content-Type": "application/json" }
      }
    )));

    await expect(refreshAccessToken()).rejects.toEqual(
      expect.objectContaining({
        status: 401,
        reauthRequired: true
      })
    );
    expect(getAccessToken()).toBeNull();
  });

  it("allows only two automatic reauthentication attempts while preserving the original URL", () => {
    window.history.replaceState(
      null,
      "",
      "/user/chat?session=history-7#latest"
    );

    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    window.history.replaceState(null, "", "/?token=callback-token");
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(false);

    expect(hasPendingReauthentication()).toBe(true);
    expect(
      window.sessionStorage.getItem("hermes_platform_post_login_path")
    ).toBe("/user/chat?session=history-7#latest");
  });

  it("does not replace the original URL when redirecting a later automatic attempt", () => {
    window.history.replaceState(
      null,
      "",
      "/user/chat?session=history-7#latest"
    );
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    window.history.replaceState(null, "", "/?callback=failed");
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    redirectToLogin();

    expect(
      window.sessionStorage.getItem("hermes_platform_post_login_path")
    ).toBe("/user/chat?session=history-7#latest");
    consoleError.mockRestore();
  });

  it("does not restart automatic attempts after the user chooses manual reauthentication", () => {
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);

    markReauthenticationManual();

    expect(hasPendingReauthentication()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(false);
  });

  it("drops an expired retry budget instead of keeping a stale redirect loop", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-25T10:00:00+08:00"));
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(false);

    vi.advanceTimersByTime(10 * 60_000 + 1);

    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(false);
  });

  it("clears the retry flow after authentication succeeds", () => {
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);

    setSession({
      accessToken: "renewed-jwt",
      tokenType: "bearer",
      expiresIn: 900,
      principal: { orgId: 1, userId: 7, name: "employee-7" }
    });

    expect(hasPendingReauthentication()).toBe(false);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
  });
});
