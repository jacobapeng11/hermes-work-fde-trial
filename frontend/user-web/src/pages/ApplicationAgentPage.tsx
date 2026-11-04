import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  Bot,
  LoaderCircle,
  Plus
} from "lucide-react";
import type {
  ApplicationAgentConfig,
  ApplicationAgentDetail,
  ApplicationAgentSummary,
  ApplicationClientSummary
} from "@hermes-cloud/schemas";
import { userApi } from "../api/client";
import { getPrincipal } from "../auth";
import { AppSidebar } from "../components/AppSidebar";
import { loadUserProfile } from "../profile";
import { usePlatformTheme } from "../theme/platform-theme";
const DEFAULT_CONFIG: ApplicationAgentConfig = {
  systemPrompt: "",
  modelEntryId: null,
  skillAssetIds: [],
  mcpAssetIds: [],
  enterpriseKnowledgeEnabled: false,
  memoryEnabled: true
};
const applicationAgentViews = [
  { id: "application-agents", label: "应用 Agent", icon: Bot }
] as const;

function pathAgentId(): number | null {
  const match = window.location.pathname.match(
    /^\/user\/application-agents\/(\d+)\/manage\/?$/
  );
  return match ? Number(match[1]) : null;
}

function statusText(status: ApplicationAgentSummary["status"]): string {
  if (status === "published") return "已发布";
  if (status === "disabled") return "已停用";
  return "未发布";
}

function runtimeReady(agent: Pick<ApplicationAgentSummary, "runtimeStatus" | "lifecycleStatus">): boolean {
  return agent.lifecycleStatus === "active" && agent.runtimeStatus === "ready";
}

function runtimeText(agent: Pick<ApplicationAgentSummary, "runtimeStatus" | "applyStatus" | "lifecycleStatus" | "deleteLastErrorCode">): string {
  if (agent.lifecycleStatus === "deleting") return "删除中";
  if (agent.runtimeStatus === "ready") return "已就绪";
  if (agent.deleteLastErrorCode) return "删除失败，后台将重试";
  if (agent.runtimeStatus === "failed" || agent.applyStatus === "failed") return "准备失败，后台将重试";
  return "准备中";
}

function debugEntryText(agent: Pick<ApplicationAgentSummary, "runtimeStatus" | "lifecycleStatus">): string {
  if (agent.lifecycleStatus === "deleting") return "删除中";
  if (runtimeReady(agent)) return "进入配置与调试";
  return "Runtime 准备中…";
}

