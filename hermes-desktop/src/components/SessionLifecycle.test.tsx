import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as auth from "../auth";
import {
  clearReauthenticationFlow,
  clearSession,
  requireReauthentication,
  reserveAutomaticReauthenticationAttempt
} from "../auth";
import { userApi } from "../api/client";
import { SessionLifecycle } from "./SessionLifecycle";

vi.mock("../api/client", () => ({
  userApi: {
    streamBrowserPresence: vi.fn()
  }
}));

describe("SessionLifecycle", () => {
  afterEach(() => {
    clearSession();
    clearReauthenticationFlow();
    vi.clearAllMocks();
  });

  it("keeps the page mounted and automatically starts the first reauthentication attempt", async () => {
    vi.mocked(userApi.streamBrowserPresence).mockImplementation(
      async (_onEvent, signal) => new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      })
    );

    render(
      <SessionLifecycle>
        <div>现有页面内容</div>
      </SessionLifecycle>
    );
    await waitFor(() => {
      expect(userApi.streamBrowserPresence).toHaveBeenCalledTimes(1);
    });
    const redirect = vi.spyOn(auth, "redirectToLogin").mockImplementation(() => undefined);

    act(() => requireReauthentication());

    expect(screen.getByText("现有页面内容")).toBeVisible();
    await waitFor(() => expect(redirect).toHaveBeenCalledOnce());
    expect(screen.queryByRole("dialog", { name: "需要重新认证" })).not.toBeInTheDocument();
  });

  it("asks for manual reauthentication after both automatic attempts were used", async () => {
    vi.mocked(userApi.streamBrowserPresence).mockImplementation(
      async (_onEvent, signal) => new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      })
    );
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    expect(reserveAutomaticReauthenticationAttempt()).toBe(true);
    const redirect = vi.spyOn(auth, "redirectToLogin").mockImplementation(() => undefined);

    render(
      <SessionLifecycle>
        <div>现有页面内容</div>
      </SessionLifecycle>
    );
    await waitFor(() => {
      expect(userApi.streamBrowserPresence).toHaveBeenCalledTimes(1);
    });

    act(() => requireReauthentication());

    const dialog = await screen.findByRole("dialog", { name: "需要重新认证" });
    expect(dialog).toBeVisible();
    // Light is the product default when the user never chose a theme.
    expect(dialog.parentElement).toHaveAttribute("data-theme", "light");
    expect(redirect).not.toHaveBeenCalled();
  });

  it("probes the session when the page becomes visible after heartbeats stopped", async () => {
    // 系统休眠唤醒场景：presence 心跳早已停止（时间戳停留在过去），页面
    // 重新可见时必须主动探测一次；refresh 被网关拦成登录页（JSON 解析
    // 失败）也要按会话失效处理，进入重登流程而不是静默重试。
    vi.mocked(userApi.streamBrowserPresence).mockImplementation(
      async (_onEvent, signal) => new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      })
    );
    const refresh = vi.spyOn(auth, "refreshAccessToken").mockRejectedValue(
      new SyntaxError("Unexpected token '<' is not valid JSON")
    );
    const redirect = vi.spyOn(auth, "redirectToLogin").mockImplementation(() => undefined);

    render(
      <SessionLifecycle>
        <div>现有页面内容</div>
      </SessionLifecycle>
    );
    await waitFor(() => {
      expect(userApi.streamBrowserPresence).toHaveBeenCalledTimes(1);
    });

    act(() => document.dispatchEvent(new Event("visibilitychange")));

    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(redirect).toHaveBeenCalledOnce());
  });

  it("skips the wake probe while presence heartbeats are fresh", async () => {
    // 普通切换标签页场景：心跳一直在线，恢复可见不应触发探测请求
    vi.mocked(userApi.streamBrowserPresence).mockImplementation(
      async (onEvent, signal) => {
        onEvent({ event: "presence.heartbeat", data: { session: "active" } });
        return new Promise<void>((resolve) => {
          signal?.addEventListener("abort", () => resolve(), { once: true });
        });
      }
    );
    const refresh = vi.spyOn(auth, "refreshAccessToken").mockImplementation(
      async () => { throw new Error("wake probe must not run"); }
    );

    render(
      <SessionLifecycle>
        <div>现有页面内容</div>
      </SessionLifecycle>
    );
    await waitFor(() => {
      expect(userApi.streamBrowserPresence).toHaveBeenCalledTimes(1);
    });

    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(refresh).not.toHaveBeenCalled();
  });

  it("reauthenticates when the server reports the presence session expired", async () => {
    vi.mocked(userApi.streamBrowserPresence).mockImplementation(
      async (onEvent, signal) => {
        onEvent({ event: "presence.session_expired", data: {} });
        return new Promise<void>((resolve) => {
          signal?.addEventListener("abort", () => resolve(), { once: true });
        });
      }
    );
    const redirect = vi.spyOn(auth, "redirectToLogin").mockImplementation(() => undefined);

    render(
      <SessionLifecycle>
        <div>现有页面内容</div>
      </SessionLifecycle>
    );

    await waitFor(() => expect(redirect).toHaveBeenCalledOnce());
  });
});
