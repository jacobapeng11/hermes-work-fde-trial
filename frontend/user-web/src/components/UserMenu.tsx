import { useEffect, useRef } from "react";
import { Bot, MessageCircleMore, Moon, MoreHorizontal, PlugZap, ShieldCheck, Sun, UserRound } from "lucide-react";
import { defaultUserProfile, type UserProfile } from "../profile";
import { DigitalHumanAvatar } from "./DigitalHumanAvatar";
import "./user-menu.css";

interface UserMenuProps {
  profile?: UserProfile;
  menuOpen: boolean;
  onMenuToggle?: () => void;
  theme?: "dark" | "light";
  onThemeToggle?: () => void;
  onProfileOpen?: () => void;
  onImOpen?: () => void;
  onMcpOpen?: () => void;
  canManageApplicationAgents?: boolean;
  onApplicationAgentsOpen?: () => void;
  applicationAgentMode?: boolean;
  applicationAgentDebugMode?: boolean;
  isAdmin?: boolean;
  onAdminOpen?: () => void;
}

/* 侧栏底部用户区 + 数字人菜单浮层（自 AppSidebar 抽离，样式见 user-menu.css）。
   浮层为手写实现而非 antd Dropdown：AppSidebar 位于主入口静态图，
   静态引入 antd 会击穿入口 chunk 基线（设计文档 014 §2）。
   主题项右侧的 Switch 为 antd small 尺寸（28×16）等价复刻，
   role="switch" + aria-checked，无文本内容以保持 menuitem textContent 契约。 */
export function UserMenu({ profile = defaultUserProfile(), menuOpen, onMenuToggle, theme = "dark", onThemeToggle, onProfileOpen, onImOpen, onMcpOpen, canManageApplicationAgents = false, onApplicationAgentsOpen, applicationAgentMode = false, applicationAgentDebugMode = false, isAdmin = false, onAdminOpen }: UserMenuProps) {
  const profileMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menuOpen || !onMenuToggle) return;
    const closeProfileMenuOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !profileMenuRef.current?.contains(target)) onMenuToggle();
    };
    document.addEventListener("pointerdown", closeProfileMenuOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeProfileMenuOnOutsidePointer);
  }, [menuOpen, onMenuToggle]);

  return <div className="profileMenu" ref={profileMenuRef}>
    <button className="profileButton" aria-label={`我的数字人：${profile.displayName}`} aria-expanded={menuOpen} onClick={onMenuToggle}>
      <span className="profileAvatarWrap">
        <DigitalHumanAvatar profile={profile} className="sidebarProfileAvatar"/>
        <span className="profilePresenceDot" aria-hidden="true"/>
      </span>
      <span className="profileName">{profile.displayName}</span>
      <MoreHorizontal className="profileMoreIcon" size={16}/>
    </button>
    {menuOpen && <div className="user-menu-popover" role="menu" aria-label="数字人菜单">
      {onProfileOpen && <button type="button" className="user-menu-item" role="menuitem" onClick={() => { onProfileOpen(); onMenuToggle?.(); }}><UserRound size={16}/>个人资料</button>}
      {canManageApplicationAgents && <button type="button" className="user-menu-item" role="menuitem" onClick={() => { onMenuToggle?.(); onApplicationAgentsOpen?.(); }}><Bot size={16}/>{applicationAgentMode || applicationAgentDebugMode ? "个人 Agent" : "应用 Agent"}</button>}
      <div
        className="user-menu-item user-menu-item-switch"
        role="menuitem"
        tabIndex={0}
        onClick={onThemeToggle}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onThemeToggle?.();
          }
        }}
      >
        <span className="user-menu-item-main">{theme === "dark" ? <Moon size={16}/> : <Sun size={16}/>}切换至{theme === "dark" ? "浅色" : "深色"}主题</span>
        {/* 无 aria-label/title：避免混入父级 menuitem 的 accessible name 计算（textContent 契约） */}
        <span className="user-menu-switch" role="switch" aria-checked={theme === "dark"} data-checked={theme === "dark"}><span className="user-menu-switch-dot"/></span>
      </div>
      {onMcpOpen && <button type="button" className="user-menu-item" role="menuitem" onClick={() => { onMenuToggle?.(); onMcpOpen(); }}><PlugZap size={16}/>MCP</button>}
      {onImOpen && <button type="button" className="user-menu-item" role="menuitem" onClick={() => { onMenuToggle?.(); onImOpen(); }}><MessageCircleMore size={16}/>IM</button>}
      {isAdmin && <button type="button" className="user-menu-item" role="menuitem" onClick={() => { onMenuToggle?.(); onAdminOpen?.(); }}><ShieldCheck size={16}/>管理后台</button>}
    </div>}
  </div>;
}
