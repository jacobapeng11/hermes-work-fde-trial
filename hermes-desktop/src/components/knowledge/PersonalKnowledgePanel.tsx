import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Input, Select } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import type {
  PersonalKnowledgeListResponse,
  PersonalKnowledgeSummary
} from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { KnowledgeCard } from "./KnowledgeCard";
import { PersonalKnowledgeDeleteDialog } from "./PersonalKnowledgeDeleteDialog";
import { PersonalKnowledgeDetail } from "./PersonalKnowledgeDetail";
import { PersonalKnowledgeFormDialog } from "./PersonalKnowledgeFormDialog";
import { PersonalKnowledgePermissionsDialog } from "./PersonalKnowledgePermissionsDialog";
import type { PersonalKnowledgeAssetApi } from "./personalKnowledgeAssetApi";

interface Props {
  runtimeWorkspaceId: number | null;
  adapter?: PersonalKnowledgeWorkspaceAdapter;
  /** 页头「新建知识库」按钮的递增信号：变化且 >0 时打开创建弹窗。 */
  createTrigger?: number;
  /** 详情层切换上报：页头据此在详情页隐藏「新建知识库」按钮。 */
  onDetailViewChange?: (active: boolean) => void;
}

export interface PersonalKnowledgeWorkspaceAdapter {
  list: (
    ownership: string,
    keyword: string
  ) => Promise<PersonalKnowledgeListResponse & { revokedCount?: number }>;
  setEnabled: (
    knowledgeBaseId: number,
    enabled: boolean
  ) => Promise<{ enabled: boolean; warning?: "no_ready_files" | null }>;
  canManageAssets?: boolean;
  canOpenAssets?: boolean;
  assetApi?: PersonalKnowledgeAssetApi;
  assetLabel?: string;
}

const roleLabel = {
  owner: "我创建的",
  download: "可预览和下载",
  preview: "仅在线预览",
  retrieve: "仅勾选检索"
} as const;

const roleColor = {
  owner: "blue",
  download: "green",
  preview: "default",
  retrieve: "gold"
} as const;

interface ToggleFeedback {
  kind: "notice" | "error";
  message: string;
}

