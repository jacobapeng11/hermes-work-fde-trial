import { useEffect, useState } from "react";

type UpdateState = NonNullable<typeof window.coworkDesktop> extends { updateStatus: () => Promise<infer T> } ? T : never;

function labelFor(status: UpdateState): string {
  if (status.state === "checking") return "正在检查桌面端更新…";
  if (status.state === "available") return `发现新版本 ${status.version ?? ""}，可以下载。`;
  if (status.state === "downloading") return `正在下载新版本${status.percent == null ? "…" : `（${Math.round(status.percent)}%）`}`;
  if (status.state === "downloaded") return `新版本 ${status.version ?? ""} 已下载，重启后安装。`;
  if (status.state === "error") return "检查桌面端更新失败，可稍后重试。";
  return "";
}

export function DesktopUpdateNotice() {
  const [status, setStatus] = useState<UpdateState | null>(null);
  const [busy, setBusy] = useState(false);
  const desktop = window.coworkDesktop;

  useEffect(() => {
    if (!desktop?.updateStatus) return;
    let active = true;
    void desktop.updateStatus().then((value) => { if (active) setStatus(value); });
    const unsubscribe = desktop.onUpdateStatus((value) => { if (active) setStatus(value); });
    return () => { active = false; unsubscribe(); };
  }, [desktop]);

  // This notice mounts inside AuthGate, so mounting means the user is logged
  // in and the session cookies exist — the update directory lives behind the
  // platform auth, so the first check can only happen here, not at app start.
  // Non-interactive: an empty or unreachable update directory stays silent.
  useEffect(() => {
    if (!desktop?.checkForUpdates) return;
    const timer = window.setTimeout(() => { void desktop.checkForUpdates({ interactive: false }); }, 3000);
    return () => window.clearTimeout(timer);
  }, [desktop]);

  if (!status || status.state === "disabled" || status.state === "not-available") return null;

  async function check() {
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

  return (
    <div className="desktopUpdateNotice" role="status">
      <span>{labelFor(status)}</span>
      {status.state === "available" ? <button type="button" onClick={() => void download()} disabled={busy}>下载更新</button> : null}
      {status.state === "downloaded" ? <button type="button" onClick={install} disabled={busy}>重启更新</button> : null}
      {status.state === "error" ? <button type="button" onClick={() => void check()} disabled={busy}>重试</button> : null}
    </div>
  );
}
