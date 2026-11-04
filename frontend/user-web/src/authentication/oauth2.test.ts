import { beforeEach, describe, expect, it, vi } from "vitest";
import { oauth2LoginUrl, readLoginMethod, restoreBusinessPath } from "../authLogin";

describe("OAuth2 login routing and business restoration", () => {
  beforeEach(() => { window.sessionStorage.clear(); vi.restoreAllMocks(); });

  it("reads exactly the Backend-selected method and never guesses on failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ login_method: "oauth2" })));
    vi.stubGlobal("fetch", fetchMock);
    expect(await readLoginMethod("/api/v1")).toBe("oauth2");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/config", { credentials: "include", cache: "no-store" });
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ login_method: "enterprise_idp" })));
    await expect(readLoginMethod("/api/v1")).rejects.toThrow("不受支持");
    fetchMock.mockResolvedValue(new Response("", { status: 503 }));
    await expect(readLoginMethod("/api/v1")).rejects.toThrow("暂时不可用");
  });

  it("keeps WeCom tickets and external app state within the tab", () => {
    const path = "/user/application-agents/authorize?client_id=app&state=external-state&redirect_uri=https%3A%2F%2Fapp.example%2Fcallback";
    expect(oauth2LoginUrl("/api/v1", path)).toBe("/api/v1/auth/oauth2/login?return_to=%2Fuser%2Fapplication-agents%2Fauthorize");
    expect(restoreBusinessPath("/user/application-agents/authorize")).toBe(path);
    oauth2LoginUrl("/api/v1", "/user/wecom-bind?ticket=private-ticket");
    expect(restoreBusinessPath("/user/wecom-bind")).toBe("/user/wecom-bind?ticket=private-ticket");
  });

  it("does not restore a stored path into an unrelated destination or external origin", () => {
    oauth2LoginUrl("/api/v1", "/admin/models?tab=hidden");
    expect(restoreBusinessPath("/user/chat")).toBe("/user/chat");
    expect(oauth2LoginUrl("/api/v1", "//evil.example")).toContain("return_to=%2Fuser%2Fchat");
    expect(oauth2LoginUrl("/api/v1", "/user/%5c%5cevil")).toContain("return_to=%2Fuser%2Fchat");
  });

  it("OAuth2 completion replaces an old JWT and bypasses me before restoring the page", async () => {
    vi.resetModules();
    const auth = await import("../auth");
    auth.setSession({ accessToken: "old-jwt", tokenType: "bearer", principal: { orgId: 1, userId: 1, name: "Old" } });
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (raw: string) => {
      calls.push(raw);
      if (raw.endsWith("/oauth2/complete")) return new Response(JSON.stringify({ return_to: "/user/chat" }));
      return new Response(JSON.stringify({ access_token: "new-jwt", expires_in: 900, principal: { org_id: 1, user_id: 7, name: "New" } }));
    }));
    const first = auth.completeOAuth2Login();
    const second = auth.completeOAuth2Login();
    expect(first).toBe(second);
    await first;
    expect(auth.getAccessToken()).toBe("new-jwt");
    expect(calls).toEqual(["/api/v1/auth/oauth2/complete", "/api/v1/auth/refresh"]);
  });
});
