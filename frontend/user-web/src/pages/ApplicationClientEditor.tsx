import { useEffect, useState, type FormEvent } from "react";
import type { ApplicationClientSummary } from "@hermes-cloud/schemas";

export type ApplicationClientDraft = {
  name: string;
  allowUserDelegation: boolean;
  allowServiceInvocation: boolean;
  allowedRedirectUris: string[];
};

export function ApplicationClientEditor({
  client,
  busy,
  readOnly = false,
  onSave,
  onDelete
}: {
  client: ApplicationClientSummary;
  busy: boolean;
  readOnly?: boolean;
  onSave: (draft: ApplicationClientDraft) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const [name, setName] = useState(client.name);
  const [authMode, setAuthMode] = useState(client.authMode);

  useEffect(() => {
    setName(client.name);
    setAuthMode(client.authMode);
  }, [client]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    await onSave({
      name: name.trim(),
      allowUserDelegation: authMode === "delegated_user",
      allowServiceInvocation: authMode === "service",
      allowedRedirectUris: [...client.allowedRedirectUris]
    });
  }

  return (
    <article className="applicationAgentClientCard">
      <header className="applicationAgentClientCardHeader"><div><strong>{client.name}</strong><code>{client.clientId}</code></div><span className="applicationAgentClientStatus" data-status={client.status}>{client.status === "active" ? "已启用" : client.status}</span></header>
      <small className="applicationAgentClientDescription">
        调用身份：{client.authMode === "delegated_user"
          ? "代表 Enterprise SSO 用户调用"
          : client.authMode === "service"
            ? "无用户身份的服务调用"
            : "历史双模式（保存前需二选一）"}
      </small>
      <form className="applicationAgentClientForm" onSubmit={(event) => void submit(event)}>
        <label className="applicationAgentField">
          调用方名称
          <input
            required
            maxLength={100}
            value={name}
            disabled={readOnly}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="applicationAgentChoice">
          <input
            aria-label="允许代表 Enterprise SSO 用户调用"
            type="radio"
            name={`client-auth-${client.applicationClientId}`}
            checked={authMode === "delegated_user"}
            disabled={readOnly}
            onChange={() => setAuthMode("delegated_user")}
          />
          <span><strong>允许代表 Enterprise SSO 用户调用</strong><small>以委托用户身份执行并校验用户权限。</small></span>
        </label>
        <label className="applicationAgentChoice">
          <input
            aria-label="允许无用户身份的服务调用"
            type="radio"
            name={`client-auth-${client.applicationClientId}`}
            checked={authMode === "service"}
            disabled={readOnly}
            onChange={() => setAuthMode("service")}
          />
          <span><strong>允许无用户身份的服务调用</strong><small>以应用服务身份执行，不代表个人用户。</small></span>
        </label>
        {!readOnly && <button className="applicationAgentPrimaryAction" type="submit" disabled={busy || !name.trim() || authMode === "legacy_dual" || authMode === "invalid"}>
          {busy ? "保存中…" : "保存调用方设置"}
        </button>}
        {!readOnly && onDelete && <button className="applicationAgentSecondaryAction" type="button" disabled={busy} onClick={() => void onDelete()}>
          删除调用方
        </button>}
      </form>
    </article>
  );
}
