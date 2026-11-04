import { useEffect, useLayoutEffect, useRef, useState, type ComponentType, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Check, Circle, LoaderCircle, MessageCircleMore, MoreHorizontal, Pencil, Sparkles, Trash2, X } from "lucide-react";
import { displayTaskTitle } from "../taskPresentation";
import { defaultUserProfile, type UserProfile } from "../profile";
import { selectCollapsedSessions } from "./sessionListLayout";
import { UserMenu } from "./UserMenu";
import "./session-delete-confirmation.css";

interface SidebarSession {
  runtimeSessionId: string;
  title?: string | null;
  sourceType?: string;
  messageCount: number;
  agentDisplayName?: string | null;
  agentProfileKind?: "default" | "expert" | null;
  isReadOnly?: boolean;
  isImmutable?: boolean;
  originLabel?: string | null;
  status?: string;
}
const isReadOnlySession = (session: SidebarSession) => (
  Boolean(session.isReadOnly || session.status === "expert_deleted")
);
// Read-only because the user is not the owner, NOT because the expert was
// deleted. Non-owner sessions cannot be managed (renamed/deleted), but an
// expert-deleted session can still be deleted by its owner to clean up.
const isNonOwnerReadOnlySession = (session: SidebarSession) => (
  Boolean(session.isReadOnly && session.status !== "expert_deleted")
);
const isImmutableSession = (session: SidebarSession) => (
  Boolean(
    session.isImmutable
    || session.sourceType === "wecom"
    || session.sourceType === "openapi"
  )
);
const isDeletingSession = (session: SidebarSession) => session.status === "deleting";
const isDeletedExpertSession = (session: SidebarSession) => (
  session.status === "expert_deleted"
);
interface SidebarView { id: string; label: string; icon: ComponentType<{ size?: string | number }>; }
interface SessionMenuState {
  id: string;
  left: number;
  top: number;
  anchorRight: number;
  anchorTop: number;
  anchorBottom: number;
  mode: "actions" | "confirm-delete";
}
interface AppSidebarProps {
  view: string;
  views: readonly SidebarView[];
  onViewChange: (view: string) => void;
  sessions: SidebarSession[];
  activeSessionId: string | null;
  onSessionChange: (id: string) => void;
  onCreateSession: () => void;
  onRenameSession?: (id: string, title: string) => Promise<boolean>;
  onDeleteSession?: (id: string) => Promise<void>;
  menuOpen?: boolean;
  onMenuToggle?: () => void;
  onThemeToggle?: () => void;
  theme?: "dark" | "light";
  profile?: UserProfile;
  onProfileOpen?: () => void;
  onImOpen?: () => void;
  onMcpOpen?: () => void;
  canManageApplicationAgents?: boolean;
  onApplicationAgentsOpen?: () => void;
  isAdmin?: boolean;
  onAdminOpen?: () => void;
  applicationAgentMode?: boolean;
  applicationAgentDebugMode?: boolean;
  sessionSectionLabel?: string;
  collapsibleSessions?: boolean;
  sessionActivities?: Record<string, {
    state: "idle" | "running" | "completed-unseen" | "error-unseen";
    runtimeRunId?: string;
  }>;
}

