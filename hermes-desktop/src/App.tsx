import { UserChatPage } from "./pages/UserChatPage";
import { ApplicationAgentArea } from "./pages/ApplicationAgentArea";
import { ApplicationAgentAuthorizationPage } from "./pages/ApplicationAgentAuthorizationPage";
import { WeComBindPage } from "./pages/WeComBindPage";
import { AuthGate } from "./components/AuthGate";
import { AuthCompletion, AuthErrorPage } from "./components/AuthCompletion";
import { DesktopLayer } from "./components/DesktopLayer";
import { DesktopUpdateNotice } from "./components/DesktopUpdateNotice";
import { DataAppAuthenticationPage } from "./components/DataAppAuthenticationPage";

export function App() {
  if (window.location.pathname === "/auth/desktop-complete") return <main className="appShell"><section className="welcome"><h2>登录完成，请返回桌面客户端。</h2></section></main>;
  if (window.location.pathname === "/auth/complete") return <AuthCompletion />;
  if (window.location.pathname === "/auth/error") return <AuthErrorPage />;
  const isApplicationAgentArea = window.location.pathname.startsWith("/user/application-agents");
  const isWeComBindPage = window.location.pathname === "/user/wecom-bind";
  const isDataAppAuthenticationPage = window.location.pathname === "/user/data-app-auth";
  const page = window.location.pathname === "/user/application-agents/authorize"
      ? <ApplicationAgentAuthorizationPage />
      : isApplicationAgentArea
        ? <ApplicationAgentArea />
        : isWeComBindPage
          ? <WeComBindPage />
          : isDataAppAuthenticationPage
            ? <DataAppAuthenticationPage />
            : <UserChatPage />;
  return (
    <AuthGate
      bootstrapPersonalRuntime={!isApplicationAgentArea && !isWeComBindPage && !isDataAppAuthenticationPage}
      loadingMessage={isDataAppAuthenticationPage ? "身份认证，获取权限内数据，马上好..." : undefined}
    >
      <DesktopLayer><DesktopUpdateNotice />{page}</DesktopLayer>
    </AuthGate>
  );
}
