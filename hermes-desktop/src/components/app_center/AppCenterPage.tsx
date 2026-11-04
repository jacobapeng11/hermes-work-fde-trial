import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Empty, Input, Tabs } from "antd";
import { AppstoreOutlined, PlusOutlined } from "@ant-design/icons";
import { LoaderCircle } from "lucide-react";
import { userApi, type AppCenterListItem } from "../../api/client";
import { AppCenterCard } from "./AppCenterCard";
import { AppCenterDeleteDialog } from "./AppCenterDeleteDialog";
import { CreateAppDialog } from "./CreateAppDialog";
import { ShareAppDialog } from "./ShareAppDialog";

interface Props {
  onEditApp: (app: AppCenterListItem) => void;
  onCreateAppSession: (prompt: string) => Promise<boolean>;
  // 2026-11-02-005：弹窗方式二选中广场看板时走 ZIP 模板注入编排（AgentWorkspace 提供）。
  onCreateFromTemplate: (app: AppCenterListItem) => Promise<boolean>;
}

interface DesktopBridge {
  openBrowser?: (url: string) => Promise<boolean>;
}

export function AppCenterPage({ onEditApp, onCreateAppSession, onCreateFromTemplate }: Props) {
  const [apps, setApps] = useState<AppCenterListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [tab, setTab] = useState<"mine" | "plaza">("mine");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [permissionApp, setPermissionApp] = useState<AppCenterListItem | null>(null);
  const [confirmDeleteApp, setConfirmDeleteApp] = useState<AppCenterListItem | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setApps(await userApi.listApps());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板区加载失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(null), 2400);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const filtered = useMemo(() => {
    const query = keyword.trim().toLocaleLowerCase();
    return apps.filter((app) => !query || app.name.toLocaleLowerCase().includes(query));
  }, [apps, keyword]);
  const mine = useMemo(() => filtered.filter((app) => app.mine), [filtered]);
  const plaza = useMemo(() => filtered.filter((app) => !app.mine), [filtered]);

  const replaceApp = (next: AppCenterListItem) => {
    setApps((current) => current.map((app) => app.id === next.id ? next : app));
  };

  const download = async (app: AppCenterListItem) => {
    if (busyId !== null) return;
    setBusyId(app.id);
    setError(null);
    try {
      const blob = await userApi.downloadAppPackage(app.id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${app.name}.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板包下载失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  const copyApp = async (app: AppCenterListItem) => {
    if (busyId !== null) return;
    setBusyId(app.id);
    setError(null);
    try {
      const created = await userApi.copyApp(app.id);
      setApps((current) => [created, ...current]);
      setTab("mine");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板复制失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  const copyLink = async (app: AppCenterListItem) => {
    if (!navigator.clipboard?.writeText) {
      setError("当前浏览器不支持复制链接");
      return;
    }
    try {
      await navigator.clipboard.writeText(app.accessUrl);
      setError(null);
      setNotice("链接已复制");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "复制链接失败，请稍后重试");
    }
  };

  const openApp = async (app: AppCenterListItem) => {
    if (busyId !== null) return;
    const desktop = (window as Window & { coworkDesktop?: DesktopBridge }).coworkDesktop;
    if (!desktop?.openBrowser) {
      window.open(app.accessUrl, "_blank", "noopener,noreferrer");
      return;
    }
    setBusyId(app.id);
    setError(null);
    try {
      const launch = await userApi.createAppBrowserLaunch(app.id);
      if (!await desktop.openBrowser(launch.launchUrl)) {
        throw new Error("无法打开系统浏览器，请稍后重试");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板打开失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  // 删除错误由 AppCenterDeleteDialog 内的 Alert 展示（对齐知识库删除弹窗），
  // 这里只负责提交与列表过滤，异常向弹窗冒泡。
  const remove = async (app: AppCenterListItem) => {
    if (busyId !== null) return;
    setBusyId(app.id);
    setError(null);
    try {
      await userApi.deleteApp(app.id);
      setApps((current) => current.filter((item) => item.id !== app.id));
    } finally {
      setBusyId(null);
    }
  };

  const renderList = (list: AppCenterListItem[], emptyTitle: string, emptyDescription: string) => <>
    {loading && <p className="appCenterState" role="status"><LoaderCircle className="uploadSpinner" size={18}/>正在加载看板…</p>}
    {!loading && error && (
      <Alert
        className="appCenterErrorAlert"
        type="error"
        showIcon
        message={error}
        action={<Button size="small" autoInsertSpace={false} onClick={() => void load()}>重试</Button>}
      />
    )}
    {!loading && !error && (
      <div className="appCenterGrid">
        {list.map((app) => <AppCenterCard
          key={app.id}
          app={app}
          busy={busyId === app.id}
          onOpen={() => void openApp(app)}
          onEdit={() => onEditApp(app)}
          onPermission={() => setPermissionApp(app)}
          onClone={() => void copyApp(app)}
          onDownload={() => void download(app)}
          onCopyLink={() => void copyLink(app)}
          onDelete={() => setConfirmDeleteApp(app)}
        />)}
        {list.length === 0 && (
          <div className="appCenterEmpty">
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyTitle}>
              <p className="appCenterEmptyHint">{emptyDescription}</p>
            </Empty>
          </div>
        )}
      </div>
    )}
  </>;

  return <section className="appCenterPage" aria-label="看板区">
    <div className="appCenterWorkspace">
      <header className="appCenterPageHeader">
        <div className="appCenterPageHeaderMain">
          <span className="appCenterHeaderIcon"><AppstoreOutlined aria-hidden/></span>
          <div className="appCenterPageHeaderText">
            <h2 className="appCenterPageHeaderTitle">看板区</h2>
            <p className="appCenterPageHeaderSubtitle">把 Agent 生成的 HTML 看板发布为可打开、可分享的内容</p>
          </div>
        </div>
        <div className="appCenterPageHeaderActions">
          <Input.Search
            className="appCenterSearchInput"
            aria-label="搜索看板"
            placeholder="搜索看板名称"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            allowClear
          />
          <Button
            type="primary"
            autoInsertSpace={false}
            icon={<PlusOutlined aria-hidden/>}
            onClick={() => setCreateOpen(true)}
          >新建看板</Button>
        </div>
      </header>
      <Tabs
        className="appCenterAntTabs"
        activeKey={tab}
        onChange={(key) => setTab(key as "mine" | "plaza")}
        items={[
          {
            key: "mine",
            label: <>我的看板 <span className="appCenterTabCount">{mine.length}</span></>,
            children: renderList(mine, "还没有自己的看板", "可在聊天生成 HTML 后分享，或去广场复制一个看板。")
          },
          {
            key: "plaza",
            label: <>看板广场 <span className="appCenterTabCount">{plaza.length}</span></>,
            children: renderList(plaza, "暂无其他用户分享的看板", "获得授权的看板会出现在这里。")
          }
        ]}
      />
      {notice && <p className="appCenterCopyNotice" role="status">{notice}</p>}
    </div>

    {createOpen && (
      <CreateAppDialog
        onClose={() => setCreateOpen(false)}
        onCreateAppSession={onCreateAppSession}
        onCreateFromTemplate={onCreateFromTemplate}
        plazaBoards={plaza}
        plazaLoading={loading}
      />
    )}
    {permissionApp && (
      <ShareAppDialog
        defaultName={permissionApp.name}
        html={null}
        currentVersion={0}
        sharedApp={null}
        permissionApp={permissionApp}
        onClose={() => setPermissionApp(null)}
        onSaved={replaceApp}
      />
    )}
    {confirmDeleteApp && (
      <AppCenterDeleteDialog
        appName={confirmDeleteApp.name}
        onClose={() => setConfirmDeleteApp(null)}
        onDelete={() => remove(confirmDeleteApp)}
      />
    )}
  </section>;
}