export function PersonalKnowledgePanel({
  runtimeWorkspaceId,
  adapter,
  createTrigger = 0,
  onDetailViewChange
}: Props) {
  const [items, setItems] = useState<PersonalKnowledgeSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [ownership, setOwnership] = useState("all");
  const [detailId, setDetailId] = useState<number | null>(null);
  const [sharing, setSharing] = useState<PersonalKnowledgeSummary | null>(null);
  const [editing, setEditing] = useState<PersonalKnowledgeSummary | null>(null);
  const [deleting, setDeleting] = useState<PersonalKnowledgeSummary | null>(null);
  const [creating, setCreating] = useState(false);
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set());
  const [toggleFeedback, setToggleFeedback] = useState<Record<number, ToggleFeedback>>({});
  const [revokedCount, setRevokedCount] = useState(0);
  const itemsRef = useRef<PersonalKnowledgeSummary[]>([]);
  const busyIdsRef = useRef<Set<number>>(new Set());
  const mutationVersionsRef = useRef<Map<number, number>>(new Map());
  const listRequestSequenceRef = useRef(0);
  const canManageAssets = adapter?.canManageAssets ?? true;
  const canOpenAssets = adapter?.canOpenAssets ?? true;
  const assetApi = adapter?.assetApi;
  const assetLabel = adapter?.assetLabel ?? "个人知识库";
  const canConfigureRetrieval = Boolean(runtimeWorkspaceId || adapter);

  useEffect(() => {
    if (createTrigger > 0 && canManageAssets) setCreating(true);
  }, [createTrigger, canManageAssets]);

  useEffect(() => {
    onDetailViewChange?.(detailId !== null);
  }, [detailId, onDetailViewChange]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedKeyword(keyword), 250);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  const load = useCallback(async () => {
    const requestSequence = ++listRequestSequenceRef.current;
    const mutationVersionsAtStart = new Map(mutationVersionsRef.current);
    setLoading(true);
    setError(null);
    try {
      const response = adapter
        ? await adapter.list(ownership, debouncedKeyword)
        : await userApi.listPersonalKnowledge(
            runtimeWorkspaceId,
            ownership,
            debouncedKeyword
          );
      if (requestSequence !== listRequestSequenceRef.current) return;
      const responseWithRevoked = response as (
        PersonalKnowledgeListResponse & { revokedCount?: number }
      );
      setRevokedCount(responseWithRevoked.revokedCount ?? 0);
      setItems((current) => {
        const currentById = new Map(current.map((item) => [item.knowledgeBaseId, item]));
        const nextItems = response.items.map((item) => {
          const versionAtStart = mutationVersionsAtStart.get(item.knowledgeBaseId) ?? 0;
          const currentVersion = mutationVersionsRef.current.get(item.knowledgeBaseId) ?? 0;
          const currentItem = currentById.get(item.knowledgeBaseId);
          return currentItem && currentVersion !== versionAtStart
            ? { ...item, runtimeEnabled: currentItem.runtimeEnabled }
            : item;
        });
        itemsRef.current = nextItems;
        return nextItems;
      });
    } catch (reason) {
      if (requestSequence !== listRequestSequenceRef.current) return;
      setError(reason instanceof Error ? reason.message : "读取个人知识库失败");
    } finally {
      if (requestSequence === listRequestSequenceRef.current) setLoading(false);
    }
  }, [adapter, runtimeWorkspaceId, ownership, debouncedKeyword]);

  useEffect(() => {
    void load();
    return () => { listRequestSequenceRef.current += 1; };
  }, [load]);

  const toggle = async (knowledgeBaseId: number, enabled: boolean) => {
    if (!canConfigureRetrieval || busyIdsRef.current.has(knowledgeBaseId)) return;
    const previous = itemsRef.current.find((item) => item.knowledgeBaseId === knowledgeBaseId)?.runtimeEnabled;
    if (previous === undefined || previous === enabled) return;
    const mutationVersion = (mutationVersionsRef.current.get(knowledgeBaseId) ?? 0) + 1;
    mutationVersionsRef.current.set(knowledgeBaseId, mutationVersion);
    const optimisticItems = itemsRef.current.map((candidate) => candidate.knowledgeBaseId === knowledgeBaseId ? { ...candidate, runtimeEnabled: enabled } : candidate);
    itemsRef.current = optimisticItems;
    setItems(optimisticItems);
    busyIdsRef.current.add(knowledgeBaseId);
    setBusyIds(new Set(busyIdsRef.current));
    setToggleFeedback((current) => {
      const next = { ...current };
      delete next[knowledgeBaseId];
      return next;
    });
    try {
      const response = adapter
        ? await adapter.setEnabled(knowledgeBaseId, enabled)
        : await userApi.setPersonalKnowledgeEnabled(
            runtimeWorkspaceId as number,
            knowledgeBaseId,
            enabled
          );
      if (mutationVersionsRef.current.get(knowledgeBaseId) !== mutationVersion) return;
      const confirmedItems = itemsRef.current.map((candidate) => candidate.knowledgeBaseId === knowledgeBaseId ? { ...candidate, runtimeEnabled: response.enabled } : candidate);
      itemsRef.current = confirmedItems;
      setItems(confirmedItems);
      setToggleFeedback((current) => ({
        ...current,
        [knowledgeBaseId]: {
          kind: "notice",
          message: response.warning === "no_ready_files" ? "已勾选；暂无可检索文件，解析完成后自动生效" : "已保存，将从下一轮对话生效"
        }
      }));
    } catch (reason) {
      if (mutationVersionsRef.current.get(knowledgeBaseId) !== mutationVersion) return;
      const rolledBackItems = itemsRef.current.map((candidate) => candidate.knowledgeBaseId === knowledgeBaseId ? { ...candidate, runtimeEnabled: previous } : candidate);
      itemsRef.current = rolledBackItems;
      setItems(rolledBackItems);
      setToggleFeedback((current) => ({
        ...current,
        [knowledgeBaseId]: {
          kind: "error",
          message: reason instanceof Error ? reason.message : "保存勾选状态失败"
        }
      }));
    } finally {
      if (mutationVersionsRef.current.get(knowledgeBaseId) === mutationVersion) {
        busyIdsRef.current.delete(knowledgeBaseId);
        setBusyIds(new Set(busyIdsRef.current));
      }
    }
  };

  const create = async (value: { name: string; description: string | null }) => {
    const created = assetApi
      ? await assetApi.create(value)
      : await userApi.createPersonalKnowledge(value);
    setDetailId(created.knowledgeBaseId);
  };

  const remove = async (item: PersonalKnowledgeSummary) => {
    if (assetApi) await assetApi.remove(item.knowledgeBaseId);
    else await userApi.deletePersonalKnowledge(item.knowledgeBaseId);
    listRequestSequenceRef.current += 1;
    setLoading(false);
    const nextItems = itemsRef.current.filter(
      (candidate) => candidate.knowledgeBaseId !== item.knowledgeBaseId
    );
    itemsRef.current = nextItems;
    setItems(nextItems);
    busyIdsRef.current.delete(item.knowledgeBaseId);
    setBusyIds(new Set(busyIdsRef.current));
    mutationVersionsRef.current.delete(item.knowledgeBaseId);
    setToggleFeedback((current) => {
      const next = { ...current };
      delete next[item.knowledgeBaseId];
      return next;
    });
  };

  const clearFilters = () => {
    setKeyword("");
    setDebouncedKeyword("");
    setOwnership("all");
  };

  if (detailId !== null) return <PersonalKnowledgeDetail knowledgeBaseId={detailId} assetApi={assetApi} assetLabel={assetLabel} onBack={() => { setDetailId(null); void load(); }}/>;

  const hasActiveFilters = Boolean(debouncedKeyword.trim() || ownership !== "all");

  return <section id="personal-knowledge-panel" className="pkbPanel">
    <p className="pkbPanelLead">{adapter && !canManageAssets ? "为当前应用逐库选择可检索的个人知识；内容管理仍在个人 Agent 中完成。" : assetApi ? "由当前应用 Agent 独立拥有，所有已授权入口均可使用。" : "集中管理个人资料与共享内容，并选择哪些知识库参与下一轮对话检索。"}</p>
    {revokedCount > 0 && <Alert className="pkbRevokedAlert" type="warning" showIcon message={`有 ${revokedCount} 个已选择的个人知识库因权限撤销而停止使用。重新获得共享权限后，请手动重新选择。`} />}
    <div className="pkbToolbar">
      <div className="pkbToolbarControls">
        <Input
          className="pkbSearchInput"
          prefix={<SearchOutlined aria-hidden />}
          aria-label="搜索个人知识库"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="搜索知识库名称"
          allowClear
        />
        {!assetApi && <Select
          className="pkbOwnershipSelect"
          aria-label="知识库归属"
          value={ownership}
          onChange={(value) => setOwnership(value)}
          options={[
            { value: "all", label: "全部知识库" },
            { value: "owned", label: "我创建的" },
            { value: "shared", label: "分享给我的" }
          ]}
        />}
      </div>
      <span className="pkbResultCount">{loading ? "正在更新…" : `当前显示 ${items.length} 个`}</span>
    </div>
    {error && <Alert className="pkbListErrorAlert" type="error" showIcon message={error} action={<Button size="small" autoInsertSpace={false} onClick={() => void load()}>重试</Button>} />}
    {loading && items.length > 0 && <p className="pkbLoading pkbBackgroundLoading" role="status">正在更新列表…</p>}
    {loading && items.length === 0 ? <p className="pkbLoading pkbInitialLoading" role="status">正在读取个人知识库…</p> : !error && items.length === 0 ? hasActiveFilters ? <div className="pkbEmpty pkbFilterEmpty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<><h3>未找到匹配的知识库</h3><p>可以调整搜索词或知识库归属，查看其他结果。</p></>} /><Button autoInsertSpace={false} onClick={clearFilters}>清除筛选</Button></div> : <div className="pkbEmpty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<><h3>{canManageAssets ? "从第一个个人知识库开始" : "暂无可绑定的个人知识库"}</h3><p>{canManageAssets ? "创建知识库并上传资料，也可以按权限分享给同事共同使用。" : "请先在个人 Agent 中创建知识库，或让同事向你共享知识库。"}</p></>} />{canManageAssets && <Button type="primary" autoInsertSpace={false} onClick={() => setCreating(true)}>新建个人知识库</Button>}</div> : items.length > 0 ? <div className="pkbGrid">{items.map((item) => {
      const canEnter = canOpenAssets && item.effectiveRole !== "retrieve";
      return <KnowledgeCard
        key={item.knowledgeBaseId}
        item={item}
        applicationOwned={Boolean(assetApi)}
        roleText={assetApi ? "应用专属" : roleLabel[item.effectiveRole]}
        roleColor={assetApi ? "blue" : roleColor[item.effectiveRole]}
        canEnter={canEnter}
        canConfigureRetrieval={canConfigureRetrieval}
        canManageAssets={canManageAssets}
        canOpenAssets={canOpenAssets}
        busy={busyIds.has(item.knowledgeBaseId)}
        feedback={toggleFeedback[item.knowledgeBaseId]}
        onToggle={(enabled) => void toggle(item.knowledgeBaseId, enabled)}
        onOpen={() => setDetailId(item.knowledgeBaseId)}
        onEdit={() => setEditing(item)}
        onShare={() => setSharing(item)}
        onDelete={() => setDeleting(item)}
      />;
    })}</div> : null}
    {canManageAssets && creating && <PersonalKnowledgeFormDialog title="创建个人知识库" supportingText="创建完成后将直接进入详情页上传文件" submitLabel="创建并上传文件" pendingLabel="创建中…" onClose={() => setCreating(false)} onSubmit={create}/>}
    {canManageAssets && editing && (
      <PersonalKnowledgeFormDialog
        key={editing.knowledgeBaseId}
        title="编辑知识库"
        supportingText="更新知识库名称与说明，不会影响现有文件"
        submitLabel="保存修改"
        pendingLabel="保存中…"
        initialName={editing.name}
        initialDescription={editing.description}
        onClose={() => setEditing(null)}
        onSubmit={async (value) => {
          if (assetApi) await assetApi.update(editing.knowledgeBaseId, value);
          else await userApi.updatePersonalKnowledge(editing.knowledgeBaseId, value);
          await load();
        }}
      />
    )}
    {canManageAssets && sharing && <PersonalKnowledgePermissionsDialog knowledgeBaseId={sharing.knowledgeBaseId} knowledgeBaseName={sharing.name} onClose={() => { setSharing(null); void load(); }}/>}
    {canManageAssets && deleting && <PersonalKnowledgeDeleteDialog key={deleting.knowledgeBaseId} knowledgeBaseName={deleting.name} onClose={() => setDeleting(null)} onDelete={() => remove(deleting)}/>}
  </section>;
}
