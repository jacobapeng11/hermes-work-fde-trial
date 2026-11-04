import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { Check, Settings } from "lucide-react";
import type {
  ApplicationAgentConfig,
  ApplicationAgentDetail,
  ApplicationAgentSkillSummary,
  SystemSkillSummary,
  ProfileMcpAssetItem,
  ProfileSkillConfiguredItem,
  ProfileSkillCatalogItem,
  PlazaSkillView,
  SkillCategorySummary,
  RuntimeModelSummary,
  UserAssetCatalogView
} from "@hermes-cloud/schemas";
import { userApi } from "../api/client";
import type { KnowledgeWorkspaceAdapter } from "../components/knowledge";
import type { SkillWorkspaceController } from "../components/skills";
import { ApplicationAgentWeComPanel } from
  "../components/ApplicationAgentWeComPanel";
import { waitForApplicationConfigApplyAfterBusy } from
  "./applicationRuntimeRecovery";
import { ApplicationAgentClientPanel } from "./ApplicationAgentClientPanel";

const PlazaSkillPanelView = lazy(() =>
  import("../components/skills").then((m) => ({ default: m.PlazaSkillPanelView })));
const KnowledgePanelView = lazy(() =>
  import("../components/knowledge").then((m) => ({ default: m.KnowledgePanelView })));
// McpWorkspace antd 化后必须留在懒加载 chunk（本组件在 AgentWorkspace 静态图内），
// McpWorkspaceView 自带 AntdModuleProviders。
const McpWorkspaceView = lazy(() =>
  import("../components/mcp").then((m) => ({ default: m.McpWorkspaceView })));

type ApplicationConfigurationSection =
  | "skills"
  | "mcps"
  | "knowledge"
  | "channels"
  | "settings"
  | "clients";

interface ApplicationAgentConfigurationPanelProps {
  applicationAgentId: number;
  section: ApplicationConfigurationSection;
  onConfigurationApplied?: (agent: ApplicationAgentDetail) => void;
}

const emptySkillView: PlazaSkillView = {
  marketItems: [],
  installedItems: [],
  categories: [],
  pendingOperations: []
};

const skillViewFromCatalog = (
  catalog: UserAssetCatalogView,
  categories: SkillCategorySummary[]
): PlazaSkillView => {
  const byKey = new Map(
    categories.map((item) => [item.categoryKey, item])
  );
  catalog.skills.forEach((item) => {
    const key = item.categoryKey ?? "other";
    if (!byKey.has(key)) {
      byKey.set(key, {
        categoryKey: key,
        displayName: key,
        isBuiltin: true,
      });
    }
  });
  return {
    ...emptySkillView,
    marketItems: catalog.skills.map((item) => ({
      assetId: item.assetId,
      name: item.name,
      description: item.description,
      categoryKey: item.categoryKey ?? "other",
      currentVersionLabel: String(item.version ?? "latest"),
      state: "installable",
      operating: false,
    })),
    categories: [...byKey.values()],
  };
};

const mcpsFromCatalog = (catalog: UserAssetCatalogView): ProfileMcpAssetItem[] => (
  catalog.mcps.map((item) => ({
    assetId: item.assetId,
    currentVersionId: item.currentVersionId,
    slug: `mcp-${item.assetId}`,
    name: item.name,
    description: item.description,
    transport: item.transport ?? "platform",
    status: item.status,
    desired: false,
    applied: false,
    convergenceStatus: "applied",
  }))
);