export function ApplicationAgentPage({
  onEnterDebug,
  onPathChange
}: {
  onEnterDebug?: (applicationAgentId: number) => void;
  onPathChange?: () => void;
} = {}) {
  const [agents, setAgents] = useState<ApplicationAgentSummary[]>([]);
  const [selected, setSelected] = useState<ApplicationAgentDetail | null>(null);
  const [clients, setClients] = useState<ApplicationClientSummary[]>([]);
  const [skillLabels, setSkillLabels] = useState<Record<number, string>>({});
  const [mcpLabels, setMcpLabels] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState({ name: "", slug: "" });
  const [menuOpen, setMenuOpen] = useState(false);
  const { theme, setTheme } = usePlatformTheme();
  const profile = loadUserProfile(getPrincipal());

  useEffect(() => {
    let active = true;
    const redirectedNotice = window.sessionStorage.getItem(
      "application-agent-manage-notice"
    );
    if (redirectedNotice) {
      setNotice(redirectedNotice);
      window.sessionStorage.removeItem("application-agent-manage-notice");
    }
    const initialAgentId = pathAgentId();
    void userApi.applicationAgentAccess()
      .then(async (access) => {
        if (!access.canManageApplicationAgents) {
          window.location.replace("/user/chat");
          return;
        }
        const list = await userApi.applicationAgents();
        if (!active) return;
        setAgents(list);
        if (initialAgentId !== null) {
          const initialExists = list.some(
            (agent) => agent.applicationAgentId === initialAgentId
          );
          if (initialExists) {
            await openAgent(initialAgentId, false, Boolean(redirectedNotice));
          } else {
            setSelected(null);
            setClients([]);
            if (window.location.pathname !== "/user/application-agents") {
              window.history.replaceState(
                null,
                "",
                "/user/application-agents"
              );
              onPathChange?.();
            }
          }
        }
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "加载应用 Agent 失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    const handlePopState = () => {
      const nextAgentId = pathAgentId();
      if (nextAgentId === null) {
        setSelected(null);
        setClients([]);
      } else {
        void openAgent(nextAgentId, false);
      }
    };
    window.addEventListener("popstate", handlePopState);
    return () => {
      active = false;
      window.removeEventListener("popstate", handlePopState);
    };
  }, []);

  useEffect(() => {
    const pending = agents.some((agent) => !runtimeReady(agent) || agent.lifecycleStatus === "deleting");
    if (!pending) return undefined;
    let active = true;
    const timer = window.setInterval(() => {
      void userApi.applicationAgents().then((list) => {
        if (!active) return;
        setAgents(list);
        const selectedId = selected?.applicationAgentId;
        if (selectedId === undefined) return;
        const current = list.find((agent) => agent.applicationAgentId === selectedId);
        if (!current) {
          setSelected(null);
          setClients([]);
          if (window.location.pathname !== "/user/application-agents") {
            window.history.replaceState(null, "", "/user/application-agents");
            onPathChange?.();
          }
          return;
        }
        void userApi.applicationAgent(selectedId).then((detail) => {
          if (active) setSelected(detail);
        }).catch(() => undefined);
      }).catch(() => undefined);
    }, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [agents, selected?.applicationAgentId]);

  async function refreshAgents() {
    setAgents(await userApi.applicationAgents());
  }

  async function openAgent(
    applicationAgentId: number,
    updatePath = true,
    preserveNotice = false
  ) {
    setLoading(true);
    setError(null);
    if (!preserveNotice) setNotice(null);
    try {
      const [detail, nextClients, catalog, privateSkills] = await Promise.all([
        userApi.applicationAgent(applicationAgentId),
        userApi.applicationAgentClients(applicationAgentId),
        typeof userApi.userAssetCatalog === "function"
          ? userApi.userAssetCatalog().catch(() => ({ skills: [], mcps: [] }))
          : Promise.resolve({ skills: [], mcps: [] }),
        typeof userApi.applicationAgentSkills === "function"
          ? userApi.applicationAgentSkills(applicationAgentId).catch(() => [])
          : Promise.resolve([])
      ]);
      setSelected(detail);
      setClients(nextClients);
      setSkillLabels(Object.fromEntries([
        ...catalog.skills.map((item) => [item.assetId, `${item.name}${item.version ? ` v${item.version}` : ""}`]),
        ...privateSkills.map((item) => [item.applicationAgentSkillId, `${item.name} v${item.version ?? 1}（应用私有）`])
      ]));
      setMcpLabels(Object.fromEntries(
        catalog.mcps.map((item) => [item.assetId, `${item.name}${item.version ? ` v${item.version}` : ""}`])
      ));
      if (updatePath) {
        window.history.pushState(
          null,
          "",
          `/user/application-agents/${applicationAgentId}/manage`
        );
        onPathChange?.();
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取应用 Agent 失败");
    } finally {
      setLoading(false);
    }
  }

  async function createAgent(event: FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError(null);
    try {
      const created = await userApi.createApplicationAgent({
        name: createDraft.name.trim(),
        slug: createDraft.slug.trim(),
        config: DEFAULT_CONFIG
      });
      setCreateOpen(false);
      setCreateDraft({ name: "", slug: "" });
      await refreshAgents();
      await openAgent(created.applicationAgentId);
      setNotice("应用 Agent 已创建，Runtime 正在后台准备；准备完成后可进入 Debug。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建应用 Agent 失败");
    } finally {
      setBusy(null);
    }
  }

  if (loading && agents.length === 0 && selected === null) {
    return <main className="appShell applicationAgentShell" data-theme={theme}><LoaderCircle className="uploadSpinner" size={28}/><span>正在加载应用 Agent…</span></main>;
  }

  async function deleteAgentFromManage(id: string) {
    const target = agents.find((agent) => String(agent.applicationAgentId) === id);
    if (!target || target.accessMode !== "owner" || target.lifecycleStatus === "deleting") return;
    await userApi.deleteApplicationAgent(target.applicationAgentId);
    setNotice("应用 Agent 删除请求已提交，后台正在清理资源。");
    setAgents((current) => current.map((agent) => (
      agent.applicationAgentId === target.applicationAgentId
        ? { ...agent, lifecycleStatus: "deleting" as const }
        : agent
    )));
    if (selected?.applicationAgentId === target.applicationAgentId) {
      setSelected((current) => current ? { ...current, lifecycleStatus: "deleting" as const } : current);
    }
  }

  return (
    <main className="appShell applicationAgentShell" data-theme={theme}>
      <AppSidebar
        view="application-agents"
        views={applicationAgentViews}
        onViewChange={() => undefined}
        sessions={agents.map((agent) => ({
          runtimeSessionId: String(agent.applicationAgentId),
          title: agent.name,
          messageCount: 0,
          isReadOnly: agent.accessMode !== "owner",
          status: agent.lifecycleStatus === "deleting" ? "deleting" : agent.runtimeStatus
        }))}
        activeSessionId={
          selected ? String(selected.applicationAgentId) : null
        }
        onSessionChange={(id) => void openAgent(Number(id))}
        onCreateSession={() => setCreateOpen(true)}
        menuOpen={menuOpen}
        onMenuToggle={() => setMenuOpen((open) => !open)}
        profile={profile}
        canManageApplicationAgents
        applicationAgentMode
        onDeleteSession={deleteAgentFromManage}
        sessionSectionLabel="应用 Agent"
        onApplicationAgentsOpen={() => window.location.assign("/user/chat")}
        theme={theme}
        onThemeToggle={() => setTheme(theme === "dark" ? "light" : "dark")}
      />
      <section className="mainArea">
        <header className="appHeader" data-view="application-agents">
          <button type="button" className="applicationDebugBack" onClick={() => window.location.assign("/user/chat")}><ArrowLeft size={16}/>返回个人 Agent</button>
          <div className="headerIdentity">
            <div className="chatTitle">
              <strong>{selected?.name || "应用 Agent 管理"}</strong>
            </div>
          </div>
        </header>
        <div className="applicationAgentWorkspace">
        <section className="applicationAgentMain">
          {error && <p className="applicationAgentError" role="alert">{error}</p>}
          {notice && <p className="applicationAgentNotice" role="status">{notice}</p>}
          {!selected && (
            <div className="applicationAgentEmpty">
              <Bot size={36}/>
              <h2>创建第一个应用 Agent</h2>
              <p>使用与个人 Agent 一致的能力配置，并发布为受认证的企业内部接口。</p>
              <button type="button" onClick={() => setCreateOpen(true)}><Plus size={16}/>新建应用 Agent</button>
            </div>
          )}
          {selected && (
            <>
              <header className="applicationAgentStudioHeader">
                <div>
                  <span className="applicationAgentStatus" data-status={selected.status}>{statusText(selected.status)}</span>
                  <h1>{selected.name}</h1>
                  <small>
                    Runtime {selected.runtimeStatus} · 配置 #{selected.configGeneration}
                    {selected.appliedGeneration ? ` / 已应用 #${selected.appliedGeneration}` : ""}
                  </small>
                  <small>
                    创建者：{selected.creatorName || "未知"}
                    {selected.creatorUserCode ? `（${selected.creatorUserCode}）` : ""}
                    {selected.creatorStatus && selected.creatorStatus !== "active"
                      ? ` · 账号状态 ${selected.creatorStatus}`
                      : ""}
                  </small>
                </div>
              </header>

              <section className="applicationAgentOwnerCard" aria-labelledby="application-agent-owner-title">
                <div>
                  <h2 id="application-agent-owner-title">配置与调试</h2>
                  <p role="status">
                    {selected.accessMode === "owner"
                      ? "你是该应用的创建者，可以配置、发布和调试。"
                      : "仅应用创建者可以配置和调试；你可以继续查看 Manage 摘要。"}
                  </p>
                </div>
                <div className="applicationAgentOwnerActions">
                    {selected.accessMode === "owner" && (
                    <button
                      type="button"
                      disabled={!runtimeReady(selected)}
                      onClick={() => {
                        if (onEnterDebug) {
                          onEnterDebug(selected.applicationAgentId);
                        } else {
                          window.location.assign(`/user/application-agents/${selected.applicationAgentId}/debug`);
                        }
                      }}
                    >
                      <Bot size={16}/>{debugEntryText(selected)}
                    </button>
                  )}
                </div>
              </section>

              <div className="applicationAgentStudioGrid">
                <section className="applicationAgentApiHelpPanel applicationAgentSummaryPanel">
                  <div>
                    <h2>运行与发布摘要</h2>
                    <small>Manage 页面只展示已保存状态，不提供写操作。</small>
                  </div>
                  <dl className="applicationAgentSummaryList">
                    <div><dt>发布状态</dt><dd><span className="applicationAgentInlineStatus" data-status={selected.status}>{statusText(selected.status)}</span></dd></div>
                    <div><dt>Runtime</dt><dd>{runtimeReady(selected) ? "已就绪" : <><LoaderCircle className="uploadSpinner" size={14}/>{runtimeText(selected)}</>}</dd></div>
                    <div><dt>配置代数</dt><dd>#{selected.configGeneration}</dd></div>
                    <div><dt>已应用代数</dt><dd>{selected.appliedGeneration ? `#${selected.appliedGeneration}` : "尚未应用"}</dd></div>
                    <div><dt>应用状态</dt><dd>{selected.applyStatus}</dd></div>
                    <div><dt>接口标识</dt><dd><code>{selected.slug}</code></dd></div>
                  </dl>
                </section>

                <section className="applicationAgentApiHelpPanel applicationAgentConfigSummaryPanel">
                  <div>
                    <h2>Agent 配置</h2>
                    <small>完整只读预览；只有应用创建者可以修改。</small>
                  </div>
                  <p>模型</p>
                  <code>{selected.config.modelEntryId ?? "使用平台默认模型"}</code>
                  <p>系统提示词</p>
                  <pre className="applicationAgentPromptPreview" tabIndex={0} aria-label="系统提示词只读预览">{selected.config.systemPrompt || "未配置"}</pre>
                  <p>技能</p>
                  <code>{[...(selected.config.skillAssetIds || []), ...(selected.config.applicationSkillIds || [])].map((id) => skillLabels[id] || `#${id}`).join(", ") || "未配置"}</code>
                  <p>MCP</p>
                  <code>{selected.config.mcpAssetIds.map((id) => mcpLabels[id] || `#${id}`).join(", ") || "未配置"}</code>
                  <p>个人知识库</p>
                  <code>
                    已启用 {selected.personalKnowledgeEnabledCount} 个
                    {selected.personalKnowledgeRevokedCount > 0
                      ? ` · 已因权限撤销停用 ${selected.personalKnowledgeRevokedCount} 个`
                      : ""}
                  </code>
                  <p>企业知识库</p>
                  <code>{selected.config.enterpriseKnowledgeEnabled ? "已启用" : "未启用"}</code>
                </section>

                <section className="applicationAgentClientPanel applicationAgentClientPanelReadOnly">
                  <div>
                    <h2>API 调用方</h2>
                    <small>调用方配置仅可由创建者在 Debug 工作台修改。</small>
                  </div>
                  <div className="applicationAgentClients">
                    {clients.length === 0 && <p className="muted">尚未创建 API 调用方。</p>}
                    {clients.map((client) => (
                      <article className="applicationAgentClientSummary" key={client.applicationClientId}>
                        <strong>{client.name}</strong>
                        <small>{client.clientId}</small>
                        <span>{client.status === "active" ? "已启用" : client.status}</span>
                        <span>{client.authMode === "service" ? "服务身份" : "委托用户身份"}</span>
                        <span>{client.allowedRedirectUris.length} 个授权回调</span>
                      </article>
                    ))}
                  </div>
                </section>

                <details className="applicationAgentApiHelpPanel applicationAgentApiHelpDetails">
                  <summary><strong>API 调用说明</strong><small>查看请求地址与示例</small></summary>
                  <p>请求地址</p>
                  <code>/openapi/v1/application-agents/{selected.slug}/responses</code>
                  <pre>{`curl -u '<Client ID>:<Client Secret>' \\
  -H 'Content-Type: application/json' \\
  -d '{
  "input": "帮我查询工单",
  "external_user_id": "customer-1001",
  "conversation_key": "default"
}' '${window.location.origin}/openapi/v1/application-agents/${selected.slug}/responses'`}</pre>
                  <ul>
                    <li><code>external_user_id</code>：外部项目里的用户编号；同一编号会接着原来的对话。</li>
                    <li><code>conversation_key</code>：可不传；外部用户不传时自动使用 <code>default</code>。</li>
                    <li>回答从返回结果的 <code>output_text</code> 字段读取。</li>
                  </ul>
                  <p>401 通常是调用方凭据错误；404 通常是接口标识不对或尚未发布；409 表示同时传了 Enterprise SSO 用户令牌和外部用户编号。</p>
                </details>

              </div>
            </>
          )}
        </section>
        </div>
      </section>

      {createOpen && (
        <div className="modalBackdrop centered" role="presentation" onClick={() => busy === null && setCreateOpen(false)}>
          <form className="applicationAgentCreateDialog" role="dialog" aria-modal="true" aria-label="新建应用 Agent" onSubmit={(event) => void createAgent(event)} onClick={(event) => event.stopPropagation()}>
            <h2>新建应用 Agent</h2>
            <label>名称<input required maxLength={100} value={createDraft.name} onChange={(event) => setCreateDraft({ ...createDraft, name: event.target.value })}/></label>
            <label>接口标识<input required pattern="[a-z0-9]+(?:-[a-z0-9]+)*" maxLength={100} placeholder="ticket-assistant" value={createDraft.slug} onChange={(event) => setCreateDraft({ ...createDraft, slug: event.target.value.toLowerCase() })}/></label>
            <footer><button type="button" onClick={() => setCreateOpen(false)} disabled={busy !== null}>取消</button><button type="submit" disabled={busy !== null}>{busy === "create" ? "创建中…" : "创建"}</button></footer>
          </form>
        </div>
      )}
    </main>
  );
}
