import { useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { userApi } from "../api/client";
import { AgentWorkspace } from "./AgentWorkspace";

export function ApplicationAgentDebugGate({
  applicationAgentId,
  onReturnManage
}: {
  applicationAgentId: number;
  onReturnManage: () => void;
}) {
  const [allowed, setAllowed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void userApi.applicationAgent(applicationAgentId)
      .then((agent) => {
        if (!active) return;
        if (agent.accessMode !== "owner") {
          window.sessionStorage.setItem(
            "application-agent-manage-notice",
            "只有应用创建者可以进入配置与调试；你仍可在 Manage 查看应用摘要。"
          );
          onReturnManage();
          return;
        }
        if (agent.lifecycleStatus === "deleting") {
          window.sessionStorage.setItem(
            "application-agent-manage-notice",
            "该应用 Agent 正在删除，暂时不能进入 Debug。"
          );
          onReturnManage();
          return;
        }
        if (agent.runtimeStatus !== "ready") {
          window.sessionStorage.setItem(
            "application-agent-manage-notice",
            agent.runtimeStatus === "failed"
              ? "Runtime 准备失败，后台会自动重试，请稍后再试。"
              : "Runtime 正在准备，请在 Manage 等待就绪后再进入 Debug。"
          );
          onReturnManage();
          return;
        }
        setAllowed(true);
      })
      .catch((reason) => {
        if (!active) return;
        setError(
          reason instanceof Error
            ? reason.message
            : "无法确认应用 Agent 调试权限"
        );
      });
    return () => {
      active = false;
    };
  }, [applicationAgentId, onReturnManage]);

  if (error) {
    return (
      <main className="appShell">
        <p role="alert">{error}</p>
        <button type="button" onClick={onReturnManage}>返回 Manage</button>
      </main>
    );
  }
  if (!allowed) {
    return (
      <main className="appShell">
        <p><LoaderCircle className="uploadSpinner" size={16}/>正在确认创建者权限…</p>
      </main>
    );
  }
  return (
    <AgentWorkspace
      context={{ kind: "application", applicationAgentId }}
    />
  );
}
