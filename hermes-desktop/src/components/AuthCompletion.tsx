import { useEffect, useState } from "react";
import { completeOAuth2Login, redirectToLogin } from "../auth";
import { authErrorMessages } from "../authLogin";

export function AuthCompletion() {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void completeOAuth2Login().catch(() => setError("登录完成验证失败，请重新登录。"));
  }, []);
  return <main className="appShell"><section className="welcome"><h2>{error || "正在恢复登录…"}</h2>
    {error && <button onClick={() => redirectToLogin()}>重新登录</button>}
  </section></main>;
}

export function AuthErrorPage() {
  const params = new URL(window.location.href).searchParams;
  const message = authErrorMessages[params.get("reason") || ""] || "认证未完成，请重新登录。";
  const requestId = params.get("request_id") || "";
  return <main className="appShell"><section className="welcome"><h2>登录未完成</h2><p>{message}</p>
    {/^[a-f0-9]{24}$/.test(requestId) && <p>请求编号：{requestId}</p>}
    <button onClick={() => redirectToLogin()}>重新登录</button>
  </section></main>;
}