export function AppSidebar({ view, views, onViewChange, sessions, activeSessionId, onSessionChange, onCreateSession, onRenameSession, onDeleteSession, menuOpen = false, onMenuToggle, onThemeToggle, theme = "dark", profile = defaultUserProfile(), onProfileOpen, onImOpen, onMcpOpen, canManageApplicationAgents = false, onApplicationAgentsOpen, isAdmin = false, onAdminOpen, applicationAgentMode = false, applicationAgentDebugMode = false, sessionSectionLabel = "最近任务", collapsibleSessions = false, sessionActivities = {} }: AppSidebarProps) {
  const sessionEntityLabel = applicationAgentMode ? "应用 Agent" : "任务";
  const sessionActionLabel = applicationAgentMode
    ? "应用 Agent 操作"
    : "任务操作";
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [sessionMenu, setSessionMenu] = useState<SessionMenuState | null>(null);
  const [deleteSaving, setDeleteSaving] = useState(false);
  const [deleteSlow, setDeleteSlow] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteSubmissionLockRef = useRef(false);
  const deleteSlowTimerRef = useRef<number | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement | null>(null);
  const sessionSectionRef = useRef<HTMLElement | null>(null);
  const sessionHeadRef = useRef<HTMLDivElement | null>(null);
  const sessionListRef = useRef<HTMLDivElement | null>(null);
  const sessionRowHeightsRef = useRef(new Map<string, number>());
  const [sessionsExpanded, setSessionsExpanded] = useState(false);
  const [sessionLayoutMeasured, setSessionLayoutMeasured] = useState(false);
  const [collapsedLayout, setCollapsedLayout] = useState({
    overflow: false,
    visibleIds: sessions.map((session) => session.runtimeSessionId)
  });

  const measureSessionLayout = () => {
    if (!collapsibleSessions || sessionsExpanded) return;
    const section = sessionSectionRef.current;
    const list = sessionListRef.current;
    if (!section || !list) return;
    for (const row of list.querySelectorAll<HTMLElement>(".sessionRow[data-session-row-id]")) {
      const id = row.dataset.sessionRowId;
      if (!id) continue;
      const height = row.getBoundingClientRect().height || row.offsetHeight;
      if (height > 0) sessionRowHeightsRef.current.set(id, height);
    }
    const availableHeight = section.clientHeight - (sessionHeadRef.current?.offsetHeight ?? 0);
    if (availableHeight <= 0) return;
    const next = selectCollapsedSessions(
      sessions.map((session) => ({
        id: session.runtimeSessionId,
        height: sessionRowHeightsRef.current.get(session.runtimeSessionId) ?? 34,
        priority: session.runtimeSessionId === activeSessionId
          || sessionActivities[session.runtimeSessionId]?.state !== undefined
            && sessionActivities[session.runtimeSessionId]?.state !== "idle"
      })),
      availableHeight
    );
    setSessionLayoutMeasured(true);
    setCollapsedLayout((current) => (
      current.overflow === next.overflow
      && current.visibleIds.join("\u0000") === next.visibleIds.join("\u0000")
        ? current
        : next
    ));
  };

  const hasUnmeasuredSession = collapsibleSessions && sessions.some(
    (session) => !sessionRowHeightsRef.current.has(session.runtimeSessionId)
  );
  const visibleSessions = !collapsibleSessions || sessionsExpanded || hasUnmeasuredSession
    ? sessions
    : sessions.filter((session) => collapsedLayout.visibleIds.includes(session.runtimeSessionId));

  useLayoutEffect(() => {
    measureSessionLayout();
  }, [sessions, activeSessionId, collapsibleSessions, sessionsExpanded, sessionActivities]);

  useEffect(() => {
    if (!collapsibleSessions) return;
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measureSessionLayout);
      return () => window.removeEventListener("resize", measureSessionLayout);
    }
    const observer = new ResizeObserver(measureSessionLayout);
    if (sessionSectionRef.current) observer.observe(sessionSectionRef.current);
    return () => observer.disconnect();
  }, [collapsibleSessions, sessionsExpanded, sessions, activeSessionId, sessionActivities]);

  const cancelDeleteSlowTimer = () => {
    if (deleteSlowTimerRef.current === null) return;
    window.clearTimeout(deleteSlowTimerRef.current);
    deleteSlowTimerRef.current = null;
  };

  useEffect(() => () => {
    cancelDeleteSlowTimer();
    deleteSubmissionLockRef.current = false;
  }, []);

  useEffect(() => {
    if (!sessionMenu) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (deleteSaving) return;
      const target = event.target as HTMLElement;
      const options = target.closest<HTMLButtonElement>(".sessionOptions");
      if (!sessionMenuRef.current?.contains(target) && options?.dataset.sessionId !== sessionMenu.id) setSessionMenu(null);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !deleteSaving) setSessionMenu(null);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [deleteSaving, sessionMenu]);

  const beginRename = (session: SidebarSession) => {
    if (isImmutableSession(session)) return;
    setSessionMenu(null);
    setDeleteError(null);
    setRenamingSessionId(session.runtimeSessionId);
    setRenameValue(displayTaskTitle(session.title));
  };
  const cancelRename = () => {
    if (renameSaving) return;
    setRenamingSessionId(null);
    setRenameValue("");
  };
  const finishRename = async (session: SidebarSession) => {
    if (isImmutableSession(session)) return;
    const nextTitle = renameValue.trim().replace(/\s+/g, " ");
    if (!nextTitle || nextTitle.length > 100 || renameSaving) return;
    if (nextTitle === displayTaskTitle(session.title)) {
      cancelRename();
      return;
    }
    setRenameSaving(true);
    try {
      if (await onRenameSession?.(session.runtimeSessionId, nextTitle)) cancelRename();
    } finally {
      setRenameSaving(false);
    }
  };
  const handleRenameKey = (event: KeyboardEvent<HTMLInputElement>, session: SidebarSession) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void finishRename(session);
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelRename();
    }
  };
  const positionSessionPopover = (
    anchor: Pick<SessionMenuState, "anchorRight" | "anchorTop" | "anchorBottom">,
    width: number,
    height: number
  ) => {
    const left = Math.max(8, Math.min(anchor.anchorRight - width, window.innerWidth - width - 8));
    const preferredTop = anchor.anchorBottom + 4;
    const top = preferredTop + height <= window.innerHeight - 8
      ? preferredTop
      : Math.max(8, anchor.anchorTop - height - 4);
    return { left, top };
  };
  const actionCountForSession = (session: SidebarSession) => (
    Number(Boolean(onRenameSession) && !isReadOnlySession(session) && !isImmutableSession(session))
    + Number(Boolean(onDeleteSession) && !isNonOwnerReadOnlySession(session) && !isImmutableSession(session) && !isDeletingSession(session))
  );
  const showSessionMenu = (session: SidebarSession, button: HTMLButtonElement, toggle = false) => {
    if (deleteSaving) return;
    if (toggle && sessionMenu?.id === session.runtimeSessionId) {
      setSessionMenu(null);
      setDeleteError(null);
      return;
    }
    const rect = button.getBoundingClientRect();
    const menuWidth = 142;
    const actionCount = actionCountForSession(session);
    const menuHeight = 10 + actionCount * 32;
    const anchor = { anchorRight: rect.right, anchorTop: rect.top, anchorBottom: rect.bottom };
    const position = positionSessionPopover(anchor, menuWidth, menuHeight);
    setDeleteError(null);
    setSessionMenu({ id: session.runtimeSessionId, ...anchor, ...position, mode: "actions" });
  };
  const menuSession = sessionMenu
    ? sessions.find((session) => session.runtimeSessionId === sessionMenu.id)
    : undefined;
  const beginDeleteConfirmation = () => {
    if (!sessionMenu || deleteSaving || !menuSession || isImmutableSession(menuSession) || isDeletingSession(menuSession)) return;
    const position = positionSessionPopover(
      sessionMenu,
      220,
      applicationAgentMode ? 196 : 154
    );
    setDeleteError(null);
    setDeleteSlow(false);
    setSessionMenu({ ...sessionMenu, ...position, mode: "confirm-delete" });
  };
  const cancelDeleteConfirmation = () => {
    if (!sessionMenu || deleteSaving) return;
    const actionCount = menuSession ? actionCountForSession(menuSession) : 0;
    const position = positionSessionPopover(sessionMenu, 142, 10 + actionCount * 32);
    setDeleteError(null);
    setDeleteSlow(false);
    setSessionMenu({ ...sessionMenu, ...position, mode: "actions" });
  };
  const confirmDeleteSession = async () => {
    if (
      !menuSession
      || !onDeleteSession
      || isImmutableSession(menuSession)
      || isDeletingSession(menuSession)
      || deleteSaving
      || deleteSubmissionLockRef.current
    ) return;
    deleteSubmissionLockRef.current = true;
    setDeleteSaving(true);
    setDeleteSlow(false);
    setDeleteError(null);
    cancelDeleteSlowTimer();
    if (applicationAgentMode) {
      deleteSlowTimerRef.current = window.setTimeout(() => {
        deleteSlowTimerRef.current = null;
        setDeleteSlow(true);
      }, 2000);
    }
    try {
      await onDeleteSession(menuSession.runtimeSessionId);
      setSessionMenu(null);
    } catch (reason) {
      setDeleteError(reason instanceof Error && reason.message ? reason.message : "删除失败，请重试。");
    } finally {
      cancelDeleteSlowTimer();
      setDeleteSlow(false);
      deleteSubmissionLockRef.current = false;
      setDeleteSaving(false);
    }
  };

  const sessionIndicator = (session: SidebarSession, active: boolean) => {
    const activity = sessionActivities[session.runtimeSessionId];
    if (activity?.state === "running") {
      return <LoaderCircle className="sessionRunSpinner" size={11} aria-hidden="true" data-status="执行中"/>;
    }
    if (activity?.state === "completed-unseen") {
      return <span className="sessionCompletedDot" aria-hidden="true" data-status="已完成，待查看"/>;
    }
    if (activity?.state === "error-unseen") {
      return <span className="sessionErrorDot" aria-hidden="true" data-status="执行出错，待查看"/>;
    }
    return <Circle className="sessionStatusDot" data-active={active} fill={active ? "currentColor" : "none"} size={7}/>;
  };

  return <><aside className="primarySidebar" aria-label="主导航">
    <div className="brand"><strong>Hermes Work</strong></div>
    <nav>{views.map((item) => <div className="navRow" key={item.id}>
      <button type="button" className="navButton" aria-label={item.label} data-active={view === item.id} onClick={() => onViewChange(item.id)}><item.icon size={16}/><span>{item.label}</span></button>
      {(item.id === "chat" || item.id === "application-agents") && <button type="button" className="navCreateButton" aria-label={applicationAgentMode ? "新建应用 Agent" : "新建任务"} title={applicationAgentMode ? "新建应用 Agent" : "新建任务"} onClick={onCreateSession}>＋</button>}
    </div>)}</nav>
    <section className="embeddedSessions" aria-label={sessionSectionLabel} ref={sessionSectionRef}>
      <div className="sessionHead" ref={sessionHeadRef}><span>{sessionSectionLabel}</span></div>
      <div className="sessionListViewport">
      <div className="sessionList" data-expanded={sessionsExpanded} data-fallback-scroll={collapsibleSessions && !sessionLayoutMeasured} ref={sessionListRef}>{sessions.length ? visibleSessions.map((session) => {
        const title = displayTaskTitle(session.title);
        const renaming = renamingSessionId === session.runtimeSessionId;
        const active = session.runtimeSessionId === activeSessionId;
        const sessionMenuOpen = sessionMenu?.id === session.runtimeSessionId;
        return <div className="sessionRow" data-menu-open={sessionMenuOpen} data-session-row-id={session.runtimeSessionId} key={session.runtimeSessionId}>
          {renaming ? <div className="sessionRenameEditor">
            {sessionIndicator(session, active)}
            <input autoFocus maxLength={100} aria-label={`重命名任务：${title}`} value={renameValue} disabled={renameSaving} onChange={(event) => setRenameValue(event.target.value)} onKeyDown={(event) => handleRenameKey(event, session)}/>
            <button type="button" aria-label="保存任务标题" disabled={!renameValue.trim() || renameSaving} onClick={() => void finishRename(session)}><Check size={13}/></button>
            <button type="button" aria-label="取消重命名" disabled={renameSaving} onClick={cancelRename}><X size={13}/></button>
          </div> : <>
            <button className="sessionSelect" data-active={active} title={session.agentProfileKind === "expert" ? `${title} · ${session.agentDisplayName || "专家"}` : title} onClick={() => { setSessionMenu(null); onViewChange("chat"); onSessionChange(session.runtimeSessionId); }}>{sessionIndicator(session, active)}<span>{title}{(session.sourceType === "wecom" || session.sourceType === "openapi") && <small className="sessionAgentLabel"><MessageCircleMore size={10}/>{session.originLabel ? `${session.originLabel} · 固定会话` : session.sourceType === "openapi" ? "API · 固定会话" : "企微 · 固定会话"}</small>}{session.agentProfileKind === "expert" && <small className="sessionAgentLabel"><Sparkles size={10}/>{session.agentDisplayName || "专家"}</small>}{isDeletedExpertSession(session) && <small className="sessionAgentLabel">专家已删除</small>}</span></button>
            {actionCountForSession(session) > 0 && <button className="sessionOptions" data-session-id={session.runtimeSessionId} type="button" aria-label={`${sessionActionLabel}：${title}`} aria-expanded={sessionMenuOpen} aria-haspopup="menu" title={sessionActionLabel} onClick={(event) => showSessionMenu(session, event.currentTarget, true)}><MoreHorizontal size={15}/></button>}
          </>}
        </div>;
      }) : <p>{applicationAgentMode ? "暂无应用 Agent" : "暂无任务"}</p>}</div>
      </div>
      {collapsibleSessions && collapsedLayout.overflow && !sessionsExpanded && !hasUnmeasuredSession && <button type="button" className="sessionExpandButton" onClick={() => setSessionsExpanded(true)}>点击展开更多</button>}
    </section>
    <UserMenu profile={profile} menuOpen={menuOpen} onMenuToggle={onMenuToggle} theme={theme} onThemeToggle={onThemeToggle} onProfileOpen={onProfileOpen} onImOpen={onImOpen} onMcpOpen={onMcpOpen} canManageApplicationAgents={canManageApplicationAgents} onApplicationAgentsOpen={onApplicationAgentsOpen} applicationAgentMode={applicationAgentMode} applicationAgentDebugMode={applicationAgentDebugMode} isAdmin={isAdmin} onAdminOpen={onAdminOpen}/>
  </aside>{sessionMenu && menuSession && createPortal(sessionMenu.mode === "actions"
    ? <div className="sessionActionsMenu" ref={sessionMenuRef} role="menu" aria-label={`${sessionActionLabel}：${displayTaskTitle(menuSession.title)}`} style={{ left: sessionMenu.left, top: sessionMenu.top }}>
      {onRenameSession && !isReadOnlySession(menuSession) && !isImmutableSession(menuSession) && <button type="button" role="menuitem" aria-label={`重命名任务：${displayTaskTitle(menuSession.title)}`} onClick={() => beginRename(menuSession)}><Pencil size={14}/><span>重命名</span></button>}
      {onDeleteSession && !isNonOwnerReadOnlySession(menuSession) && !isImmutableSession(menuSession) && !isDeletingSession(menuSession) && <button type="button" role="menuitem" className="danger" aria-label={`删除${sessionEntityLabel}：${displayTaskTitle(menuSession.title)}`} onClick={beginDeleteConfirmation}><Trash2 size={14}/><span>删除</span></button>}
    </div>
    : <section className="sessionDeleteConfirm" ref={sessionMenuRef} role="dialog" aria-label={`确认删除${sessionEntityLabel}：${displayTaskTitle(menuSession.title)}`} style={{ left: sessionMenu.left, top: sessionMenu.top }}>
      <strong>删除“{displayTaskTitle(menuSession.title)}”？</strong>
      <p aria-live="polite">{deleteSlow
        ? "正在停止 Runtime 并清理资源，通常需要约 30 秒…"
        : applicationAgentMode
          ? "将永久删除配置、调用方、会话、记忆以及全部 Runtime 资源，且无法恢复。"
          : "将删除该任务的全部消息，且无法恢复。"}</p>
      {deleteError && <p className="sessionDeleteError" role="alert">{deleteError}</p>}
      <div className="sessionDeleteActions">
        <button type="button" onClick={cancelDeleteConfirmation} disabled={deleteSaving}>取消</button>
        <button type="button" className="danger" onClick={() => void confirmDeleteSession()} disabled={deleteSaving}>{deleteSaving ? "删除中…" : "确认删除"}</button>
      </div>
    </section>, document.querySelector(".appShell") ?? document.body)}</>;
}
