import { useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Tabs } from "antd";
import { CheckOutlined, ShareAltOutlined } from "@ant-design/icons";
import type { ExpertShareScope } from "@hermes-cloud/schemas";
import { ApiError, userApi, type AppCenterListItem } from "../../api/client";
import { useOrganizationDirectory } from "../../hooks/useOrganizationDirectory";
import { usePlatformTheme } from "../../theme/platform-theme";
import { htmlDocumentTitle } from "../../utils/htmlArtifact";
import { AppScopeEditor } from "./AppScopeEditor";

const EMPTY_SCOPE: ExpertShareScope = {
  organization: false,
  departmentIds: [],
  userIds: []
};

export interface AppPackageSource {
  fileId: number;
  displayName: string;
}

interface Props {
  defaultName: string;
  html: string | null;
  packageSource?: AppPackageSource | null;
  currentVersion: number;
  sharedApp: { app: AppCenterListItem; version: number } | null;
  sourceSessionId?: string | null;
  sourceRunId?: string | null;
  permissionApp?: AppCenterListItem | null;
  updateApp?: AppCenterListItem | null;
  onClose: () => void;
  onSaved: (app: AppCenterListItem) => void;
  onOpenCenter?: () => void;
  onAppMissing?: () => void;
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `app-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function initialDescription(options: {
  html: string | null;
  permissionApp?: AppCenterListItem | null;
  sharedApp?: { app: AppCenterListItem; version: number } | null;
}): string {
  const value = options.permissionApp?.description
    ?? options.sharedApp?.app.description
    ?? htmlDocumentTitle(options.html ?? "")
    ?? "";
  return value.replace(/\s+/g, " ").trim().slice(0, 20) || "HTML 看板";
}

// 分享/更新/权限三合一弹窗（T5 迁 antd Modal，三模式语义与提交契约零改动）：
// - 分享模式：scope/link 双 Tab + 名称/描述 + AppScopeEditor + 幂等键创建
// - 更新模式：只读权限范围行 + 更新提交（ZIP 优先，HTML 兜底）
// - 权限模式：只读名称/描述 + AppScopeEditor + updateAppPermissions 写回
// APP_CENTER_APP_NOT_FOUND → onAppMissing 的 404 自愈链路不动。
export function ShareAppDialog({
  defaultName,
  html,
  packageSource = null,
  currentVersion,
  sharedApp,
  sourceSessionId,
  sourceRunId,
  permissionApp = null,
  updateApp = null,
  onClose,
  onSaved,
  onOpenCenter,
  onAppMissing
}: Props) {
  const { theme } = usePlatformTheme();
  const isPermissionMode = permissionApp !== null;
  const isUpdateMode = permissionApp === null && updateApp !== null;
  const [tab, setTab] = useState<"scope" | "link">("scope");
  const [name, setName] = useState(updateApp?.name ?? defaultName);
  const [description, setDescription] = useState(() => updateApp?.description ?? initialDescription({
    html,
    permissionApp,
    sharedApp
  }));
  const [scope, setScope] = useState<ExpertShareScope>(
    permissionApp?.scope ?? EMPTY_SCOPE
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<AppCenterListItem | null>(
    sharedApp && sharedApp.version === currentVersion ? sharedApp.app : null
  );
  const [idempotencyKey] = useState(newIdempotencyKey);
  const directoryState = useOrganizationDirectory(true);
  useEffect(() => {
    if (permissionApp?.scope) setScope(permissionApp.scope);
  }, [permissionApp?.scope]);
  useEffect(() => {
    if (permissionApp?.description) setDescription(permissionApp.description);
  }, [permissionApp?.description]);

  const sameVersion = sharedApp !== null && sharedApp.version === currentVersion;
  const effectiveResult = sameVersion ? sharedApp.app : result;
  const canSubmit = isPermissionMode
    ? Boolean(name.trim())
    : Boolean(name.trim() && description.trim() && (packageSource || html));
  const submitLabel = isPermissionMode
    ? "保存权限"
    : isUpdateMode
      ? "更新分享"
      : tab === "scope"
        ? "确认分享"
        : "生成链接";

  const loadPackageFile = async () => {
    if (!packageSource) throw new Error("未找到可分享的看板包");
    const blob = await userApi.downloadChatFile(packageSource.fileId);
    const filename = /\.zip$/i.test(packageSource.displayName)
      ? packageSource.displayName
      : `${packageSource.displayName}.zip`;
    return new File([blob], filename, { type: "application/zip" });
  };

  const submit = async () => {
    if (!canSubmit || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (isPermissionMode && permissionApp) {
        const updated = await userApi.updateAppPermissions(permissionApp.id, scope);
        setResult(updated);
        onSaved(updated);
        setNotice("权限范围已更新，被授权用户下次刷新即可看到最新状态。");
        return;
      }
      if (isUpdateMode && updateApp) {
        if (packageSource) {
          const updated = await userApi.updateAppPackage(updateApp.id, {
            file: await loadPackageFile()
          });
          setResult(updated);
          onSaved(updated);
          setNotice("看板包已更新，使用者刷新页面即可看到新版。");
          return;
        }
        if (!html) throw new Error("未找到可更新的 HTML 内容");
        const updated = await userApi.updateAppPackage(updateApp.id, {
          htmlContent: html,
          name: name.trim(),
          description: description.trim()
        });
        setResult(updated);
        onSaved(updated);
        setNotice("看板已更新，使用者刷新页面即可看到新版。");
        return;
      }
      if (packageSource) {
        const created = await userApi.uploadApp({
          name: name.trim(),
          description: description.trim(),
          shareMode: tab,
          idempotencyKey,
          scope: tab === "scope" ? scope : null,
          sourceSessionId: sourceSessionId ?? null,
          sourceRunId: sourceRunId ?? null,
          file: await loadPackageFile()
        });
        setResult(created);
        onSaved(created);
        setNotice(tab === "scope" ? "看板包已分享到看板区。" : "看板包链接已生成。");
        return;
      }
      if (!html) throw new Error("未找到可分享的 HTML 内容");
      const created = await userApi.createApp({
        name: name.trim(),
        description: description.trim(),
        htmlContent: html,
        shareMode: tab,
        idempotencyKey,
        scope: tab === "scope" ? scope : null,
        sourceSessionId: sourceSessionId ?? null,
        sourceRunId: sourceRunId ?? null
      });
      setResult(created);
      onSaved(created);
      setNotice(tab === "scope" ? "已分享到看板区。" : "链接已生成。");
    } catch (reason) {
      // 看板已在别处被删除（如看板区移除卡片）时，更新必然 404；
      // 通知外层丢弃过期的分享状态，关闭弹窗后入口会回到「分享」新建流程。
      if (
        reason instanceof ApiError &&
        reason.code === "APP_CENTER_APP_NOT_FOUND" &&
        onAppMissing
      ) {
        onAppMissing();
        setError("原看板已被删除，关闭后可重新分享");
      } else {
        setError(reason instanceof Error ? reason.message : "看板分享失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  const copyLink = async () => {
    if (!effectiveResult?.accessUrl || !navigator.clipboard?.writeText) return;
    await navigator.clipboard.writeText(effectiveResult.accessUrl);
    setNotice("链接已复制。");
  };

  return (
    <Modal
      open
      title={isPermissionMode ? "设置看板权限" : isUpdateMode ? "更新分享看板" : "分享看板"}
      centered
      width={520}
      rootClassName={theme === "dark" ? "appCenterAntDialog appCenterAntDialogDark" : "appCenterAntDialog"}
      className="appCenterShareModal"
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      onCancel={onClose}
      footer={[
        <Button key="close" autoInsertSpace={false} disabled={busy} onClick={onClose}>关闭</Button>,
        !effectiveResult && (
          <Button
            key="submit"
            type="primary"
            autoInsertSpace={false}
            loading={busy}
            disabled={!canSubmit}
            onClick={() => void submit()}
          >{submitLabel}</Button>
        ),
        effectiveResult && onOpenCenter && (
          <Button
            key="center"
            type="primary"
            autoInsertSpace={false}
            icon={<ShareAltOutlined aria-hidden />}
            onClick={onOpenCenter}
          >打开看板区</Button>
        )
      ]}
    >
      {(isPermissionMode || isUpdateMode) && (
        <div className="appCenterDialogTarget">
          <small>{isPermissionMode ? "看板权限" : "更新已分享看板"}</small>
          <strong>{isPermissionMode ? permissionApp?.name : updateApp?.name}</strong>
        </div>
      )}
      {isUpdateMode && !effectiveResult && (
        <p className="appCenterModeHint">当前预览内容与已分享版本不同，确认后更新看板；链接保持不变，使用者刷新即可看到新版。</p>
      )}
      {!isPermissionMode && !isUpdateMode && (
        <Tabs
          className="appCenterShareModeTabs"
          aria-label="分享方式"
          size="small"
          activeKey={tab}
          onChange={(key) => setTab(key as "scope" | "link")}
          items={[
            { key: "scope", label: "分享到看板区" },
            { key: "link", label: "分享链接" }
          ]}
        />
      )}

      <label className="appCenterField">
        <span>看板名称</span>
        <Input
          value={name}
          maxLength={50}
          onChange={(event) => setName(event.target.value)}
          disabled={Boolean(effectiveResult) || isPermissionMode}
        />
      </label>

      <label className="appCenterField">
        <span>看板描述 <em>{description.length}/20</em></span>
        <Input
          value={description}
          maxLength={20}
          onChange={(event) => setDescription(event.target.value.replace(/\s+/g, " ").trimStart())}
          placeholder="例如：雪花生成原理简介"
          disabled={Boolean(effectiveResult) || isPermissionMode}
        />
      </label>

      {isUpdateMode && (
        <label className="appCenterField">
          <span>权限范围</span>
          <Input value={`${updateApp?.scopeSummary ?? ""}（在看板区调整）`} readOnly disabled />
        </label>
      )}

      {(isPermissionMode || (!isUpdateMode && tab === "scope")) && (
        <div className="appCenterScopeEditor">
          <p className="appCenterScopeNotice">权限只控制哪些用户会在看板区的“看板广场”看到该看板。</p>
          <AppScopeEditor
            scope={scope}
            onChange={setScope}
            directory={directoryState.directory}
            loading={directoryState.loading}
            error={directoryState.error}
            onRetry={() => void directoryState.retry()}
          />
        </div>
      )}

      {!isPermissionMode && !isUpdateMode && tab === "link" && !effectiveResult && (
        <p className="appCenterWarning">链接使用本项目域名，可访问本项目平台的用户即可打开。</p>
      )}

      {effectiveResult && (
        <div className="appCenterShareResult">
          <span className="appCenterSuccessBadge"><CheckOutlined aria-hidden />已发布</span>
          <div className="appCenterLinkRow">
            <Input readOnly value={effectiveResult.accessUrl} onFocus={(event) => event.currentTarget.select()} aria-label="看板链接" />
            <Button autoInsertSpace={false} onClick={() => void copyLink()}>复制</Button>
          </div>
        </div>
      )}
      {notice && <p className="appCenterNotice" role="status">{notice}</p>}
      {error && <Alert className="appCenterFormError" type="error" showIcon message={error} />}
    </Modal>
  );
}
