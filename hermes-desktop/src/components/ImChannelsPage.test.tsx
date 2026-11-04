import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImChannelCard } from "@hermes-cloud/schemas";
import { ImChannelsPage } from "./ImChannelsPage";

const api = vi.hoisted(() => ({
  imChannels: vi.fn(),
  createWeComQrOnboarding: vi.fn(),
  weComQrOnboarding: vi.fn(),
  registerWeComChannel: vi.fn(),
  setWeComChannelEnabled: vi.fn(),
  revokeWeComChannel: vi.fn()
}));

vi.mock("../api/client", () => ({ userApi: api }));

const cards: ImChannelCard[] = [
  {
    provider: "feishu",
    displayName: "飞书",
    available: false,
    enabled: false,
    status: "unconfigured",
    secretConfigured: false
  },
  {
    provider: "wecom",
    displayName: "企微",
    available: true,
    enabled: false,
    status: "unconfigured",
    secretConfigured: false
  },
  {
    provider: "wechat",
    displayName: "微信",
    available: false,
    enabled: false,
    status: "unconfigured",
    secretConfigured: false
  },
  {
    provider: "dingtalk",
    displayName: "钉钉",
    available: false,
    enabled: false,
    status: "unconfigured",
    secretConfigured: false
  }
];

describe("ImChannelsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.imChannels.mockResolvedValue({ channels: cards });
    api.registerWeComChannel.mockResolvedValue(cards[1]);
  });

  it("renders four providers and exposes only the WeCom configuration", async () => {
    const user = userEvent.setup();
    render(<ImChannelsPage />);

    expect(await screen.findByRole("button", { name: "配置企微" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "配置飞书" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "配置微信" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "配置钉钉" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "配置企微" }));
    const dialog = screen.getByRole("dialog", {
      name: "注册企微助理通道"
    });
    expect(
      within(dialog).getByText(
        "选择连接方式并输入对应凭据，以将此工作区绑定到企微助理。"
      )
    ).toBeVisible();
    expect(
      within(dialog).getByRole("radio", {
        name: /WebSocket 长连接/
      })
    ).toBeChecked();
    expect(
      within(dialog).getByRole("radio", {
        name: /使用 URL 回调/
      })
    ).toBeDisabled();
    expect(within(dialog).getByText(/暂不可选 · 敬请期待/)).toBeVisible();
  });

  it("registers manually entered Bot ID and Secret", async () => {
    const onChannelChanged = vi.fn();
    const user = userEvent.setup();
    render(<ImChannelsPage onChannelChanged={onChannelChanged} />);

    await user.click(await screen.findByRole("button", { name: "配置企微" }));
    const dialog = screen.getByRole("dialog", {
      name: "注册企微助理通道"
    });
    await user.type(
      within(dialog).getByPlaceholderText("请输入 Bot ID"),
      "bot-manual"
    );
    await user.type(
      within(dialog).getByPlaceholderText("请输入 Secret"),
      "secret-manual"
    );
    await user.click(within(dialog).getByRole("button", { name: "注册" }));

    await waitFor(() => expect(api.registerWeComChannel).toHaveBeenCalledWith({
      connectionMode: "websocket",
      botId: "bot-manual",
      secret: "secret-manual"
    }));
    await waitFor(() => expect(onChannelChanged).toHaveBeenCalledOnce());
  });

  it("uses QR onboarding credentials without exposing the Secret", async () => {
    api.createWeComQrOnboarding.mockResolvedValue({
      onboardingId: "imo-1",
      qrContent: "https://work.weixin.qq.com/ai/qc/confirm?code=one",
      expiresAt: "2026-07-29T13:00:00Z",
      pollAfterMs: 1
    });
    api.weComQrOnboarding.mockResolvedValue({
      onboardingId: "imo-1",
      status: "credentials_ready",
      expiresAt: "2026-07-29T13:00:00Z",
      botId: "bot-from-qr",
      secretReady: true
    });
    const user = userEvent.setup();
    render(<ImChannelsPage />);

    await user.click(await screen.findByRole("button", { name: "配置企微" }));
    await user.click(
      screen.getByRole("button", { name: "快捷绑定接入" })
    );

    expect(
      await screen.findByText("凭据获取成功，Bot ID 与 Secret 已自动填入")
    ).toBeVisible();
    expect(screen.getByDisplayValue("bot-from-qr")).toBeVisible();
    expect(
      screen.getByDisplayValue("••••••••（已通过快捷绑定获取）")
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "注册" }));
    await waitFor(() => expect(api.registerWeComChannel).toHaveBeenCalledWith({
      connectionMode: "websocket",
      onboardingId: "imo-1"
    }));
  });
});
