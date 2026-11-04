import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  CheckCircle2,
  LoaderCircle,
  MessageCircleMore,
  QrCode,
  RefreshCw,
  ShieldCheck,
  Unplug,
  UserRoundCheck,
  UsersRound,
  X
} from "lucide-react";
import QRCode from "react-qr-code";
import type {
  ApplicationWeComChannel,
  ApplicationWeComMessage,
  ApplicationWeComUser,
  ImQrOnboardingCreated,
  ImQrOnboardingStatus
} from "@hermes-cloud/schemas";
import { userApi } from "../api/client";


interface ApplicationAgentWeComPanelProps {
  applicationAgentId: number;
}

const terminalOnboardingStatuses = new Set([
  "credentials_ready",
  "failed",
  "expired",
  "consumed"
]);

const transitionalChannelStatuses = new Set([
  "connecting",
  "reconnecting"
]);

function channelStatusLabel(
  channel: ApplicationWeComChannel | null
): string {
  if (!channel) return "读取中";
  return {
    unconfigured: "未配置",
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    disabled: "已停用",
    error: "连接异常",
    revoked: "凭据已解除",
    credentials_invalid: "凭据无效",
    legacy_migration_required: "待迁移"
  }[channel.status];
}

function formatDate(value?: string | null): string {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "暂无";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function dispatchLatency(message: ApplicationWeComMessage): number | null {
  if (!message.firstProviderDispatchAt) return null;
  const received = new Date(message.receivedAt).getTime();
  const dispatched = new Date(message.firstProviderDispatchAt).getTime();
  if (!Number.isFinite(received) || !Number.isFinite(dispatched)) return null;
  return Math.max(0, dispatched - received);
}

function latencyLabel(value: number | null): string {
  if (value == null) return "等待首包";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function ApplicationAgentWeComPanel({
  applicationAgentId
}: ApplicationAgentWeComPanelProps) {
  const [channel, setChannel] =
    useState<ApplicationWeComChannel | null>(null);
  const [users, setUsers] = useState<ApplicationWeComUser[]>([]);
  const [messages, setMessages] = useState<ApplicationWeComMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [botId, setBotId] = useState("");
  const [secret, setSecret] = useState("");
  const [onboarding, setOnboarding] =
    useState<ImQrOnboardingCreated | null>(null);
  const [onboardingStatus, setOnboardingStatus] =
    useState<ImQrOnboardingStatus | null>(null);
  const [rotateConfirm, setRotateConfirm] = useState(false);

  const load = async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const [nextChannel, userList, messageList] =
        await Promise.all([
          userApi.applicationWeComChannel(applicationAgentId),
          userApi.applicationWeComUsers(applicationAgentId, "", 100),
          userApi.applicationWeComMessages(applicationAgentId, 50)
        ]);
      setChannel(nextChannel);
      setUsers(userList.users);
      setMessages(messageList.messages);
      setError(null);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "读取应用企微渠道失败"
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(true);
  }, [applicationAgentId]);

  useEffect(() => {
    if (!channel || !transitionalChannelStatuses.has(channel.status)) return;
    const timer = window.setTimeout(() => void load(), 1200);
    return () => window.clearTimeout(timer);
  }, [channel]);

  useEffect(() => {
    if (
      !onboarding
      || (
        onboardingStatus
        && terminalOnboardingStatuses.has(onboardingStatus.status)
      )
    ) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await userApi.applicationWeComQrOnboarding(
          applicationAgentId,
          onboarding.onboardingId
        );
        setOnboardingStatus(next);
        if (next.botId) setBotId(next.botId);
        if (next.status === "failed" || next.status === "expired") {
          setError(
            next.errorMessage
            || (
              next.status === "expired"
                ? "二维码已过期，请重新获取"
                : "快捷绑定失败"
            )
          );
        }
      } catch (reason) {
        setError(
          reason instanceof Error
            ? reason.message
            : "读取扫码状态失败"
        );
      }
    }, onboarding.pollAfterMs);
    return () => window.clearTimeout(timer);
  }, [applicationAgentId, onboarding, onboardingStatus]);

  const quickReady = (
    onboardingStatus?.status === "credentials_ready"
    && onboardingStatus.secretReady
  );
  const canRegister = Boolean(
    (botId.trim() && secret.trim())
    || (onboarding && quickReady)
  );

  const latencySamples = useMemo(
    () => messages
      .map(dispatchLatency)
      .filter((value): value is number => value != null)
      .sort((left, right) => left - right),
    [messages]
  );
  const medianLatency = latencySamples.length > 0
    ? latencySamples[Math.floor((latencySamples.length - 1) / 2)]
    : null;

  const openSetup = () => {
    setBotId(channel?.botId ?? "");
    setSecret("");
    setOnboarding(null);
    setOnboardingStatus(null);
    setRotateConfirm(false);
    setError(null);
    setNotice(null);
    setSetupOpen(true);
  };

  const startQuickBinding = async () => {
    if (busy) return;
    setBusy("qrcode");
    setError(null);
    try {
      const created = await userApi.createApplicationWeComQrOnboarding(
        applicationAgentId
      );
      setOnboarding(created);
      setOnboardingStatus({
        onboardingId: created.onboardingId,
        status: "pending",
        expiresAt: created.expiresAt,
        secretReady: false
      });
      setSecret("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "二维码获取失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const register = async () => {
    if (!canRegister || busy) return;
    setBusy("register");
    setError(null);
    setNotice(null);
    try {
      const updated = await userApi.registerApplicationWeComChannel(
        applicationAgentId,
        onboarding && quickReady
          ? {
              connectionMode: "websocket",
              onboardingId: onboarding.onboardingId
            }
          : {
              connectionMode: "websocket",
              botId: botId.trim(),
              secret: secret.trim()
            }
      );
      setChannel(updated);
      setSetupOpen(false);
      setNotice(
        updated.enabled
          ? "企微机器人已接入，长连接正在建立。"
          : "企微凭据已保存；发布应用 Agent 后即可启用。"
      );
      await load();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "企微注册失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const setEnabled = async (enabled: boolean) => {
    if (busy || !channel?.secretConfigured) return;
    setBusy(enabled ? "enable" : "disable");
    setError(null);
    setNotice(null);
    try {
      const updated = await userApi.setApplicationWeComEnabled(
        applicationAgentId,
        enabled
      );
      setChannel(updated);
      setNotice(enabled ? "企微渠道正在连接。" : "企微渠道已停用。");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "更新渠道状态失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const reconnect = async () => {
    if (busy) return;
    setBusy("reconnect");
    setError(null);
    setNotice(null);
    try {
      const updated = await userApi.reconnectApplicationWeComChannel(
        applicationAgentId
      );
      setChannel(updated);
      setNotice("已发起立即重连。");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "企微重连失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const updateMessageScope = async (
    scope: "direct" | "group",
    enabled: boolean
  ) => {
    if (!channel || busy) return;
    setBusy(`scope-${scope}`);
    setError(null);
    try {
      const updated = await userApi.updateApplicationWeComSettings(
        applicationAgentId,
        {
          directEnabled: (
            scope === "direct" ? enabled : channel.directEnabled
          ),
          groupEnabled: (
            scope === "group" ? enabled : channel.groupEnabled
          )
        }
      );
      setChannel(updated);
      setNotice("消息范围已更新。");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "保存消息范围失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const rotate = async () => {
    if (busy) return;
    if (!rotateConfirm) {
      setRotateConfirm(true);
      return;
    }
    setBusy("rotate");
    setError(null);
    try {
      const updated = await userApi.rotateApplicationWeComChannel(
        applicationAgentId
      );
      setChannel(updated);
      setSetupOpen(false);
      setRotateConfirm(false);
      setNotice("机器人凭据已解除，企微连接已停止。");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "解除企微凭据失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const revokeIdentity = async (user: ApplicationWeComUser) => {
    const identityId = user.identityId;
    if (
      busy
      || identityId === null
      || !window.confirm(
        `确认撤销 ${user.userName} 的组织企微身份映射？该用户下次需要重新绑定。`
      )
    ) return;
    setBusy(`revoke-${user.identityId}`);
    setError(null);
    try {
      await userApi.revokeOrganizationWeComIdentity(identityId);
      await load();
      setNotice(`${user.userName} 的企微身份映射已撤销。`);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "撤销身份映射失败"
      );
    } finally {
      setBusy(null);
    }
  };

  const qrHint = useMemo(() => {
    if (!onboardingStatus) return "请使用企微扫码完成快捷绑定";
    if (onboardingStatus.status === "scanned") {
      return "已扫码，正在安全获取机器人凭据…";
    }
    if (onboardingStatus.status === "credentials_ready") {
      return "凭据获取成功，可立即注册";
    }
    if (onboardingStatus.status === "expired") return "二维码已过期";
    if (onboardingStatus.status === "failed") return "快捷绑定失败";
    return "请使用企微扫码完成快捷绑定";
  }, [onboardingStatus]);

  return <section className="applicationWeComPage">
    <header className="applicationWeComHeader">
      <span><MessageCircleMore size={22}/></span>
      <div>
        <h1>企微渠道</h1>
        <p>
          将应用 Agent 发布给企业内部成员。单聊与群聊均按真实用户身份运行，
          思考过程、工具状态、正文卡片和附件与 WebUI 使用同一套输出链路。
        </p>
      </div>
      <div className="applicationWeComHeaderActions">
        <span
          className="applicationWeComStatus"
          data-status={channel?.status ?? "loading"}
        >
          {channelStatusLabel(channel)}
        </span>
        <button type="button" onClick={openSetup} disabled={loading}>
          <ShieldCheck size={15}/>
          {channel?.secretConfigured ? "更新机器人凭据" : "接入机器人"}
        </button>
      </div>
    </header>

    {error && !setupOpen && (
      <p className="imError" role="alert">{error}</p>
    )}
    {notice && (
      <p className="applicationAgentNotice" role="status">{notice}</p>
    )}

    {loading ? <p className="muted">正在读取企微渠道配置…</p> : <>
      <div className="applicationWeComGrid">
        <article className="applicationWeComCard applicationWeComConnection">
          <header>
            <div>
              <span className="applicationWeComCardIcon">
                <Activity size={18}/>
              </span>
              <div>
                <h2>实时连接</h2>
                <p>WebSocket 长连接 · 事件唤醒 · 会话级并发</p>
              </div>
            </div>
            {channel?.secretConfigured && (
              <button
                type="button"
                className="knowledgeSwitch"
                role="switch"
                aria-label="启用企微渠道"
                aria-checked={channel.enabled}
                disabled={busy !== null || !channel.activationAllowed}
                onClick={() => void setEnabled(!channel.enabled)}
              >
                <span/>
              </button>
            )}
          </header>
          <div className="applicationWeComFacts">
            <div>
              <span>Bot ID</span>
              <strong>{channel?.botId || "尚未配置"}</strong>
            </div>
            <div>
              <span>最近连接</span>
              <strong>{formatDate(channel?.lastConnectedAt)}</strong>
            </div>
            <div>
              <span>最近消息</span>
              <strong>{formatDate(channel?.lastMessageAt)}</strong>
            </div>
          </div>
          {channel?.lastErrorMessage && (
            <p className="applicationWeComInlineError">
              {channel.lastErrorMessage}
            </p>
          )}
          {channel?.secretConfigured && !channel.activationAllowed && (
            <p className="applicationWeComInlineHint">
              凭据已保存。请先发布应用 Agent，再启用企微消息接入。
            </p>
          )}
          <div className="applicationWeComActions">
            <button
              type="button"
              onClick={() => void reconnect()}
              disabled={
                busy !== null
                || !channel?.enabled
                || !channel.secretConfigured
              }
            >
              <RefreshCw
                size={14}
                className={busy === "reconnect" ? "uploadSpinner" : ""}
              />
              立即重连
            </button>
            <button type="button" onClick={openSetup}>
              <QrCode size={14}/>扫码或手工配置
            </button>
          </div>
        </article>

        <article className="applicationWeComCard">
          <header>
            <div>
              <span className="applicationWeComCardIcon">
                <UsersRound size={18}/>
              </span>
              <div>
                <h2>消息范围</h2>
                <p>所有企业内部用户均可访问，无额外成员白名单</p>
              </div>
            </div>
          </header>
          <div className="applicationWeComScopeRows">
            <div>
              <span>
                <strong>单聊消息</strong>
                <small>每位成员拥有独立长期会话</small>
              </span>
              <button
                type="button"
                className="knowledgeSwitch"
                role="switch"
                aria-label="接收企微单聊消息"
                aria-checked={channel?.directEnabled ?? true}
                disabled={!channel?.channelId || busy !== null}
                onClick={() => void updateMessageScope(
                  "direct",
                  !channel?.directEnabled
                )}
              >
                <span/>
              </button>
            </div>
            <div>
              <span>
                <strong>群聊消息</strong>
                <small>仅在群内 @ 机器人时触发，按群 + 发送者隔离顺序</small>
              </span>
              <button
                type="button"
                className="knowledgeSwitch"
                role="switch"
                aria-label="接收企微群聊消息"
                aria-checked={channel?.groupEnabled ?? true}
                disabled={!channel?.channelId || busy !== null}
                onClick={() => void updateMessageScope(
                  "group",
                  !channel?.groupEnabled
                )}
              >
                <span/>
              </button>
            </div>
          </div>
          <div className="applicationWeComCapabilities">
            <span>公开思考过程</span>
            <span>工具进度</span>
            <span>企微卡片</span>
            <span>生成附件</span>
          </div>
        </article>

        <article className="applicationWeComCard applicationWeComLatency">
          <header>
            <div>
              <span className="applicationWeComCardIcon">
                <Activity size={18}/>
              </span>
              <div>
                <h2>首包链路</h2>
                <p>Backend 收到消息至首次向企微派发可见内容</p>
              </div>
            </div>
            <button type="button" onClick={() => void load()}>
              <RefreshCw size={14}/>刷新
            </button>
          </header>
          <div className="applicationWeComMetric">
            <strong>{latencyLabel(medianLatency)}</strong>
            <span>最近 {latencySamples.length} 条有效样本的中位数</span>
          </div>
          <div className="applicationWeComRecentMessages">
            {messages.slice(0, 5).map((message) => (
              <div key={message.inboundMessageId}>
                <span>
                  {message.chatType === "group" ? "群聊" : "单聊"}
                </span>
                <strong>{latencyLabel(dispatchLatency(message))}</strong>
                <small>{formatDate(message.receivedAt)}</small>
              </div>
            ))}
            {messages.length === 0 && <p>收到企微消息后将在这里显示链路时间。</p>}
          </div>
        </article>
      </div>

      <section className="applicationWeComUsers">
        <header>
          <div>
            <UserRoundCheck size={18}/>
            <div>
              <h2>已识别用户</h2>
              <p>
                每条单聊和群聊消息都会重新校验组织身份与平台账号状态。
              </p>
            </div>
          </div>
          <span>{users.length} 位</span>
        </header>
        <div className="applicationWeComUserTable">
          {users.map((user) => (
            <article key={user.identityId}>
              <span className="applicationWeComAvatar">
                {user.userName.slice(0, 1)}
              </span>
              <span>
                <strong>{user.userName}</strong>
                <small>
                  工号 {user.userCode || "未设置"} · 企微 {user.memberIdMasked}
                </small>
              </span>
              <span>
                <strong>{user.lastChatType === "group" ? "群聊" : "单聊"}</strong>
                <small>最近访问 {formatDate(user.lastSeenAt)}</small>
              </span>
              <button
                type="button"
                disabled={busy !== null || user.status === "revoked"}
                onClick={() => void revokeIdentity(user)}
              >
                {user.status === "revoked" ? "已撤销" : "撤销映射"}
              </button>
            </article>
          ))}
          {users.length === 0 && (
            <p className="muted">
              暂无已识别用户。成员首次发消息时会自动匹配；未匹配成功时，
              Agent 会发送一次性身份绑定链接。
            </p>
          )}
        </div>
      </section>
    </>}

    {setupOpen && (
      <div
        className="modalBackdrop centered imDialogBackdrop"
        role="presentation"
        onClick={() => !busy && setSetupOpen(false)}
      >
        <section
          className="imWeComDialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="application-wecom-register-title"
          onClick={(event) => event.stopPropagation()}
        >
          <button
            className="imDialogClose"
            type="button"
            aria-label="关闭"
            onClick={() => setSetupOpen(false)}
            disabled={busy !== null}
          >
            <X size={18}/>
          </button>
          <header>
            <span className="imDialogIcon"><ShieldCheck size={22}/></span>
            <div>
              <h2 id="application-wecom-register-title">
                接入应用 Agent 企微机器人
              </h2>
              <p>
                使用 WebSocket 长连接实时收发多用户单聊与群聊消息。
                可扫码快捷获取凭据，也可手工填写 Bot ID 和 Secret。
              </p>
            </div>
          </header>
          <fieldset className="imConnectionModes">
            <legend>连接方式</legend>
            <label data-selected="true">
              <input type="radio" checked readOnly/>
              <span>
                <strong>WebSocket 长连接</strong>
                <small>实时推送，Runtime 输出与企微发送解耦</small>
              </span>
            </label>
            <label data-disabled="true">
              <input type="radio" disabled/>
              <span>
                <strong>URL 回调</strong>
                <small>本渠道不使用</small>
              </span>
            </label>
          </fieldset>
          <button
            className="imQuickBind"
            type="button"
            onClick={() => void startQuickBinding()}
            disabled={busy !== null}
          >
            {busy === "qrcode"
              ? <LoaderCircle className="uploadSpinner" size={16}/>
              : <QrCode size={17}/>}
            扫码快捷获取 Bot 凭据
          </button>
          {onboarding && (
            <section className="imQrPanel" aria-live="polite">
              <div className="imQrCode">
                <QRCode value={onboarding.qrContent} size={164}/>
              </div>
              <div>
                <strong>{qrHint}</strong>
                <small>
                  二维码将在 {
                    new Date(onboarding.expiresAt).toLocaleTimeString(
                      "zh-CN",
                      { hour: "2-digit", minute: "2-digit" }
                    )
                  } 失效
                </small>
                {onboardingStatus?.status === "pending"
                  || onboardingStatus?.status === "scanned"
                  ? <LoaderCircle className="uploadSpinner" size={16}/>
                  : onboardingStatus?.status === "credentials_ready"
                    ? <CheckCircle2 size={18}/>
                    : null}
              </div>
            </section>
          )}
          <label className="imCredentialField">
            <span>Bot ID</span>
            <input
              value={botId}
              onChange={(event) => {
                setBotId(event.target.value);
                setOnboarding(null);
                setOnboardingStatus(null);
              }}
              placeholder="请输入 Bot ID"
              autoComplete="off"
            />
          </label>
          <label className="imCredentialField">
            <span>Secret</span>
            <input
              type="password"
              value={quickReady ? "qr-secret-ready" : secret}
              onChange={(event) => setSecret(event.target.value)}
              placeholder={
                channel?.secretConfigured
                  ? "已配置；如需更新请输入新的 Secret"
                  : "请输入 Secret"
              }
              disabled={quickReady}
              autoComplete="new-password"
            />
            {quickReady && <small>Secret 已安全获取，不会显示在页面中。</small>}
          </label>
          {error && <p className="imError" role="alert">{error}</p>}
          {channel?.secretConfigured && (
            <div className="imRevokeArea">
              <button
                type="button"
                onClick={() => void rotate()}
                disabled={busy !== null}
              >
                <Unplug size={13}/>
                {rotateConfirm ? "再次点击确认解除凭据" : "解除机器人凭据"}
              </button>
              <small>解除后立即断开，但不影响组织用户身份映射。</small>
            </div>
          )}
          <footer>
            <button
              type="button"
              onClick={() => setSetupOpen(false)}
              disabled={busy !== null}
            >
              取消
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => void register()}
              disabled={!canRegister || busy !== null}
            >
              {busy === "register" ? "注册中…" : "注册并连接"}
            </button>
          </footer>
        </section>
      </div>
    )}

  </section>;
}
