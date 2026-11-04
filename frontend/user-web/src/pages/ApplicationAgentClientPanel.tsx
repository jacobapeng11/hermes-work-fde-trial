import { useEffect, useState, type FormEvent } from "react";
import { CirclePause, KeyRound, Play, Send } from "lucide-react";
import type {
  ApplicationAgentDetail,
  ApplicationClientCreated,
  ApplicationClientSummary
} from "@hermes-cloud/schemas";
import { userApi } from "../api/client";
import {
  ApplicationClientEditor,
  type ApplicationClientDraft
} from "./ApplicationClientEditor";

export function ApplicationAgentClientPanel({
  applicationAgentId
}: {
  applicationAgentId: number;
}) {
  const [agent, setAgent] = useState<ApplicationAgentDetail | null>(null);
  const [clients, setClients] = useState<ApplicationClientSummary[]>([]);
  const [name, setName] = useState("");
  const [authMode, setAuthMode] = useState<"delegated_user" | "service">("delegated_user");
  const [secret, setSecret] = useState<ApplicationClientCreated | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = async () => {
    const [detail, nextClients] = await Promise.all([
      userApi.applicationAgent(applicationAgentId),
      userApi.applicationAgentClients(applicationAgentId)
    ]);
    setAgent(detail);
    setClients(nextClients);
  };

  useEffect(() => {
    let active = true;
    void Promise.all([
      userApi.applicationAgent(applicationAgentId),
      userApi.applicationAgentClients(applicationAgentId)
    ]).then(([detail, nextClients]) => {
      if (!active) return;
      setAgent(detail);
      setClients(nextClients);
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : "加载 API 调用方失败");
    });
    return () => { active = false; };
  }, [applicationAgentId]);

  const createClient = async (event: FormEvent) => {
    event.preventDefault();
    setBusy("create");
    setError(null);
    try {
      const created = await userApi.createApplicationAgentClient(applicationAgentId, {
        name: name.trim(),
        allowUserDelegation: authMode === "delegated_user",
        allowServiceInvocation: authMode === "service",
        scopes: ["responses:create"],
        allowedRedirectUris: []
      });
      setSecret(created);
      setName("");
      await load();
      setNotice("调用方已创建。Client Secret 只显示这一次，请立即保存。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建调用方失败");
    } finally {
      setBusy(null);
    }
  };

  const updateClient = async (
    client: ApplicationClientSummary,
    draft: ApplicationClientDraft
  ) => {
    const operation = `client-${client.applicationClientId}`;
    setBusy(operation);
    try {
      const updated = await userApi.updateApplicationAgentClient(
        applicationAgentId,
        client.applicationClientId,
        draft
      );
      setClients((current) => current.map((item) =>
        item.applicationClientId === updated.applicationClientId ? updated : item
      ));
      setNotice("调用方授权设置已保存。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存调用方失败");
    } finally {
      setBusy(null);
    }
  };

  const deleteClient = async (client: ApplicationClientSummary) => {
    if (!window.confirm(`确定删除调用方“${client.name}”吗？`)) return;
    const operation = `client-${client.applicationClientId}`;
    setBusy(operation);
    try {
      await userApi.deleteApplicationAgentClient(
        applicationAgentId,
        client.applicationClientId
      );
      await load();
      setNotice("调用方已删除。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除调用方失败");
    } finally {
      setBusy(null);
    }
  };

  const lifecycle = async (action: "publish" | "disable" | "resume") => {
    setBusy(action);
    try {
      const updated = action === "publish"
        ? await userApi.publishApplicationAgent(applicationAgentId)
        : action === "disable"
          ? await userApi.disableApplicationAgent(applicationAgentId)
          : await userApi.resumeApplicationAgent(applicationAgentId);
      setAgent(updated);
      setNotice(action === "publish" ? "应用已发布。" : action === "disable" ? "应用已停用。" : "应用已恢复。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生命周期操作失败");
    } finally {
      setBusy(null);
    }
  };

  const isOwner = agent?.accessMode === "owner";

  return (
    <section className="applicationAgentClientWorkspace">
      {error && <p className="applicationAgentError" role="alert">{error}</p>}
      {notice && <p className="applicationAgentNotice" role="status">{notice}</p>}
      {agent && (
        <header className="applicationAgentLifecycleBar">
          <div><strong>{agent.name}</strong><small>{agent.status} · Runtime {agent.runtimeStatus}</small></div>
          {isOwner && <div>
            {agent.status === "unpublished" && <button type="button" disabled={busy !== null || clients.length === 0} onClick={() => void lifecycle("publish")}><Send size={16}/>发布</button>}
            {agent.status === "published" && <button type="button" disabled={busy !== null} onClick={() => void lifecycle("disable")}><CirclePause size={16}/>停用</button>}
            {agent.status === "disabled" && <button type="button" disabled={busy !== null} onClick={() => void lifecycle("resume")}><Play size={16}/>恢复</button>}
          </div>}
        </header>
      )}
      <div className="applicationAgentSectionHeader"><div className="applicationAgentSectionIcon"><KeyRound size={17}/></div><div><h2>API 调用方</h2><p>{isOwner ? "管理绑定给其他系统或服务端的认证调用方。" : "调用方配置仅可由创建者管理，当前为只读视图。"}</p></div></div>
      {isOwner && <form className="applicationAgentClientCreate" onSubmit={(event) => void createClient(event)}>
        <label className="applicationAgentField">调用方名称<input required maxLength={100} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：工单门户" /></label>
        <fieldset className="applicationAgentAuthChoices">
          <legend>调用身份模式</legend>
          <label className="applicationAgentChoice"><input aria-label="代表 Enterprise SSO 用户调用" type="radio" checked={authMode === "delegated_user"} onChange={() => setAuthMode("delegated_user")}/><span><strong>代表 Enterprise SSO 用户调用</strong><small>请求必须携带已映射的平台用户身份。</small></span></label>
          <label className="applicationAgentChoice"><input aria-label="无用户身份的服务调用" type="radio" checked={authMode === "service"} onChange={() => setAuthMode("service")}/><span><strong>无用户身份的服务调用</strong><small>适合后端服务，按应用服务身份执行。</small></span></label>
        </fieldset>
        <button className="applicationAgentPrimaryAction" type="submit" disabled={!name.trim() || busy !== null}>创建调用方</button>
      </form>}
      {secret && <div className="applicationAgentSecret" role="status"><strong>请立即保存 Client Secret</strong><code>{secret.clientSecret}</code><small>离开本页后不再显示。</small></div>}
      <div className="applicationAgentClients">
        {clients.map((client) => (
          <ApplicationClientEditor
            key={client.applicationClientId}
            client={client}
            busy={busy === `client-${client.applicationClientId}`}
            readOnly={!isOwner}
            onSave={(draft) => updateClient(client, draft)}
            onDelete={() => deleteClient(client)}
          />
        ))}
      </div>
    </section>
  );
}
