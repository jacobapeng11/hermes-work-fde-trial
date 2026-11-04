import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { userApi } from "../api/client";
import * as authModule from "../auth";
import { WeComBindPage } from "./WeComBindPage";


describe("WeComBindPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(
      null,
      "",
      "/user/wecom-bind?qw_token=wecom-zhangsan"
    );
    window.sessionStorage.clear();
  });

  it("calls bindQwToken and shows success when the user is already logged in", async () => {
    vi.spyOn(authModule, "getAccessToken").mockReturnValue("fake-jwt-token");
    vi.spyOn(userApi, "bindQwToken").mockResolvedValue({
      status: "bound",
      qwToken: "wecom-zhangsan",
    });

    render(<WeComBindPage />);

    await waitFor(() => {
      expect(userApi.bindQwToken).toHaveBeenCalledWith("wecom-zhangsan");
    });
    expect(
      await screen.findByRole("heading", { name: "企业身份绑定成功" })
    ).toBeInTheDocument();
  });

  it("keeps the legacy ticket binding path available during migration", async () => {
    window.history.replaceState(
      null,
      "",
      "/user/wecom-bind?ticket=wcb_legacy_ticket"
    );
    vi.spyOn(authModule, "getAccessToken").mockReturnValue("fake-jwt-token");
    vi.spyOn(userApi, "bindApplicationAgentWeComMember").mockResolvedValue({
      applicationAgentName: "工单助手",
      channelName: "企微入口",
      wecomMemberIdMasked: "zh****an",
      boundAt: "2026-09-10T00:00:00Z",
    });

    render(<WeComBindPage />);

    await waitFor(() => {
      expect(userApi.bindApplicationAgentWeComMember).toHaveBeenCalledWith(
        "wcb_legacy_ticket"
      );
    });
    expect(
      await screen.findByRole("heading", { name: "企业身份绑定成功" })
    ).toBeInTheDocument();
  });

  it("redirects to Enterprise SSO login when the user is not logged in, saving qw_token", async () => {
    vi.spyOn(authModule, "getAccessToken").mockReturnValue(null);
    const redirectToLogin = vi.spyOn(authModule, "redirectToLogin").mockImplementation(() => {});

    render(<WeComBindPage />);

    await waitFor(() => {
      expect(redirectToLogin).toHaveBeenCalled();
    });
    expect(window.sessionStorage.getItem("pending_qw_token")).toBe("wecom-zhangsan");
  });

  it("shows an error when qw_token is missing from the URL", async () => {
    window.history.replaceState(null, "", "/user/wecom-bind");
    vi.spyOn(authModule, "getAccessToken").mockReturnValue("fake-jwt-token");

    render(<WeComBindPage />);

    expect(
      await screen.findByRole("heading", { name: "绑定未完成" })
    ).toBeInTheDocument();
  });

  it("shows an error when the bind API fails", async () => {
    vi.spyOn(authModule, "getAccessToken").mockReturnValue("fake-jwt-token");
    vi.spyOn(userApi, "bindQwToken").mockRejectedValue(new Error("该企微身份已绑定其他用户"));

    render(<WeComBindPage />);

    expect(
      await screen.findByRole("heading", { name: "绑定未完成" })
    ).toBeInTheDocument();
    expect(screen.getByText(/该企微身份已绑定其他用户/)).toBeInTheDocument();
  });
});