export function ApplicationAgentConfigurationPanel({
  applicationAgentId,
  section,
  onConfigurationApplied
}: ApplicationAgentConfigurationPanelProps) {
  const [agent, setAgent] = useState<ApplicationAgentDetail | null>(null);
  const agentRef = useRef<ApplicationAgentDetail | null>(null);
  const [settingsDraft, setSettingsDraft] =
    useState<ApplicationAgentConfig | null>(null);
  const [settingsBaseline, setSettingsBaseline] =
    useState<ApplicationAgentConfig | null>(null);
  const [models, setModels] = useState<RuntimeModelSummary[]>([]);
  const [skillView, setSkillView] =
    useState<PlazaSkillView>(emptySkillView);
  const [applicationSkills, setApplicationSkills] = useState<
    ApplicationAgentSkillSummary[]
  >([]);
  const [systemSkills, setSystemSkills] = useState<SystemSkillSummary[]>([]);
  const [skillLoading, setSkillLoading] = useState(false);
  const [mcps, setMcps] = useState<ProfileMcpAssetItem[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const savingRef = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [settingsSavedFor, setSettingsSavedFor] = useState<number | null>(null);
  const recoveryAbortControllerRef = useRef(new AbortController());

  useEffect(() => {
    if (recoveryAbortControllerRef.current.signal.aborted) {
      recoveryAbortControllerRef.current = new AbortController();
    }
    const controller = recoveryAbortControllerRef.current;
    return () => controller.abort();
  }, [applicationAgentId]);

  const applyAgent = useCallback((detail: ApplicationAgentDetail) => {
    agentRef.current = detail;
    setAgent(detail);
    setSettingsBaseline(detail.config);
    setSettingsDraft(detail.config);
    return detail;
  }, []);

  const settingsDirty = useMemo(
    () => settingsDraft !== null
      && settingsBaseline !== null
      && JSON.stringify(settingsDraft) !== JSON.stringify(settingsBaseline),
    [settingsBaseline, settingsDraft]
  );

  useEffect(() => {
    if (!settingsDirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener(
      "beforeunload",
      warnBeforeUnload
    );
  }, [settingsDirty]);

  const loadAgent = useCallback(async () => {
    const detail = await userApi.applicationAgent(applicationAgentId);
    return applyAgent(detail);
  }, [applicationAgentId, applyAgent]);

  useEffect(() => {
    let active = true;
    setError(null);
    Promise.all([
      userApi.applicationAgent(applicationAgentId),
      userApi.runtimeModels()
    ]).then(([detail, nextModels]) => {
      if (!active) return;
      applyAgent(detail);
      setModels(nextModels);
    }).catch((reason) => {
      if (active) {
        setError(
          reason instanceof Error
            ? reason.message
            : "加载应用 Agent 配置失败"
        );
      }
    });
    return () => {
      active = false;
    };
  }, [applicationAgentId, applyAgent]);

  const saveConfig = useCallback(async (
    nextConfig: ApplicationAgentConfig,
    operationKey: string
  ): Promise<ApplicationAgentDetail> => {
    const current = agentRef.current;
    if (!current) throw new Error("应用 Agent 配置尚未加载");
    if (savingRef.current) throw new Error("上一项配置正在保存");
    savingRef.current = operationKey;
    setSaving(operationKey);
    setSettingsSavedFor(null);
    setError(null);
    setNotice(null);
    try {
      const expectedGeneration = current.configGeneration + 1;
      let updated: ApplicationAgentDetail;
      try {
        updated = await userApi.updateApplicationAgent(
          applicationAgentId,
          {
            name: current.name,
            avatarUrl: current.avatarUrl,
            config: nextConfig,
            expectedConfigGeneration: current.configGeneration
          }
        );
      } catch (reason) {
        updated = await waitForApplicationConfigApplyAfterBusy({
          reason,
          expectedGeneration,
          targetConfig: nextConfig,
          loadAgent: () => userApi.applicationAgent(applicationAgentId),
          signal: recoveryAbortControllerRef.current.signal
        });
      }
      applyAgent(updated);
      // 资产类操作（skill/MCP/知识库开关）的成功反馈交给每张卡自己的按钮状态
      // 与徽章（"安装中/生效中/已安装"）——与个人页的反馈节奏保持一致；
      // 顶层横幅仅在 Runtime 应用失败时出现，避免冗余的"配置 #N 已保存"。
      if (updated.applyStatus === "failed") {
        setNotice(
          `配置已保存，但 Runtime 仍停留在 #${
            updated.appliedGeneration ?? 0
          }`
        );
      } else {
        setNotice(null);
        if (operationKey === "settings" && updated.applyStatus === "applied"
          && updated.appliedGeneration === updated.configGeneration) {
          setSettingsSavedFor(applicationAgentId);
        }
      }
      onConfigurationApplied?.(updated);
      return updated;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存配置失败");
      throw reason;
    } finally {
      savingRef.current = null;
      setSaving(null);
    }
  }, [applicationAgentId, applyAgent, onConfigurationApplied]);

  const loadSkills = useCallback(async () => {
    setSkillLoading(true);
    setError(null);
    try {
      const [catalog, categories, privateSkills] = await Promise.all([
        userApi.userAssetCatalog(),
        userApi.personalSkillCategories(),
        userApi.applicationAgentSkills(applicationAgentId)
      ]);
      setSkillView(skillViewFromCatalog(catalog, categories));
      setApplicationSkills(privateSkills);
      try {
        const loadedSystemSkills = await userApi.systemSkills();
        setSystemSkills(Array.isArray(loadedSystemSkills) ? loadedSystemSkills : []);
      } catch {
        setSystemSkills([]);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载技能失败");
      throw reason;
    } finally {
      setSkillLoading(false);
    }
  }, [applicationAgentId]);

  useEffect(() => {
    if (section !== "mcps") return;
    let active = true;
    setMcpLoading(true);
    setError(null);
    userApi.userAssetCatalog().then(mcpsFromCatalog).then((items) => {
      if (active) {
        setMcps(items.filter((item) => item.status === "published"));
      }
    }).catch((reason) => {
      if (active) {
        setError(reason instanceof Error ? reason.message : "加载 MCP 失败");
      }
    }).finally(() => {
      if (active) setMcpLoading(false);
    });
    return () => {
      active = false;
    };
  }, [section]);

  const configuredSkillIds = agent?.config.skillAssetIds ?? [];
  const applicationMarketItems = useMemo<ProfileSkillCatalogItem[]>(
    () => skillView.marketItems.map((item) => ({
      ...item,
      state: configuredSkillIds.includes(item.assetId)
        ? "current"
        : "installable",
      configuredAssetId: configuredSkillIds.includes(
        item.assetId
      ) ? item.assetId : null,
      installedVersionLabel: configuredSkillIds.includes(
        item.assetId
      ) ? item.currentVersionLabel : null,
      operating: saving === `skill-install:${item.assetId}`
        || saving === `skill-remove:${item.assetId}`
    })),
    [configuredSkillIds, saving, skillView.marketItems]
  );
  const applicationInstalledItems = useMemo<ProfileSkillConfiguredItem[]>(
    () => [
      ...systemSkills.map((item, index) => ({
        assetId: -(index + 1),
        source: "system" as const,
        name: item.name,
        description: item.description,
        categoryKey: item.categoryKey,
        versionLabel: "内置",
        updateStatus: "not_applicable" as const,
        catalogAssetId: null,
        operating: false
      })),
      ...applicationMarketItems
        .filter((item) => configuredSkillIds.includes(
          item.assetId
        ))
        .map((item) => ({
          assetId: item.assetId,
          source: "market" as const,
          name: item.name,
          description: item.description,
          categoryKey: item.categoryKey,
          versionLabel: item.currentVersionLabel,
          updateStatus: "current" as const,
          catalogAssetId: item.assetId,
          operating: saving === `skill-remove:${item.assetId}`
        })),
      ...applicationSkills.map((item) => ({
        assetId: item.applicationAgentSkillId,
        applicationAgentSkillId: item.applicationAgentSkillId,
        source: "application" as const,
        name: item.name,
        description: item.description,
        categoryKey: item.categoryKey,
        versionLabel: `v${item.version ?? 1}`,
        updateStatus: "current" as const,
        catalogAssetId: null,
        operating: saving === (
          `application-skill-remove:${item.applicationAgentSkillId}`
        )
      }))
    ],
    [
      applicationMarketItems,
      applicationSkills,
      systemSkills,
      configuredSkillIds,
      saving
    ]
  );

  const skillController = useMemo<SkillWorkspaceController>(() => ({
    marketItems: applicationMarketItems,
    personalItems: [],
    installedItems: applicationInstalledItems,
    categories: skillView.categories,
    pendingOperations: [],
    loading: skillLoading,
    error: section === "skills" ? error : null,
    load: loadSkills,
    submitInstall: async (assetId: number) => {
      const current = agentRef.current;
      if (!current || current.config.skillAssetIds.includes(assetId)) return;
      await saveConfig({
        ...current.config,
        skillAssetIds: [...current.config.skillAssetIds, assetId]
      }, `skill-install:${assetId}`).catch(() => undefined);
    },
    submitUninstall: async (
      skillId: number,
      catalogAssetId?: number | null
    ) => {
      if (catalogAssetId == null) {
        const operationKey = `application-skill-remove:${skillId}`;
        if (savingRef.current) throw new Error("上一项配置正在保存");
        savingRef.current = operationKey;
        setSaving(operationKey);
        setError(null);
        setNotice(null);
        try {
          await userApi.deleteApplicationAgentSkill(
            applicationAgentId,
            skillId
          );
          await Promise.all([loadAgent(), loadSkills()]);
          setNotice("应用 Agent 私有技能已卸载");
        } catch (reason) {
          setError(
            reason instanceof Error
              ? reason.message
              : "卸载应用 Agent 技能失败"
          );
          throw reason;
        } finally {
          savingRef.current = null;
          setSaving(null);
        }
        return;
      }
      const assetId = catalogAssetId;
      const current = agentRef.current;
      if (!current || !current.config.skillAssetIds.includes(assetId)) return;
      await saveConfig({
        ...current.config,
        skillAssetIds: current.config.skillAssetIds.filter(
          (item) => item !== assetId
        )
      }, `skill-remove:${assetId}`).catch(() => undefined);
    },
    submitUpload: async (file: File, categoryKey: string, description?: string) => {
      const operationKey = "application-skill-upload";
      if (savingRef.current) throw new Error("上一项配置正在保存");
      savingRef.current = operationKey;
      setSaving(operationKey);
      setError(null);
      setNotice(null);
      try {
        if (description === undefined) {
          await userApi.uploadApplicationAgentSkill(
            applicationAgentId,
            file,
            categoryKey,
          );
        } else {
          await userApi.uploadApplicationAgentSkill(
            applicationAgentId,
            file,
            categoryKey,
            description,
          );
        }
        await Promise.all([loadAgent(), loadSkills()]);
        setNotice("技能已上传并安装到当前应用 Agent");
      } catch (reason) {
        setError(
          reason instanceof Error
            ? reason.message
            : "上传应用 Agent 技能失败"
        );
        throw reason;
      } finally {
        savingRef.current = null;
        setSaving(null);
      }
    },
    submitDescriptionUpdate: async (
      skillId: number,
      description: string | null,
      name?: string,
    ) => {
      const operationKey = `application-skill-description:${skillId}`;
      if (savingRef.current) throw new Error("上一项配置正在保存");
      savingRef.current = operationKey;
      setSaving(operationKey);
      setError(null);
      setNotice(null);
      try {
        const updated = await userApi.updateApplicationAgentSkillDescription(
          applicationAgentId,
          skillId,
          description,
          name,
        );
        await loadSkills();
        setNotice("应用 Agent 技能信息已保存");
        return updated;
      } catch (reason) {
        setError(
          reason instanceof Error
            ? reason.message
            : "保存应用 Agent 技能信息失败",
        );
        throw reason;
      } finally {
        savingRef.current = null;
        setSaving(null);
      }
    }
  }), [
    applicationAgentId,
    applicationInstalledItems,
    applicationMarketItems,
    applicationSkills,
    error,
    loadSkills,
    loadAgent,
    saveConfig,
    section,
    skillLoading,
    skillView.categories
  ]);

  const knowledgeAdapter = useMemo<KnowledgeWorkspaceAdapter>(() => ({
    context: "application",
    getEnterpriseSettings: async () => {
      const current = agentRef.current ?? await loadAgent();
      return { enabled: current.config.enterpriseKnowledgeEnabled };
    },
    updateEnterpriseSettings: async (enabled: boolean) => {
      const current = agentRef.current ?? await loadAgent();
      const updated = await saveConfig({
        ...current.config,
        enterpriseKnowledgeEnabled: enabled
      }, "enterprise-knowledge");
      return { enabled: updated.config.enterpriseKnowledgeEnabled };
    },
    personalKnowledge: {
      assetLabel: "个人知识库",
      canManageAssets: false,
      canOpenAssets: false,
      list: async (ownership: string, keyword: string) => {
        const catalog = await userApi.applicationAgentPersonalKnowledge(
          applicationAgentId
        );
        const normalizedKeyword = keyword.trim().toLocaleLowerCase();
        const items = catalog.items
          .filter((item) => (
            ownership === "all"
            || (ownership === "owned" && item.effectiveRole === "owner")
            || (ownership === "shared" && item.effectiveRole !== "owner")
          ))
          .filter((item) => (
            !normalizedKeyword
            || item.name.toLocaleLowerCase().includes(normalizedKeyword)
          ))
          .map((item) => ({
            ...item,
            runtimeEnabled: item.selected
          }));
        return {
          items,
          total: items.length,
          revokedCount: catalog.revokedCount
        };
      },
      setEnabled: async (knowledgeBaseId, enabled) => {
        const catalog = enabled
          ? await userApi.selectApplicationAgentPersonalKnowledge(
              applicationAgentId,
              knowledgeBaseId
            )
          : await userApi.disableApplicationAgentPersonalKnowledge(
              applicationAgentId,
              knowledgeBaseId
            );
        await loadAgent();
        return {
          enabled: catalog.items.some(
            (item) => item.knowledgeBaseId === knowledgeBaseId
              && item.selected
          )
        };
      }
    }
  }), [applicationAgentId, loadAgent, saveConfig]);

  const configuredModelUnavailable = settingsDraft?.modelEntryId != null
    && !models.some(
      (model) => model.modelEntryId === settingsDraft.modelEntryId
    );
  const fallbackModel = models.find((model) => model.isDefault) ?? models[0];
  const modelSelectValue = configuredModelUnavailable
    ? "model-unavailable"
    : settingsDraft?.modelEntryId ?? "";

  const renderSettings = () => (
    <section className="applicationWorkspaceSettings">
      <div className="assetHeader">
        <Settings size={18}/>
        <div>
          <h2>Agent 配置</h2>
          <p>设置系统提示词、默认模型和长期会话策略。</p>
        </div>
      </div>
      {agent && settingsDraft && (
        <form
          className="applicationAgentConfigPanel"
          onSubmit={(event) => {
            event.preventDefault();
            if (!settingsDirty) return;
            void saveConfig(settingsDraft, "settings").catch(
              () => undefined
            );
          }}
        >
          <div
            className="applicationAgentConfigStatus"
            role="status"
          >
            {settingsSavedFor === applicationAgentId && !settingsDirty
              ? (
                <span className="applicationAgentConfigSuccess">
                  <Check size={14} aria-hidden="true" />已保存并应用
                </span>
              )
              : saving === "settings"
              ? "正在保存并应用"
              : settingsDirty
                ? "有未保存修改"
              : agent.applyStatus === "applying"
                ? "已保存，正在应用"
                : agent.applyStatus === "failed"
                  ? "配置应用失败"
                  : "配置已应用"}
          </div>
          <label>
            系统提示词
            <textarea
              rows={12}
              maxLength={50_000}
              value={settingsDraft.systemPrompt}
              onChange={(event) => {
                setSettingsSavedFor(null);
                setSettingsDraft({
                  ...settingsDraft,
                  systemPrompt: event.target.value
                });
              }}
            />
          </label>
          <label>
            默认模型
            <select
              value={modelSelectValue}
              onChange={(event) => {
                setSettingsSavedFor(null);
                setSettingsDraft({
                  ...settingsDraft,
                  modelEntryId: event.target.value
                    ? Number(event.target.value)
                    : null
                });
              }}
            >
              {configuredModelUnavailable && (
                <option value="model-unavailable">
                  当前配置模型已不可用
                </option>
              )}
              <option value="">
                平台默认模型
                {fallbackModel?.isDefault
                  ? `（当前：${fallbackModel.displayName}）`
                  : fallbackModel
                    ? `（将回退为：${fallbackModel.displayName}）`
                  : ""}
              </option>
              {models.map((model) => (
                <option
                  key={model.modelEntryId}
                  value={model.modelEntryId}
                >
                  {model.displayName}
                </option>
              ))}
            </select>
          </label>
          {configuredModelUnavailable && (
            <p className="adminInlineError applicationAgentModelError" role="alert">
              当前配置模型已不可用，请重新选择。保存前请选择平台默认模型或一个当前可见模型。
            </p>
          )}
          <label className="applicationAgentCheckbox">
            <input
              type="checkbox"
              checked={settingsDraft.memoryEnabled}
              onChange={(event) => {
                setSettingsSavedFor(null);
                setSettingsDraft({
                  ...settingsDraft,
                  memoryEnabled: event.target.checked
                });
              }}
            />
            为外部长期会话启用应用级记忆
          </label>
          <div className="applicationAgentConfigActions">
            <button
              type="button"
              disabled={!settingsDirty || saving !== null}
              onClick={() => setSettingsDraft(settingsBaseline)}
            >
              取消修改
            </button>
            <button
              type="submit"
              disabled={!settingsDirty || saving !== null}
            >
              {saving === "settings" ? "保存中…" : "保存并应用"}
            </button>
          </div>
        </form>
      )}
    </section>
  );

  const mcpItems = useMemo(
    () => {
      const configGen = agent?.configGeneration ?? 0;
      const appliedGen = agent?.appliedGeneration ?? 0;
      const isApplying = agent?.applyStatus === "applying" && appliedGen !== configGen;
      const isApplied = agent?.applyStatus === "applied" && appliedGen === configGen;
      const isFailed = agent?.applyStatus === "failed" && appliedGen !== configGen;
      const operationInFlight = isApplying || isFailed;
      return mcps.map((item) => {
        const desired = agent?.config.mcpAssetIds.includes(item.assetId) ?? false;
        // Runtime 还没真正应用新配置时，不要把 applied 跟随 desired 一起翻——
        // 这样 McpWorkspace 才能展示"安装中/删除中"与"正在更新 Runtime 配置…"
        // 的收敛过程，与个人页 MCP 卡片的节奏保持一致。
        const applied = operationInFlight
          ? !desired
          : isApplied
            ? desired
            : desired;
        const convergenceStatus = isFailed
          ? "failed"
          : isApplying || desired !== applied
            ? "updating"
            : "applied";
        return {
          ...item,
          desired,
          applied,
          convergenceStatus,
        };
      });
    },
    [
      agent?.applyStatus,
      agent?.appliedGeneration,
      agent?.configGeneration,
      agent?.config.mcpAssetIds,
      mcps
    ]
  );

  return <section className="applicationAgentConfigurationView">
    {error && section !== "skills" && (
      <div className="errorNotice" role="alert">{error}</div>
    )}
    {notice && (
      <div className="applicationAgentNotice" role="status">{notice}</div>
    )}
    {section === "skills" && (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><span>正在加载技能…</span></div>}>
        <PlazaSkillPanelView
          controller={skillController}
          allowUpload
          installTargetLabel="当前应用 Agent"
          uploadDialogLabel="上传应用 Agent 技能"
          uploadTitle="上传技能配置"
          uploadDescription="上传包含 SKILL.md 的 zip 包，技能只会安装到当前应用 Agent。"
        />
      </Suspense>
    )}
    {section === "mcps" && (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><span>正在加载 MCP…</span></div>}>
      <McpWorkspaceView
        items={mcpItems}
        busyKey={saving?.startsWith("mcp:") ? saving : null}
        statusMessage={mcpLoading ? "正在加载 MCP 广场…" : null}
        error={null}
        loadDetail={async (asset) => ({
          asset,
          detail: {
            transport: mcps.find(
              (item) => item.assetId === asset.assetId
            )?.transport ?? "platform",
            endpoint: "平台统一 MCP Registry",
          },
        })}
        onAction={async (asset, action) => {
          const current = agentRef.current;
          if (!current) return;
          if (!window.confirm(
            "MCP 配置会立即重载，正在执行的工具调用可能中断且不会自动重试。是否继续？"
          )) return;
          const selected = current.config.mcpAssetIds.includes(asset.assetId);
          if (
            (action === "install" && selected)
            || (action === "remove" && !selected)
          ) return;
          await saveConfig({
            ...current.config,
            mcpAssetIds: action === "install"
              ? [...current.config.mcpAssetIds, asset.assetId]
              : current.config.mcpAssetIds.filter(
                  (item) => item !== asset.assetId
                )
          }, `mcp:${asset.assetId}`);
        }}
      />
      </Suspense>
    )}
    {section === "knowledge" && (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><span>正在加载知识库…</span></div>}>
        <KnowledgePanelView
          runtimeWorkspaceId={null}
          adapter={knowledgeAdapter}
        />
      </Suspense>
    )}
    {section === "channels" && (
      <ApplicationAgentWeComPanel
        applicationAgentId={applicationAgentId}
      />
    )}
    {section === "settings" && renderSettings()}
    {section === "clients" && (
      <ApplicationAgentClientPanel applicationAgentId={applicationAgentId} />
    )}
  </section>;
}
