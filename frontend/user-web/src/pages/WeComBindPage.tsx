import { useEffect, useState } from "react";
import { CheckCircle2, LoaderCircle, TriangleAlert } from "lucide-react";
import { userApi } from "../api/client";
import { getAccessToken, redirectToLogin } from "../auth";
import { usePlatformTheme } from "../theme/platform-theme";


export function WeComBindPage() {
  const [bindingParams] = useState(() => {
    const search = new URL(window.location.href).searchParams;
    return {
      qwToken: search.get("qw_token") || "",
      ticket: search.get("ticket") || "",
    };
  });
  const [bound, setBound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    window.history.replaceState(null, document.title, "/user/wecom-bind");
    const { qwToken, ticket } = bindingParams;
    if (!qwToken && !ticket) {
      setError("绑定链接无效，请返回企业微信重新打开应用 Agent。");
      return;
    }
    const token = getAccessToken();
    if (!token) {
      if (qwToken) {
        window.sessionStorage.setItem("pending_qw_token", qwToken);
        redirectToLogin("/user/wecom-bind");
      } else {
        redirectToLogin(`/user/wecom-bind?ticket=${encodeURIComponent(ticket)}`);
      }
      return;
    }
    let active = true;
    const bindRequest = qwToken
      ? userApi.bindQwToken(qwToken)
      : userApi.bindApplicationAgentWeComMember(ticket);
    void bindRequest
      .then(() => {
        if (active) setBound(true);
      })
      .catch((reason) => {
        if (active) {
          setError(
            reason instanceof Error
              ? reason.message
              : "企业身份绑定失败，请返回企业微信重试。"
          );
        }
      });
    return () => {
      active = false;
    };
  }, [bindingParams]);

  const { theme } = usePlatformTheme();
  return (
    <main className="appShell wecomBindShell" data-theme={theme}>
      <section className="wecomBindCard">
        <div className="wecomBindMark">
          {error ? (
            <TriangleAlert size={28} />
          ) : bound ? (
            <CheckCircle2 size={28} />
          ) : (
            <LoaderCircle className="uploadSpinner" size={28} />
          )}
        </div>
        <h1>
          {error
            ? "绑定未完成"
            : bound
              ? "企业身份绑定成功"
              : "正在绑定企业身份…"}
        </h1>
        {bound ? (
          <>
            <p>你的企业微信身份已成功绑定到当前账号。</p>
            <strong>现在可以关闭此页面，返回企业微信继续对话。</strong>
          </>
        ) : (
          <p>{error || "正在通过 Enterprise SSO 身份验证绑定企业微信。"}</p>
        )}
        {error && (
          <button type="button" onClick={() => window.location.assign("/user/chat")}>
            返回个人 Agent
          </button>
        )}
      </section>
    </main>
  );
}
