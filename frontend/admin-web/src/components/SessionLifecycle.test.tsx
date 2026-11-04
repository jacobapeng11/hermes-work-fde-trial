import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as auth from "../auth";
import { SessionLifecycle } from "./SessionLifecycle";

describe("admin SessionLifecycle", () => {
  afterEach(() => {
    auth.clearSession();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps the platform session alive without unmounting the editing page", async () => {
    vi.useFakeTimers();
    auth.setSession({
      accessToken: "admin-jwt",
      tokenType: "bearer",
      expiresIn: 900,
      principal: { orgId: 1, userId: 2, name: "Administrator", userCode: "admin" }
    });
    const refresh = vi.spyOn(auth, "refreshAccessToken").mockImplementation(async () => {
      const session = {
        accessToken: "renewed-admin-jwt",
        tokenType: "bearer",
        expiresIn: 900,
        principal: { orgId: 1, userId: 2, name: "Administrator", userCode: "admin" }
      };
      auth.setSession(session);
      return session;
    });
    const redirect = vi.spyOn(auth, "redirectToLogin").mockImplementation(() => undefined);

    render(<SessionLifecycle><div>正在编辑的管理页面</div></SessionLifecycle>);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 1000);
    });

    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByText("正在编辑的管理页面")).toBeVisible();
    expect(redirect).not.toHaveBeenCalled();
  });
});
