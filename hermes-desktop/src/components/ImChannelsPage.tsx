import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  LoaderCircle,
  MessageCircleMore,
  QrCode,
  ShieldCheck,
  X
} from "lucide-react";
import QRCode from "react-qr-code";
import type {
  ImChannelCard,
  ImQrOnboardingCreated,
  ImQrOnboardingStatus
} from "@hermes-cloud/schemas";
import { userApi } from "../api/client";


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

  return <section className="imChannelsPage" aria-label="IM 配置">
    <header>
      <span className="imPageMark"><MessageCircleMore size={22}/></span>
      <div>
        <h1>IM</h1>
        <p>为个人 Agent 配置消息入口，随时从常用 IM 发起任务。</p>
      </div>
    </header>
    {error && !dialogOpen && <p className="imError" role="alert">{error}</p>}
    {loading ? <p className="muted">正在读取 IM 配置…</p> : <div className="imChannelGrid">
      {channels.map((channel) => <article
        className="imChannelCard"
        data-provider={channel.provider}
        data-enabled={channel.enabled}
        data-available={channel.available}
        key={channel.provider}
      >
        <button
          type="button"
          className="imChannelOpen"
          disabled={!channel.available}
          onClick={channel.provider === "wecom" ? openWeCom : undefined}
          aria-label={`配置${channel.displayName}`}
        >
          <span className="imProviderLogo">{channel.displayName.slice(0, 1)}</span>
          <span>
            <strong>{channel.displayName}</strong>
            <small>{channel.available ? statusLabel(channel) : "暂不可用"}</small>
          </span>
        </button>
        <button
          type="button"
          className="imChannelCheck"
          role="switch"
          aria-checked={channel.enabled}
          aria-label={`${channel.enabled ? "停用" : "启用"}${channel.displayName}`}
          disabled={!channel.available || busy}
          onClick={() => void toggleEnabled(channel)}
        >
          <span>{channel.enabled && <CheckCircle2 size={14}/>}</span>
          {channel.available ? "勾选" : "敬请期待"}
        </button>
        {channel.provider === "wecom" && channel.runtimeSessionId && <small className="imFixedSession">固定会话 · {channel.runtimeSessionId}</small>}
      </article>)}
    </div>}

    {dialogOpen && <div className="modalBackdrop centered imDialogBackdrop" role="presentation" onClick={() => !busy && setDialogOpen(false)}>
      <section className="imWeComDialog" role="dialog" aria-modal="true" aria-labelledby="wecom-register-title" onClick={(event) => event.stopPropagation()}>
        <button className="imDialogClose" type="button" aria-label="关闭" onClick={() => setDialogOpen(false)} disabled={busy}><X size={18}/></button>
        <header>
          <span className="imDialogIcon"><ShieldCheck size={22}/></span>
          <div>
            <h2 id="wecom-register-title">注册企微助理通道</h2>
            <p>选择连接方式并输入对应凭据，以将此工作区绑定到企微助理。</p>
          </div>
        </header>

        <details className="imConfigGuide">
          <summary>配置指南</summary>
          <p>可手工填写 Bot ID 与 Secret，也可使用快捷绑定接入，通过企微扫码自动获取并填入凭据。</p>
        </details>

        <fieldset className="imConnectionModes">
          <legend>连接方式</legend>
          <label data-selected="true"><input type="radio" name="wecom-mode" checked readOnly/><span><strong>WebSocket 长连接</strong><small>实时收发企微单聊消息</small></span></label>
          <label data-disabled="true"><input type="radio" name="wecom-mode" disabled/><span><strong>使用 URL 回调</strong><small>暂不可选 · 敬请期待</small></span></label>
        </fieldset>

        <button className="imQuickBind" type="button" onClick={() => void startQuickBinding()} disabled={busy}>
          {busy && !onboarding ? <LoaderCircle className="uploadSpinner" size={16}/> : <QrCode size={17}/>}
          快捷绑定接入
        </button>

        {onboarding && <section className="imQrPanel" aria-live="polite">
          <div className="imQrCode"><QRCode value={onboarding.qrContent} size={164}/></div>
          <div>
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
          <input value={botId} onChange={(event) => { setBotId(event.target.value); setOnboarding(null); setOnboardingStatus(null); }} placeholder="请输入 Bot ID" autoComplete="off"/>
        </label>
        <label className="imCredentialField">
          <span>Secret</span>
          <input
            type={quickReady ? "text" : "password"}
            value={quickReady ? "••••••••（已通过快捷绑定获取）" : secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={wecom?.secretConfigured ? "已配置；如需更换请输入新的 Secret" : "请输入 Secret"}
            disabled={quickReady}
            autoComplete="new-password"
          />
        </label>

        {error && <p className="imError" role="alert">{error}</p>}
        {wecom?.secretConfigured && <div className="imRevokeArea">
          <button type="button" onClick={() => void revoke()} disabled={busy}>{revokeConfirm ? "再次点击确认解除绑定" : "解除绑定"}</button>
          <small>解除后会保留企微固定会话和全部历史。</small>
        </div>}
        <footer>
          <button type="button" onClick={() => setDialogOpen(false)} disabled={busy}>取消</button>
          <button type="button" className="primary" onClick={() => void register()} disabled={!canRegister || busy}>{busy ? "注册中…" : "注册"}</button>
        </footer>
      </section>
    </div>}
  </section>;
}
