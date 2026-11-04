import { useEffect, useRef, useState } from "react";
import { Button } from "antd";
import { CloseOutlined, ExportOutlined, ReloadOutlined, ShareAltOutlined } from "@ant-design/icons";
import type { AppCenterListItem } from "../../api/client";
import { ShareAppDialog, type AppPackageSource } from "./ShareAppDialog";

interface Props {
  html: string;
  packageSource?: AppPackageSource | null;
  version: number;
  sourceSessionId?: string | null;
  sourceRunId?: string | null;
  sourceName: string;
  fallbackName: string;
  sharedApp: { app: AppCenterListItem; version: number } | null;
  previouslySharedApp: AppCenterListItem | null;
  editTargetName: string | null;
  onSharedAppChange: (value: { app: AppCenterListItem; version: number } | null) => void;
  onUpdateApp: (html: string, version: number) => void;
  onUpdateAppPackage: (packageSource: AppPackageSource, version: number) => void;
  onClose: () => void;
  onOpenCenter: () => void;
  onAppMissing?: () => void;
}

interface DesktopHtmlPreviewBridge {
  openHtmlPreview?: (html: string) => Promise<boolean>;
}

// 聊天视图右侧 HTML 预览面板（T6 轻量 antd 化：操作按钮 + 图标迁移）。
// 行为契约零改动：iframe 固定 sandbox="allow-scripts" 且不带 allow-same-origin；
// 版本对比决定「分享/更新分享/已分享/更新看板」入口形态；桌面桥接与
// objectUrl 生命周期不动。
export function HtmlPreviewPanel({
  html,
  packageSource = null,
  version,
  sourceSessionId,
  sourceRunId,
  sourceName,
  fallbackName,
  sharedApp,
  previouslySharedApp,
  editTargetName,
  onSharedAppChange,
  onUpdateApp,
  onUpdateAppPackage,
  onClose,
  onOpenCenter,
  onAppMissing
}: Props) {
  const [shareOpen, setShareOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const objectUrlRef = useRef<string | null>(null);
  const previousVersionRef = useRef(version);
  const [updatedNotice, setUpdatedNotice] = useState<string | null>(null);

  useEffect(() => {
    if (previousVersionRef.current !== version) {
      previousVersionRef.current = version;
      setUpdatedNotice(`已更新到 v${version}`);
      const timer = window.setTimeout(() => setUpdatedNotice(null), 2400);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [version]);

  // The in-memory share state knows the exact version that was published;
  // the session-level fallback only knows an app exists for this chat. Same
  // version means the panel content is already live — anything else means a
  // refresh is pending, so the entry point becomes "update", not "share"
  // (which would silently create a duplicate app).
  const contentAlreadyPublished = sharedApp !== null && sharedApp.version === version;
  const sharedAppForUpdate = sharedApp?.app ?? previouslySharedApp;

  useEffect(() => () => {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
  }, []);

  const openInNewWindow = async () => {
    const desktop = (window as Window & { coworkDesktop?: DesktopHtmlPreviewBridge })
      .coworkDesktop;
    if (desktop?.openHtmlPreview) {
      await desktop.openHtmlPreview(html);
      return;
    }
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    objectUrlRef.current = url;
    window.open(url, "_blank", "noopener,noreferrer");
  };

  return <>
    <aside className="htmlPreviewPanel" aria-label="HTML 预览面板">
      <header>
        <div>
          <strong>预览 · v{version}</strong>
          <small className="htmlPreviewSourceName" title={sourceName}>{sourceName}</small>
          {updatedNotice && <small role="status">{updatedNotice}</small>}
        </div>
        <div className="htmlPreviewActions">
          <Button
            type="text"
            size="small"
            aria-label="刷新预览"
            icon={<ReloadOutlined aria-hidden />}
            onClick={() => setRefreshKey((key) => key + 1)}
          />
          <Button
            size="small"
            autoInsertSpace={false}
            icon={<ExportOutlined aria-hidden />}
            onClick={() => void openInNewWindow()}
          >新窗口</Button>
          {editTargetName ? (
            <Button
              type="primary"
              size="small"
              autoInsertSpace={false}
              onClick={() => {
                if (packageSource) onUpdateAppPackage(packageSource, version);
                else onUpdateApp(html, version);
              }}
            >更新看板</Button>
          ) : contentAlreadyPublished ? (
            <Button type="primary" size="small" autoInsertSpace={false} icon={<ShareAltOutlined aria-hidden />} disabled>已分享</Button>
          ) : sharedAppForUpdate ? (
            <Button
              type="primary"
              size="small"
              autoInsertSpace={false}
              icon={<ReloadOutlined aria-hidden />}
              onClick={() => setShareOpen(true)}
            >更新分享</Button>
          ) : (
            <Button
              type="primary"
              size="small"
              autoInsertSpace={false}
              icon={<ShareAltOutlined aria-hidden />}
              onClick={() => setShareOpen(true)}
            >分享</Button>
          )}
          <Button
            type="text"
            size="small"
            aria-label="关闭预览"
            icon={<CloseOutlined aria-hidden />}
            onClick={onClose}
          />
        </div>
      </header>
      <iframe
        key={refreshKey}
        title={`HTML 看板预览 v${version}`}
        sandbox="allow-scripts"
        srcDoc={html}
      />
      {editTargetName && <footer>当前修改将更新看板「{editTargetName}」；发布后所有使用者刷新即可看到新版。</footer>}
      {packageSource && <footer>已识别 ZIP 看板包。当前预览仅加载根 index.html，包内多文件资源发布后完整生效。</footer>}
    </aside>
    {shareOpen && (
      <ShareAppDialog
        defaultName={fallbackName}
        html={html}
        packageSource={packageSource}
        currentVersion={version}
        sharedApp={sharedApp}
        updateApp={sharedAppForUpdate && !contentAlreadyPublished ? sharedAppForUpdate : null}
        sourceSessionId={sourceSessionId}
        sourceRunId={sourceRunId}
        onClose={() => setShareOpen(false)}
        onSaved={(app) => onSharedAppChange({ app, version })}
        onAppMissing={onAppMissing}
        onOpenCenter={() => {
          setShareOpen(false);
          onOpenCenter();
        }}
      />
    )}
  </>;
}
