import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, Modal, Radio, Switch, Tag } from "antd";
import {
  CheckCircle2,
  LoaderCircle,
  MessageCircleMore,
  QrCode
} from "lucide-react";
import QRCode from "react-qr-code";
import type {
  ImChannelCard,
  ImQrOnboardingCreated,
  ImQrOnboardingStatus
} from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";


interface ImChannelsPageProps {
  onChannelChanged?: () => void | Promise<void>;
}

const stableStatuses = new Set(["connected", "disabled", "error", "revoked", "unconfigured"]);

function statusLabel(channel: ImChannelCard): string {
  if (!channel.available) return "敬请期待";
  return {
    unconfigured: "未配置",
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    disabled: "已停用",
    error: "连接异常",
    revoked: "已解除"
  }[channel.status];
}

// 状态 Tag 语义色（对齐 expert/mcp 卡 Tag 色板）
function statusTone(channel: ImChannelCard): string {
  if (!channel.available) return "muted";
  if (channel.status === "connected") return "green";
  if (channel.status === "connecting" || channel.status === "reconnecting") return "blue";
  if (channel.status === "error") return "red";
  return "muted";
}

export function ImChannelsPage({ onChannelChanged }: ImChannelsPageProps) {
  const [channels, setChannels] = useState<ImChannelCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [botId, setBotId] = useState("");
  const [secret, setSecret] = useState("");
  const [onboarding, setOnboarding] = useState<ImQrOnboardingCreated | null>(null);
  const [onboardingStatus, setOnboardingStatus] = useState<ImQrOnboardingStatus | null>(null);
  const [revokeConfirm, setRevokeConfirm] = useState(false);
  const { theme } = usePlatformTheme();

  const wecom = channels.find((channel) => channel.provider === "wecom");
  const quickReady = onboardingStatus?.status === "credentials_ready" && onboardingStatus.secretReady;
  const canRegister = Boolean((botId.trim() && secret.trim()) || (onboarding && quickReady));

  const load = async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const response = await userApi.imChannels();
      setChannels(response.channels);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 IM 配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(true);
  }, []);

  useEffect(() => {
    if (!channels.some((channel) => channel.available && !stableStatuses.has(channel.status))) return;
    const timer = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(timer);
  }, [channels]);

  useEffect(() => {
    if (!onboarding || onboardingStatus?.status === "credentials_ready" || onboardingStatus?.status === "failed" || onboardingStatus?.status === "expired") return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await userApi.weComQrOnboarding(onboarding.onboardingId);
        setOnboardingStatus(next);
        if (next.botId) setBotId(next.botId);
        if (next.status === "failed" || next.status === "expired") {
          setError(next.errorMessage || (next.status === "expired" ? "二维码已过期，请重新获取" : "快捷绑定失败"));
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "读取扫码状态失败");
      }
    }, onboarding.pollAfterMs);
    return () => window.clearTimeout(timer);
  }, [onboarding, onboardingStatus]);

  const openWeCom = () => {
    setBotId(wecom?.botId || "");
    setSecret("");
    setOnboarding(null);
    setOnboardingStatus(null);
    setRevokeConfirm(false);
    setError(null);
    setDialogOpen(true);
  };

  const startQuickBinding = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await userApi.createWeComQrOnboarding();
      setOnboarding(created);
      setOnboardingStatus({
        onboardingId: created.onboardingId,
        status: "pending",
        expiresAt: created.expiresAt,
        secretReady: false
      });
      setSecret("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "二维码获取失败");
    } finally {
      setBusy(false);
    }
  };

  const register = async () => {
    if (!canRegister || busy) return;
    setBusy(true);
    setError(null);
    try {
      await userApi.registerWeComChannel(onboarding && quickReady
        ? { connectionMode: "websocket", onboardingId: onboarding.onboardingId }
        : { connectionMode: "websocket", botId: botId.trim(), secret: secret.trim() });
      setDialogOpen(false);
      await load();
      await onChannelChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "企微注册失败");
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async (channel: ImChannelCard) => {
    if (busy || !channel.available) return;
    if (channel.status === "unconfigured" || channel.status === "revoked") {
      openWeCom();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await userApi.setWeComChannelEnabled(!channel.enabled);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新企微状态失败");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (busy) return;
    if (!revokeConfirm) {
      setRevokeConfirm(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await userApi.revokeWeComChannel();
      setDialogOpen(false);
      await load();
      await onChannelChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "解除企微绑定失败");
    } finally {
      setBusy(false);
      setRevokeConfirm(false);
    }
  };

  const qrHint = useMemo(() => {
    if (!onboardingStatus) return null;
    if (onboardingStatus.status === "scanned") return "已扫码，正在获取机器人凭据…";
    if (onboardingStatus.status === "credentials_ready") return "凭据获取成功，Bot ID 与 Secret 已自动填入";
    if (onboardingStatus.status === "expired") return "二维码已过期";
    if (onboardingStatus.status === "failed") return "快捷绑定失败";
    return "请使用企微扫码完成快捷绑定";
  }, [onboardingStatus]);

  return <section className="imPage" aria-label="IM 配置">
    <div className="imPanel">
      <header className="imModuleHeader">
        <div className="imModuleHeaderMain">
          <span className="imModuleHeaderIcon" aria-hidden="true"><MessageCircleMore size={20}/></span>
          <div className="imModuleHeaderText">
            <h2 className="imModuleHeaderTitle">IM</h2>
            <p className="imModuleHeaderSubtitle">为个人 Agent 配置消息入口，随时从常用 IM 发起任务。</p>
          </div>
        </div>
      </header>
      {error && !dialogOpen && <Alert className="imPanelAlert" type="error" showIcon message={error}/>}
      {loading ? <p className="imLoadingText">正在读取 IM 配置…</p> : <div className="imCatalogGrid">
        {channels.map((channel) => <article
          className="im-card"
          data-provider={channel.provider}
          data-enabled={channel.enabled}
          data-available={channel.available}
          key={channel.provider}
        >
          <div className="im-card-main">
            <button
              type="button"
              className="im-card-open"
              disabled={!channel.available}
              onClick={channel.provider === "wecom" ? openWeCom : undefined}
              aria-label={`配置${channel.displayName}`}
            >
              <span className="im-card-logo" aria-hidden="true">{channel.displayName.slice(0, 1)}</span>
              <span className="im-card-info">
                <strong>{channel.displayName}</strong>
                <Tag className={`im-card-tag ${statusTone(channel)}`}>{channel.available ? statusLabel(channel) : "暂不可用"}</Tag>
              </span>
            </button>
            <span className="im-card-side">
              <Switch
                checked={channel.enabled}
                aria-label={`${channel.enabled ? "停用" : "启用"}${channel.displayName}`}
                disabled={!channel.available || busy}
                onChange={() => void toggleEnabled(channel)}
              />
              <small>{channel.available ? "勾选" : "敬请期待"}</small>
            </span>
          </div>
          {channel.provider === "wecom" && channel.runtimeSessionId && <small className="im-card-session">固定会话 · {channel.runtimeSessionId}</small>}
        </article>)}
      </div>}
    </div>

    {dialogOpen && <Modal
      open
      title="注册企微助理通道"
      centered
      width={560}
      rootClassName={theme === "dark" ? "imAntDialog imAntDialogDark" : "imAntDialog"}
      className="imRegisterModal"
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      onCancel={() => setDialogOpen(false)}
      footer={[
        <Button key="cancel" autoInsertSpace={false} onClick={() => setDialogOpen(false)} disabled={busy}>取消</Button>,
        <Button key="register" type="primary" autoInsertSpace={false} onClick={() => void register()} disabled={!canRegister || busy}>{busy ? "注册中…" : "注册"}</Button>
      ]}
    >
      <div className="imRegisterBody">
        <p className="imRegisterHint">选择连接方式并输入对应凭据，以将此工作区绑定到企微助理。</p>

        <details className="imGuide">
          <summary>配置指南</summary>
          <p>可手工填写 Bot ID 与 Secret，也可使用快捷绑定接入，通过企微扫码自动获取并填入凭据。</p>
        </details>

        <div className="imModeGroup">
          <p className="imModeGroupTitle">连接方式</p>
          <Radio.Group className="imModeList" value="websocket">
            <Radio value="websocket"><span className="imModeText"><strong>WebSocket 长连接</strong><small>实时收发企微单聊消息</small></span></Radio>
            <Radio value="callback" disabled><span className="imModeText"><strong>使用 URL 回调</strong><small>暂不可选 · 敬请期待</small></span></Radio>
          </Radio.Group>
        </div>

        <Button className="imQuickBindBtn" block autoInsertSpace={false} onClick={() => void startQuickBinding()} disabled={busy} icon={busy && !onboarding ? <LoaderCircle className="uploadSpinner" size={16}/> : <QrCode size={17}/>}>
          快捷绑定接入
        </Button>

        {onboarding && <section className="imQrSection" aria-live="polite">
          <div className="imQrCodeBox"><QRCode value={onboarding.qrContent} size={164}/></div>
          <div className="imQrInfo">
            <strong>{qrHint}</strong>
            <small>二维码将在 {new Date(onboarding.expiresAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 失效</small>
            {onboardingStatus?.status === "pending" || onboardingStatus?.status === "scanned"
              ? <LoaderCircle className="uploadSpinner" size={16}/>
              : onboardingStatus?.status === "credentials_ready"
                ? <CheckCircle2 size={18}/>
                : null}
          </div>
        </section>}

        <label className="imCredentialField">
          <span>Bot ID</span>
          <Input value={botId} onChange={(event) => { setBotId(event.target.value); setOnboarding(null); setOnboardingStatus(null); }} placeholder="请输入 Bot ID" autoComplete="off"/>
        </label>
        <label className="imCredentialField">
          <span>Secret</span>
          <Input
            type={quickReady ? "text" : "password"}
            value={quickReady ? "••••••••（已通过快捷绑定获取）" : secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={wecom?.secretConfigured ? "已配置；如需更换请输入新的 Secret" : "请输入 Secret"}
            disabled={quickReady}
            autoComplete="new-password"
          />
        </label>

        {error && <Alert className="imRegisterAlert" type="error" showIcon message={error}/>}
        {wecom?.secretConfigured && <div className="imRevokeArea">
          <Button type="text" danger size="small" autoInsertSpace={false} onClick={() => void revoke()} disabled={busy}>{revokeConfirm ? "再次点击确认解除绑定" : "解除绑定"}</Button>
          <small>解除后会保留企微固定会话和全部历史。</small>
        </div>}
      </div>
    </Modal>}
  </section>;
}
