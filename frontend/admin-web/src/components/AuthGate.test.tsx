import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  bootstrapSession: vi.fn(),
  clearSession: vi.fn(),
  getAccessToken: vi.fn(),
  isDevAuthMode: vi.fn(),
  isPasswordAuthMode: vi.fn(),
  redirectToLogin: vi.fn(),
}));
vi.mock("../auth", () => auth);
vi.mock("../api/types", () => ({ camelize: (value: unknown) => value }));
vi.mock("./AdminLoginPage", () => ({ AdminLoginPage: () => <div>密码登录页</div> }));
vi.mock("./SessionLifecycle", () => ({
  SessionLifecycle: ({ children }: { children: React.ReactNode }) => children
}));

import { AuthGate } from "./AuthGate";

describe("AuthGate authentication modes", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({})
    }));
    auth.bootstrapSession.mockResolvedValue(null);
    auth.getAccessToken.mockReturnValue(null);
    auth.isDevAuthMode.mockReturnValue(false);
    auth.isPasswordAuthMode.mockReturnValue(false);
  });

  it("shows the admin reauthentication transition while session recovery is pending", () => {
    auth.bootstrapSession.mockReturnValue(new Promise(() => undefined));

    render(<AuthGate><div>控制台</div></AuthGate>);

    expect(screen.getByRole("status", { name: "正在重新认证…" })).toBeVisible();
    expect(screen.getByText("Hermes Console")).toBeVisible();
    expect(screen.queryByText("控制台")).not.toBeInTheDocument();
    expect(screen.queryByText("正在验证管理权限…")).not.toBeInTheDocument();
  });

  it("shows the password login page for an unauthenticated password mode session", async () => {
    auth.isPasswordAuthMode.mockReturnValue(true);
    render(<AuthGate><div>控制台</div></AuthGate>);

    expect(await screen.findByText("密码登录页")).toBeInTheDocument();
    expect(auth.redirectToLogin).not.toHaveBeenCalled();
  });

  it("keeps dev mode directly accessible", async () => {
    auth.isDevAuthMode.mockReturnValue(true);
    render(<AuthGate><div>控制台</div></AuthGate>);

    await waitFor(() => expect(screen.getByText("控制台")).toBeInTheDocument());
  });

  it("returns an unauthenticated inherit-mode visitor to the user entry", async () => {
    render(<AuthGate><div>控制台</div></AuthGate>);

    await waitFor(() => expect(auth.redirectToLogin).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledWith("/api/v1/admin/me", {
      credentials: "include",
      headers: {}
    });
  });

  it("shows a forbidden page when the authenticated user lacks a work-number grant", async () => {
    auth.bootstrapSession.mockResolvedValue({ orgId: 1, userId: 2, name: "普通用户" });
    vi.mocked(fetch).mockResolvedValue({ status: 403, ok: false } as Response);

    render(<AuthGate><div>控制台</div></AuthGate>);

    expect(await screen.findByText("无管理权限")).toBeInTheDocument();
    expect(auth.redirectToLogin).not.toHaveBeenCalled();
  });
});
