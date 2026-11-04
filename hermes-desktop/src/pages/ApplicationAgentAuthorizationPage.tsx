import { useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { ApiError, userApi } from "../api/client";

type AuthorizationParameters = {
  applicationAgent: string;
  clientId: string;
  redirectUri: string;
  state: string;
};

let activeAttempt: {
  key: string;
  promise: Promise<void>;
} | null = null;

export function parseAuthorizationParameters(
  rawUrl: string
): AuthorizationParameters | null {
  const query = new URL(rawUrl).searchParams;
  const applicationAgent = query.get("application_agent") || "";
  const clientId = query.get("client_id") || "";
  const redirectUri = query.get("redirect_uri") || "";
  const state = query.get("state") || "";
  if (
    !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(applicationAgent)
    || clientId.length < 1
    || clientId.length > 128
    || redirectUri.length < 1
    || redirectUri.length > 2_048
    || state.length < 32
    || state.length > 512
    || !/^[A-Za-z0-9._~-]+$/.test(state)
  ) {
    return null;
  }
  return { applicationAgent, clientId, redirectUri, state };
}

export function buildAuthorizationCallbackUrl(
  redirectUri: string,
  values: { code?: string; error?: string; state: string }
): string {
  const target = new URL(redirectUri);
  if (values.code) target.searchParams.set("code", values.code);
  if (values.error) target.searchParams.set("error", values.error);
  target.searchParams.set("state", values.state);
  return target.toString();
}

function safeErrorCode(reason: unknown): string {
  if (!(reason instanceof ApiError)) return "temporarily_unavailable";
  if (
    reason.code === "application_agent_not_found"
    || reason.code === "application_client_unauthorized"
    || reason.code === "application_authorization_not_allowed"
  ) {
    return "unauthorized_client";
  }
  if (reason.status === 403) return "access_denied";
  return "temporarily_unavailable";
}

async function completeAuthorization(
  parameters: AuthorizationParameters
): Promise<void> {
  try {
    const response = await userApi.authorizeApplicationAgent({
      applicationAgent: parameters.applicationAgent,
      clientId: parameters.clientId,
      redirectUri: parameters.redirectUri
    });
    if (response.redirectUri !== parameters.redirectUri) {
      throw new Error("平台没有确认 SQLBot 回调地址");
    }
    window.location.replace(buildAuthorizationCallbackUrl(response.redirectUri, {
      code: response.code,
      state: parameters.state
    }));
  } catch (reason) {
    if (
      reason instanceof ApiError
      && reason.redirectUri === parameters.redirectUri
    ) {
      window.location.replace(buildAuthorizationCallbackUrl(reason.redirectUri, {
        error: safeErrorCode(reason),
        state: parameters.state
      }));
      return;
    }
    throw reason;
  }
}

export function ApplicationAgentAuthorizationPage() {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const parameters = parseAuthorizationParameters(window.location.href);
    if (!parameters) {
      setError("授权地址缺少必要信息，请返回 SQLBot 重新登录。");
      return;
    }
    const key = window.location.href;
    if (activeAttempt?.key !== key) {
      activeAttempt = {
        key,
        promise: completeAuthorization(parameters)
      };
    }
    void activeAttempt.promise.catch((reason) => {
      setError(
        reason instanceof Error
          ? reason.message
          : "暂时无法完成 SQLBot 授权，请稍后重试。"
      );
    });
  }, []);

  return (
    <main className="appShell authLoadingShell">
      <section className="authLoadingMain">
        <div className="authLoadingStatus" role={error ? "alert" : "status"}>
          {!error && <LoaderCircle className="uploadSpinner" size={30}/>}
          <strong>{error || "正在完成 SQLBot 授权…"}</strong>
          {error && (
            <button type="button" onClick={() => window.location.reload()}>
              重试
            </button>
          )}
        </div>
      </section>
    </main>
  );
}
