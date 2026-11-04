import { render, screen, waitFor, within } from
  "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApplicationWeComChannel } from
  "@hermes-cloud/schemas";
import { ApplicationAgentWeComPanel } from
  "./ApplicationAgentWeComPanel";


const api = vi.hoisted(() => ({
  applicationWeComChannel: vi.fn(),
  applicationWeComUsers: vi.fn(),
  applicationWeComMessages: vi.fn(),
  createApplicationWeComQrOnboarding: vi.fn(),
  applicationWeComQrOnboarding: vi.fn(),
  registerApplicationWeComChannel: vi.fn(),
  updateApplicationWeComSettings: vi.fn(),
  setApplicationWeComEnabled: vi.fn(),
  reconnectApplicationWeComChannel: vi.fn(),
  rotateApplicationWeComChannel: vi.fn(),
  revokeOrganizationWeComIdentity: vi.fn()
}));

vi.mock("../api/client", () => ({ userApi: api }));

const channel: ApplicationWeComChannel = {
  channelId: 41,
  applicationAgentId: 31,
  provider: "wecom",
  targetType: "application_agent",
  activationAllowed: true,
  enabled: true,
  status: "connected",
  connectionMode: "websocket",
  botId: "bot-application",
  secretConfigured: true,
  directEnabled: true,
  groupEnabled: true,
  groupTrigger: "mention_only",
  configGeneration: 2,
  lastConnectedAt: "2026-07-30T08:00:00Z",
  lastMessageAt: "2026-07-30T08:01:00Z",
  legacyMigrationRequired: false
};

describe("ApplicationAgentWeComPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.applicationWeComChannel.mockResolvedValue(channel);
    api.applicationWeComUsers.mockResolvedValue({
      users: [{
        identityId: 8,
        platformUserId: 12,
        userName: "张三",
        userCode: "E0012",
        memberIdMasked: "zh****an",
        status: "active",
        firstSeenAt: "2026-07-30T07:00:00Z",
        lastSeenAt: "2026-07-30T08:01:00Z",
        lastChatType: "group"
      }],
      total: 1
    });
    api.applicationWeComMessages.mockResolvedValue({
      messages: [{
        inboundMessageId: 91,
        chatType: "group",
        status: "completed",
        identityStatus: "resolved",
        senderPlatformUserId: 12,
        receivedAt: "2026-07-30T08:01:00.000Z",
        firstProviderDispatchAt: "2026-07-30T08:01:00.120Z",
        completedAt: "2026-07-30T08:01:02.000Z"
      }]
    });
    api.registerApplicationWeComChannel.mockResolvedValue(channel);
    api.updateApplicationWeComSettings.mockResolvedValue(channel);
  });

  it("shows multi-user, group and first-dispatch state", async () => {
    render(<ApplicationAgentWeComPanel applicationAgentId={31}/>);

    expect(await screen.findByText("实时连接")).toBeVisible();
    expect(screen.getByRole("switch", {
      name: "接收企微单聊消息"
    })).toBeChecked();
    expect(screen.getByRole("switch", {
      name: "接收企微群聊消息"
    })).toBeChecked();
    expect(screen.getAllByText("120 ms")).toHaveLength(2);
    expect(screen.getByText("张三")).toBeVisible();
    expect(screen.getByText(/工号 E0012/)).toBeVisible();
    expect(screen.getByText("仅在群内 @ 机器人时触发，按群 + 发送者隔离顺序"))
      .toBeVisible();
    expect(screen.queryByText("成员邀请二维码")).not.toBeInTheDocument();
  });

  it("keeps QR and manual Bot credential entrances", async () => {
    const user = userEvent.setup();
    api.createApplicationWeComQrOnboarding.mockResolvedValue({
      onboardingId: "ima-qr-1",
      qrContent: "https://work.weixin.qq.com/ai/qc/confirm?code=one",
      expiresAt: "2026-07-30T09:00:00Z",
      pollAfterMs: 1
    });
    api.applicationWeComQrOnboarding.mockResolvedValue({
      onboardingId: "ima-qr-1",
      status: "credentials_ready",
      expiresAt: "2026-07-30T09:00:00Z",
      botId: "bot-from-qr",
      secretReady: true
    });
    render(<ApplicationAgentWeComPanel applicationAgentId={31}/>);

    await user.click(await screen.findByRole("button", {
      name: "更新机器人凭据"
    }));
    const dialog = screen.getByRole("dialog", {
      name: "接入应用 Agent 企微机器人"
    });
    expect(within(dialog).getByPlaceholderText("请输入 Bot ID")).toBeVisible();
    expect(within(dialog).getByText(/手工填写 Bot ID 和 Secret/))
      .toBeVisible();

    await user.click(within(dialog).getByRole("button", {
      name: "扫码快捷获取 Bot 凭据"
    }));
    expect(await within(dialog).findByText("凭据获取成功，可立即注册"))
      .toBeVisible();
    await user.click(within(dialog).getByRole("button", {
      name: "注册并连接"
    }));

    await waitFor(() =>
      expect(api.registerApplicationWeComChannel).toHaveBeenCalledWith(
        31,
        {
          connectionMode: "websocket",
          onboardingId: "ima-qr-1"
        }
      )
    );
  });
});
