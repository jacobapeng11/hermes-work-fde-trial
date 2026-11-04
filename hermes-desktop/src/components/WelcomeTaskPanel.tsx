import { useEffect, useRef, useState } from "react";
import type { AppCenterListItem } from "../api/client";
import type { TaskPromptConfigDocument } from "@hermes-cloud/schemas";
import { userApi } from "../api/client";
import { WelcomeBoardGallery } from "./welcome/WelcomeBoardGallery";
import { WelcomeGuideCard } from "./welcome/WelcomeGuideCard";
import { WelcomeOfficeTemplates } from "./welcome/WelcomeOfficeTemplates";
import "./WelcomeTaskPanel.css";

interface Props {
  ownerIdentity?: string | null;
  onTaskPreview: (prompt: string | null) => void;
  onTaskCommit: (prompt: string) => void;
  onOpenBoard: (board: AppCenterListItem) => void;
  onViewAllBoards?: () => void;
}

const welcomeBoardLimit = 5;
type ConfigLoadState = "loading" | "ready" | "failed";

// 空会话首页（demo「ChatHome」）：问候语 + 模式 Segmented（task-prompt-config tabs，
// 配置驱动）+ 双模式视图。showBoardGallery=true 的 Tab 渲染看板视图（案例广场），
// 否则渲染办公视图（推荐办公模板）。Tab/主题/典型问题与预览、固化契约不变；
// 新增 Tag 单选选中态（子组件内维护，切换时随 key 重挂载重置）。
export function WelcomeTaskPanel({ ownerIdentity = null, onTaskPreview, onTaskCommit, onOpenBoard, onViewAllBoards }: Props) {
  const [config, setConfig] = useState<TaskPromptConfigDocument | null>(null);
  const [configLoadState, setConfigLoadState] = useState<ConfigLoadState>("loading");
  const [activeTabLabel, setActiveTabLabel] = useState<string | null>(null);
  const [boards, setBoards] = useState<AppCenterListItem[] | null>(null);
  const previewPromptRef = useRef<string | null>(null);
  const taskPreviewRef = useRef(onTaskPreview);

  useEffect(() => {
    taskPreviewRef.current = onTaskPreview;
  }, [onTaskPreview]);

  useEffect(() => () => taskPreviewRef.current(null), []);

  useEffect(() => {
    let active = true;
    userApi.taskPromptConfig()
      .then((state) => {
        if (!active) return;
        setConfig(state.config ?? null);
        setConfigLoadState("ready");
      })
      .catch(() => {
        if (!active) return;
        setConfig(null);
        setConfigLoadState("failed");
      });
    return () => {
      active = false;
    };
  }, []);

  const tabs = config?.tabs ?? [];
  const activeTab = tabs.find((tab) => tab.label === activeTabLabel)
    ?? tabs.find((tab) => tab.isDefault)
    ?? tabs[0];
  const showBoardGallery = activeTab?.showBoardGallery ?? false;
  const shouldLoadBoards = tabs.some((tab) => tab.showBoardGallery);

  useEffect(() => {
    setActiveTabLabel(activeTab?.label ?? null);
  }, [activeTab?.label]);

  useEffect(() => {
    if (!shouldLoadBoards) return;
    let active = true;
    userApi.listApps()
      .then((apps) => {
        if (!active) return;
        setBoards(apps.filter((app) => !app.mine).slice(0, welcomeBoardLimit));
      })
      .catch(() => {
        if (active) setBoards([]);
      });
    return () => {
      active = false;
    };
  }, [shouldLoadBoards]);

  const setPreviewPrompt = (prompt: string | null) => {
    if (previewPromptRef.current === prompt) return;
    previewPromptRef.current = prompt;
    onTaskPreview(prompt);
  };

  const switchMode = (label: string) => {
    setPreviewPrompt(null);
    setActiveTabLabel(label);
  };

  return <section className="chatHomePanel" aria-label="任务引导">
    <h2>{ownerIdentity ? `${ownerIdentity}，您好 👋 我是您的数字分身` : "您好 👋 我是您的数字分身"}</h2>
    <div className="chatHomeModeSwitch" role="tablist" aria-label="任务类型">
      {tabs.map((tab) => (
        <button
          type="button"
          role="tab"
          key={tab.label}
          aria-selected={tab.label === activeTab?.label}
          className={tab.label === activeTab?.label ? "is-active" : undefined}
          onClick={() => switchMode(tab.label)}
        >{tab.label}</button>
      ))}
    </div>
    {configLoadState === "loading" && <p className="chatHomeConfigState" role="status">正在加载任务提示…</p>}
    {configLoadState === "failed" && <p className="chatHomeConfigState" role="status">任务提示配置暂不可用，可直接输入任务。</p>}
    {configLoadState === "ready" && tabs.length === 0 && (
      <p className="chatHomeConfigState">尚未配置任务提示，可直接输入任务。</p>
    )}
    {activeTab && (
      <div className="chatHomeView" key={activeTab.label}>
        <WelcomeGuideCard
          tab={activeTab}
          iconKind={showBoardGallery ? "board" : "office"}
          onPreview={setPreviewPrompt}
          onCommit={(detail) => {
            setPreviewPrompt(null);
            onTaskCommit(detail);
          }}
        />
        {showBoardGallery
          ? <WelcomeBoardGallery boards={boards} onOpenBoard={onOpenBoard} onViewAll={onViewAllBoards} />
          : <WelcomeOfficeTemplates onCommit={(detail) => {
              setPreviewPrompt(null);
              onTaskCommit(detail);
            }} />}
      </div>
    )}
  </section>;
}
