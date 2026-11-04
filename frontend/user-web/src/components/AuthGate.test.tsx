import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  bootstrapSession,
  hasAdminPostLoginIntent,
  hasPendingReauthentication,
  isDevAuthMode,
  markReauthenticationManual,
  redirectToLogin,
  reserveAutomaticReauthenticationAttempt
} from "../auth";
import { userApi } from "../api/client";
import { AuthGate } from "./AuthGate";

vi.mock("../auth", () => ({
  bootstrapSession: vi.fn(),
  hasAdminPostLoginIntent: vi.fn(),
  hasPendingReauthentication: vi.fn(),
  isDevAuthMode: vi.fn(),
  markReauthenticationManual: vi.fn(),
  reserveAutomaticReauthenticationAttempt: vi.fn(),
  redirectToLogin: vi.fn()
}));
vi.mock("../api/client", () => ({
  userApi: {
    bootstrapRuntime: vi.fn()
  }
}));
vi.mock("./SessionLifecycle", () => ({
  SessionLifecycle: ({ children }: { children: React.ReactNode }) => children
}));

describe("AuthGate", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.sessionStorage.clear();
    vi.mocked(isDevAuthMode).mockReturnValue(false);
    vi.mocked(hasAdminPostLoginIntent).mockImplementation(() => {
      const path = window.sessionStorage.getItem("hermes_platform_post_login_path");
      return path === "/admin" || Boolean(path?.startsWith("/admin/"));
    });
    vi.mocked(hasPendingReauthentication).mockReturnValue(false);
    vi.mocked(userApi.bootstrapRuntime).mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "starting"
    });
  });

  it("renders an admin reauthentication handoff instead of the user shell", () => {
    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "/admin/runtime-resources?source=reauth#workspace"
    );
    vi.mocked(bootstrapSession).mockReturnValue(new Promise(() => undefined));

    render(<AuthGate bootstrapPersonalRuntime><div>主页面</div></AuthGate>);

    expect(screen.getByRole("status", { name: "正在重新认证…" })).toBeVisible();
    expect(screen.getByText("Hermes Console")).toBeVisible();
    expect(screen.queryByText("Hermes Work")).not.toBeInTheDocument();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
  });

  it("renders a minimal authentication spinner without mounting the application while pending", () => {
    vi.mocked(bootstrapSession).mockReturnValue(new Promise(() => undefined));

    const { container } = render(<AuthGate><div>主页面</div></AuthGate>);

    expect(screen.getByRole("status", { name: "身份认证登录中…" })).toBeVisible();
    expect(container.querySelector(".authLoadingSpinner")).toBeInTheDocument();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
    expect(screen.queryByText("正在验证企业身份…")).not.toBeInTheDocument();
    expect(screen.queryByText("正在跳转企业登录…")).not.toBeInTheDocument();
  });

  it("renders a custom authentication message for the data app handoff", () => {
    vi.mocked(bootstrapSession).mockReturnValue(new Promise(() => undefined));

    render(
    <AuthGate
      loadingMessage="身份认证，获取权限内数据，马上好..."
      showLoadingMessage
    >
        <div>主页面</div>
      </AuthGate>
    );

    expect(
      screen.getByRole("status", { name: "身份认证，获取权限内数据，马上好..." })
    ).toBeVisible();
    expect(screen.getByText("身份认证，获取权限内数据，马上好...")).toBeVisible();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
  });

  it("renders the application directly after authentication succeeds", async () => {
    vi.mocked(bootstrapSession).mockResolvedValue({
      orgId: 1,
      userId: 7,
      name: "employee-7",
      userCode: "employee-7"
    });

    render(<AuthGate bootstrapPersonalRuntime><div>主页面</div></AuthGate>);

    expect(await screen.findByText("主页面")).toBeVisible();
    expect(screen.queryByText("正在验证企业身份…")).not.toBeInTheDocument();
  });

  it("starts personal Runtime bootstrap before mounting the chat shell", async () => {
    vi.mocked(bootstrapSession).mockResolvedValue({
      orgId: 1,
      userId: 7,
      name: "employee-7",
      userCode: "employee-7"
    });

    render(<AuthGate bootstrapPersonalRuntime><div>主页面</div></AuthGate>);

    await waitFor(() => expect(userApi.bootstrapRuntime).toHaveBeenCalledOnce());
    expect(await screen.findByText("主页面")).toBeVisible();
  });

  it("does not bootstrap the personal Runtime for non-chat destinations", async () => {
    vi.mocked(bootstrapSession).mockResolvedValue({
      orgId: 1,
      userId: 7,
      name: "employee-7",
      userCode: "employee-7"
    });

    render(<AuthGate><div>主页面</div></AuthGate>);

    expect(await screen.findByText("主页面")).toBeVisible();
    expect(userApi.bootstrapRuntime).not.toHaveBeenCalled();
  });

  it("does not mount the application during an EnterpriseIdp admin callback handoff", async () => {
    window.history.replaceState(null, "", "/?token=callback-token");
    window.sessionStorage.setItem(
      "hermes_platform_post_login_path",
      "/admin/runtime-resources"
    );
    vi.mocked(bootstrapSession).mockResolvedValue({
      orgId: 1,
      userId: 7,
      name: "employee-7",
      userCode: "employee-7"
    });

    render(<AuthGate><div>主页面</div></AuthGate>);

    expect(
      await screen.findByRole("status", { name: "正在重新认证…" })
    ).toBeVisible();
    expect(screen.getByText("Hermes Console")).toBeVisible();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
    expect(userApi.bootstrapRuntime).not.toHaveBeenCalled();
  });

  it("still mounts the application for a non-admin EnterpriseIdp callback", async () => {
    window.history.replaceState(null, "", "/?token=callback-token");
    vi.mocked(bootstrapSession).mockResolvedValue({
      orgId: 1,
      userId: 7,
      name: "employee-7",
      userCode: "employee-7"
    });

    render(<AuthGate bootstrapPersonalRuntime><div>主页面</div></AuthGate>);

    expect(await screen.findByText("主页面")).toBeVisible();
    expect(screen.queryByRole("status", { name: "正在重新认证…" })).not.toBeInTheDocument();
    expect(userApi.bootstrapRuntime).toHaveBeenCalledOnce();
  });

  it("keeps the authentication shell visible while enterprise login starts", async () => {
    vi.mocked(bootstrapSession).mockResolvedValue(null);

    render(<AuthGate><div>主页面</div></AuthGate>);

    await waitFor(() => expect(redirectToLogin).toHaveBeenCalledOnce());
    expect(screen.getByRole("status", { name: "身份认证登录中…" })).toBeVisible();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
    expect(screen.queryByText("正在跳转企业登录…")).not.toBeInTheDocument();
  });

  it("keeps authentication failures visible and retryable", async () => {
    vi.mocked(bootstrapSession).mockRejectedValue(new Error("企业认证服务不可用"));

    render(<AuthGate><div>主页面</div></AuthGate>);

    expect(await screen.findByRole("heading", { name: "认证初始化失败" })).toBeVisible();
    expect(screen.getByText("企业认证服务不可用")).toBeVisible();
    expect(screen.getByRole("button", { name: "重试" })).toBeVisible();
    expect(screen.queryByText("主页面")).not.toBeInTheDocument();
  });

  it("automatically starts the second enterprise-login attempt after a failed callback", async () => {
    vi.mocked(hasPendingReauthentication).mockReturnValue(true);
    vi.mocked(reserveAutomaticReauthenticationAttempt).mockReturnValue(true);
    vi.mocked(bootstrapSession).mockRejectedValue(
      new Error("企业身份认证失败：401")
    );

    render(<AuthGate><div>主页面</div></AuthGate>);

    await waitFor(() => {
      expect(reserveAutomaticReauthenticationAttempt).toHaveBeenCalledOnce();
      expect(redirectToLogin).toHaveBeenCalledOnce();
    });
    expect(screen.getByRole("status", { name: "身份认证登录中…" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "需要重新认证" })).not.toBeInTheDocument();
  });

  it("shows manual reauthentication only after the automatic budget is exhausted", async () => {
    vi.mocked(hasPendingReauthentication).mockReturnValue(true);
    vi.mocked(reserveAutomaticReauthenticationAttempt).mockReturnValue(false);
    vi.mocked(bootstrapSession).mockRejectedValue(
      new Error("企业身份认证失败：401")
    );

    render(<AuthGate><div>主页面</div></AuthGate>);

    const dialog = await screen.findByRole("dialog", { name: "需要重新认证" });
    expect(dialog).toBeVisible();
    expect(redirectToLogin).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新认证" }));

    expect(markReauthenticationManual).toHaveBeenCalledOnce();
    expect(redirectToLogin).toHaveBeenCalledOnce();
  });
});
