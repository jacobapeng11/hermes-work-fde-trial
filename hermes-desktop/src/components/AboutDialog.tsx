import { useEffect, useState } from "react";
import { X } from "lucide-react";

type UpdateState = NonNullable<typeof window.coworkDesktop> extends { updateStatus: () => Promise<infer T> } ? T : never;

type UpdateView = { label: string; tone: "muted" | "ok" | "error" };

function viewFor(status: UpdateState): UpdateView | null {
  if (status.state === "disabled") return { label: "当前构建未配置更新源", tone: "muted" };
  if (status.state === "checking") return { label: "正在检查更新…", tone: "muted" };
  if (status.state === "available") return { label: `发现新版本 v${status.version ?? ""}`, tone: "ok" };
  if (status.state === "downloading") {
    return { label: `正在下载${status.percent == null ? "…" : `（${Math.round(status.percent)}%）`}`, tone: "muted" };
  }
  if (status.state === "downloaded") return { label: `v${status.version ?? ""} 已下载，重启后安装`, tone: "ok" };
  if (status.state === "error") return { label: status.error ?? "检查更新失败", tone: "error" };
  if (status.state === "not-available") return { label: "已是最新版本", tone: "ok" };
  return null;
}

// About dialog (avatar menu → 关于): brand + version header, a short product
// introduction, and a self-contained update flow (check → download with
// progress → restart & install) that reuses the updater IPC, plus the
// copyright line. The version rides on updateStatus because the sandboxed
// renderer cannot read app.getVersion() itself.
export function AboutDialog({ onClose }: { onClose: () => void }) {
  const [version, setVersion] = useState<string | null>(null);
  const [status, setStatus] = useState<UpdateState | null>(null);
  const [busy, setBusy] = useState(false);
  const desktop = window.coworkDesktop;

  useEffect(() => {
    if (!desktop?.updateStatus) return;
    let active = true;
    void desktop.updateStatus().then((value) => {
      if (!active) return;
      setVersion(value.currentVersion);
      setStatus(value);
    });
    const unsubscribe = desktop.onUpdateStatus((value) => {
      if (active) setStatus(value);
    });
    return () => { active = false; unsubscribe(); };
  }, [desktop]);

  async function checkForUpdates() {
    if (busy) return;
    setBusy(true);
    try { setStatus(await desktop.checkForUpdates()); }
    finally { setBusy(false); }
  }

  async function download() {
    setBusy(true);
    try { setStatus(await desktop.downloadUpdate()); }
    finally { setBusy(false); }
  }

  function install() { setBusy(true); void desktop.installUpdate(); }

  const view = status ? viewFor(status) : null;
  return <div className="profileDialogBackdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="profileDialog aboutDialog" role="dialog" aria-modal="true" aria-label="关于 Hermes Work" onMouseDown={(event) => event.stopPropagation()}>
      <header className="aboutHeader">
        <div className="aboutBrand"><strong>Hermes Work</strong><span>{version ? `v${version}` : "版本获取中…"}</span></div>
        <button type="button" className="profileDialogClose" aria-label="关闭关于" onClick={onClose}><X size={18}/></button>
      </header>
      <div className="profileDialogBody aboutDialogBody">
        <p className="aboutIntro">
          Hermes Work 是面向Hermes内部的企业级 AI Agent 工作台：内置Hermes私有知识库，任务产物优先保存到本地工作目录；桌面终端关闭后，云端 Agent 仍持续运行，提供 24 小时不间断服务。
        </p>
        <p className="aboutIntro">
          支持接入企业微信，群聊机器人与 Agent API 能力可向管理员申请开通；自定义的专家智能体可共享给公司内授权人员使用。
        </p>
        <div className="aboutUpdate">
          {status?.state === "available"
            ? <button type="button" className="primary" onClick={() => void download()} disabled={busy}>升级到 v{status.version}</button>
            : null}
          {status?.state === "downloaded"
            ? <button type="button" className="primary" onClick={install} disabled={busy}>重启更新</button>
            : null}
          <button type="button" onClick={() => void checkForUpdates()} disabled={busy || status?.state === "downloading"}>
            {status?.state === "checking" ? "检查中…" : "检查更新"}
          </button>
          {view && <p role="status" data-tone={view.tone}>{view.label}</p>}
        </div>
      </div>
      <footer className="profileDialogFooter aboutDialogFooter">
        <p>Copyright © 2026 Hermes Cloud Platform. 保留所有权利。</p>
        <button type="button" onClick={onClose}>关闭</button>
      </footer>
    </section>
  </div>;
}
