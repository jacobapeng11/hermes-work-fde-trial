import { useEffect, useState } from "react";
import { ChevronRight, FolderOpen } from "lucide-react";

// A single clickable capsule inside the app header (it must live in the
// .appHeader subtree: the borderless header is the window drag surface and a
// fixed overlay's own no-drag declaration is ignored by Chromium, so the
// generic ".appHeader button" exemption is what keeps it clickable). Before a
// folder is chosen it just says "打开本地文件夹"; once chosen it shows the
// active local path and clicking swaps it. The folder picker opens at the
// last chosen folder (persisted by the main process), falling back to the
// user's home directory.
export function DesktopWorkspaceBar() {
  const [path, setPath] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void window.coworkDesktop?.getWorkspace().then((value) => setPath(value.path));
  }, []);

  async function chooseFolder() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try { setPath(await window.coworkDesktop.selectFolder()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "选择文件夹失败"); }
    finally { setBusy(false); }
  }

  return (
    <button
      type="button"
      className="desktopWorkspaceBar"
      onClick={() => void chooseFolder()}
      disabled={busy}
      title={path ? "点击更换本地文件夹" : "选择本地文件夹，Agent 生成的文件将保存在这里"}
      aria-label={path ? `本地路径 ${path}，点击更换` : "打开本地文件夹"}
    >
      <FolderOpen size={16} aria-hidden="true" />
      {path
        ? <>
          <span className="desktopWorkspaceLabel">本地路径</span>
          <span className="desktopWorkspacePath">{busy ? "选择中…" : path}</span>
          <ChevronRight size={16} aria-hidden="true" />
        </>
        : <span className="desktopWorkspaceLabel">{busy ? "选择中…" : "打开本地文件夹"}</span>}
      {error ? <span className="desktopWorkspaceError" role="alert">{error}</span> : null}
    </button>
  );
}
