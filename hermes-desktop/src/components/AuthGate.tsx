import { useEffect, useState, type ReactNode } from "react";
import { LoaderCircle } from "lucide-react";
import {
  bootstrapSession,
  hasAdminPostLoginIntent,
  hasPendingReauthentication,
  isDevAuthMode,
  markReauthenticationManual,
  redirectToLogin,
  reserveAutomaticReauthenticationAttempt
} from "../auth";
import { ReauthenticationPrompt } from "./ReauthenticationPrompt";
import { SessionLifecycle } from "./SessionLifecycle";
import { AdminAuthenticationTransition } from "./AdminAuthenticationTransition";
import { usePlatformTheme } from "../theme/platform-theme";
import { userApi } from "../api/client";

interface AuthGateProps {
  bootstrapPersonalRuntime?: boolean;
  loadingMessage?: string;
  children: ReactNode;
}

export function AuthGate({
  bootstrapPersonalRuntime = false,
  loadingMessage = "身份认证登录中…",
  children
}: AuthGateProps) {
  const [state, setState] = useState<
    "loading" | "ready" | "redirecting" | "error" | "reauth_required"
  >("loading");
  const [error, setError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();
  // The admin post-login handoff clears sessionStorage and rewrites the
  // callback URL inside bootstrapSession(), then navigates away with
  // location.replace(). Capture both facts during the first render: reading
  // them again on the ready render would let the user chat shell mount for a
  // few hundred milliseconds and bootstrap/prewarm the admin's personal
  // Runtime before the browser follows the navigation.
  const [adminHandoff] = useState(() => hasAdminPostLoginIntent());
  const [adminHandoffNavigation] = useState(
    () => adminHandoff
      && new URL(window.location.href).searchParams.has("token")
  );

  useEffect(() => {
    let active = true;
    const continueReauthentication = (): boolean => {
      if (!hasPendingReauthentication()) return false;
      if (reserveAutomaticReauthenticationAttempt()) {
        setState("redirecting");
        redirectToLogin();
      } else {
        markReauthenticationManual();
        setState("reauth_required");
      }
      return true;
    };
    void bootstrapSession().then((principal) => {
      if (!active) return;
      if (!principal && !isDevAuthMode()) {
        if (continueReauthentication()) return;
        setState("redirecting");
        redirectToLogin();
        return;
      }
      if (principal && bootstrapPersonalRuntime && !adminHandoffNavigation) {
        void userApi.bootstrapRuntime().catch(() => undefined);
      }
      setState("ready");
    }).catch((reason) => {
      if (!active) return;
      if (continueReauthentication()) return;
      setError(reason instanceof Error ? reason.message : "认证初始化失败");
      setState("error");
    });
    return () => { active = false; };
  }, [adminHandoffNavigation, bootstrapPersonalRuntime]);

  if (state === "ready" && !adminHandoffNavigation) {
    return <SessionLifecycle>{children}</SessionLifecycle>;
  }
  if (state === "reauth_required") return (
    <main className="appShell" data-theme={theme}>
      <ReauthenticationPrompt
        theme={theme}
        onAuthenticate={() => {
          setState("redirecting");
          redirectToLogin();
        }}
      />
    </main>
  );
  if (state === "error") return <main className="appShell"><section className="welcome"><h2>认证初始化失败</h2><p>{error}</p><button onClick={() => window.location.reload()}>重试</button></section></main>;
  if (adminHandoff) return <AdminAuthenticationTransition />;
  return <main className="appShell authLoadingShell" data-theme={theme}>
    <aside className="authLoadingSidebar" aria-hidden="true">
      <div className="authLoadingBrand"><strong>Hermes Work</strong></div>
      <div className="authLoadingNav">{["wide", "medium", "wide", "short", "medium"].map((width, index) => <i key={index} data-width={width}/>)}</div>
      <div className="authLoadingSessions"><span/><i/><i/><i/></div>
      <div className="authLoadingProfile"><i/><span/></div>
    </aside>
    <section className="authLoadingMain">
      <header className="authLoadingHeader" aria-hidden="true"><div><i/><span/></div><i/></header>
      <div className="authLoadingStatus" role="status" aria-label={loadingMessage}>
        <div className="authLoadingMark" aria-hidden="true">
          <span className="authLoadingTrack"/>
          <LoaderCircle size={30}/>
          <i/>
        </div>
        <strong>{loadingMessage}</strong>
        <div className="authLoadingDots" aria-hidden="true"><i/><i/><i/></div>
      </div>
    </section>
  </main>;
}
