import { useEffect, useState, type ReactNode } from "react";
import {
  bootstrapSession,
  clearSession,
  getAccessToken,
  isDevAuthMode,
  isPasswordAuthMode,
  redirectToLogin,
} from "../auth";
import { camelize } from "../api/types";
import { AdminLoginPage } from "./AdminLoginPage";
import { AdminAuthenticationTransition } from "./AdminAuthenticationTransition";
import { SessionLifecycle } from "./SessionLifecycle";

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"loading" | "login" | "ready" | "redirecting" | "forbidden" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const restore = async (active: () => boolean) => {
    setState("loading");
    try {
      const principal = await bootstrapSession();
      if (!active()) return;
      if (!principal && isDevAuthMode()) { setState("ready"); return; }
      if (!principal && isPasswordAuthMode()) { setState("login"); return; }
      const accessToken = getAccessToken();
      const response = await fetch("/api/v1/admin/me", {
        credentials: "include",
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {}
      });
      if (!active()) return;
      if (response.status === 401) {
        clearSession();
        if (isPasswordAuthMode()) { setState("login"); return; }
        setState("redirecting");
        redirectToLogin();
        return;
      }
      if (response.status === 403) { setState("forbidden"); return; }
      if (!response.ok) throw new Error(`管理端认证失败：${response.status}`);
      camelize(await response.json());
      setState("ready");
    } catch (reason) {
      if (!active()) return;
      clearSession();
      setError(reason instanceof Error ? reason.message : "认证初始化失败");
      setState("error");
    }
  };
  useEffect(() => {
    let active = true;
    void restore(() => active);
    return () => { active = false; };
  }, []);
  if (state === "ready") return <SessionLifecycle>{children}</SessionLifecycle>;
  if (state === "login") return <AdminLoginPage onAuthenticated={() => { void restore(() => true); }} />;
  if (state === "forbidden") return <main className="adminShell"><section className="adminPanel"><h2>无管理权限</h2><p>当前企业账户没有有效的管理后台工号授权。</p><button onClick={() => window.location.assign("/user/chat")}>返回用户端</button></section></main>;
  if (state === "error") return <main className="adminShell"><section className="adminPanel"><h2>认证初始化失败</h2><p>{error}</p><button onClick={() => window.location.reload()}>重试</button></section></main>;
  return <AdminAuthenticationTransition />;
}
