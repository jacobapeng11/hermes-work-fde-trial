import { useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "antd";
import type {
  ProfileSkillCatalogItem,
  ProfileSkillConfiguredItem,
  SkillCategorySummary,
} from "@hermes-cloud/schemas";
import {
  usePlazaSkillStore,
  type MineSkillViewItem,
} from "../../state/plaza-skill-store";
import { SkillPageHeader } from "./SkillPageHeader";
import { SkillToolbar } from "./SkillToolbar";
import { SkillCategoryFilter } from "./SkillCategoryFilter";
import { SkillCardGrid } from "./SkillCardGrid";
import { SkillCard } from "./SkillCard";
import { SkillUploadDialog } from "./SkillUploadDialog";
import { SkillRemovalDialog } from "./SkillRemovalDialog";
import { SkillDetailDialog } from "./SkillDetailDialog";
import { useSkillPanelActions } from "./use-skill-panel-actions";
import { categoryTabs, fallbackCategories } from "./shared";
import type { RemovalConfirmation, SkillSelection, SkillWorkspaceController } from "./shared";
import "./skills.css";

export type { RemovalConfirmation, SkillSelection, SkillWorkspaceController };

export interface PlazaSkillPanelProps {
  runtimeWorkspaceId?: number | null;
  controller?: SkillWorkspaceController;
  allowPersonalUpload?: boolean;
  allowUpload?: boolean;
  installTargetLabel?: string;
  uploadDialogLabel?: string;
  uploadTitle?: string;
  uploadDescription?: string;
  /** 无框嵌入模式（2026-11-03-016）：不渲染灰底页/白色工作卡/模块页头，
   * 内容直接嵌入外层大卡片（专家-技能组合页）。默认 false 保持独立模块视觉。 */
  chromeless?: boolean;
  /** 上传信号（递增即打开上传弹窗）：外层大卡片页头的「上传」按钮经此触发，
   * 语义同知识库 KnowledgePage 的 createTicket。 */
  uploadTrigger?: number;
}

type SkillTab = "mine" | "market";

export function PlazaSkillPanel({
  runtimeWorkspaceId,
  controller,
  allowPersonalUpload = true,
  allowUpload,
  installTargetLabel = "当前 Hermes",
  uploadDialogLabel = "上传个人技能",
  uploadTitle = "上传个人技能",
  uploadDescription = "上传包含 SKILL.md 的 zip 包，SKILL.md 将由后端校验。",
  chromeless = false,
  uploadTrigger,
}: PlazaSkillPanelProps = {}) {
  const personalStore = usePlazaSkillStore();
  const store: SkillWorkspaceController = controller ?? personalStore;
  const personalMode = !controller;
  const uploadHandler = store.submitUpload ?? store.submitPersonalUpload;
  const uploadAllowed = (allowUpload ?? allowPersonalUpload) && Boolean(uploadHandler);
  const uploadLabel = personalMode ? "上传个人技能" : "上传技能配置";

  const [tab, setTab] = useState<SkillTab>("mine");
  const [searchQuery, setSearchQuery] = useState("");
  const [category, setCategory] = useState("all");

  const actions = useSkillPanelActions({
    store,
    uploadHandler,
    onUploadSuccess: () => setTab("mine"),
  });
  const {
    selectedSkill, removal, removing, uploadOpen, createdCategories,
    actionError, operationNotice, setUploadOpen, setSelectedSkill, setRemoval,
    runInstall, confirmRemoval, handleUploadSubmit, handleCreateCategory,
    handleSaveDetails, canEditSelection,
  } = actions;

  // 外层大卡片页头的「上传」信号（2026-11-03-016）：递增即打开上传弹窗。
  // ref 比较保证幂等，重复渲染不误开；初始挂载（0 → 0）不触发。
  const uploadSignalRef = useRef(uploadTrigger ?? 0);
  useEffect(() => {
    const signal = uploadTrigger ?? 0;
    if (signal > uploadSignalRef.current) setUploadOpen(true);
    uploadSignalRef.current = signal;
  }, [uploadTrigger, setUploadOpen]);

  useEffect(() => {
    if (controller || runtimeWorkspaceId) {
      void store.load(runtimeWorkspaceId ?? undefined).catch(() => undefined);
    }
    // The store is process-local. Loading once per mounted plaza avoids a
    // refresh loop because bound controller methods can change on each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtimeWorkspaceId]);

  const categories = useMemo(() => {
    const base = store.categories.length > 0 ? store.categories : fallbackCategories;
    const byKey = new Map(base.map((item) => [item.categoryKey, item]));
    createdCategories.forEach((item) => byKey.set(item.categoryKey, item));
    return [...byKey.values()];
  }, [createdCategories, store.categories]);
  const mineItems = personalMode ? store.personalItems : store.installedItems;
  const sourceItems = tab === "mine" ? mineItems : store.marketItems;
  const tabs = useMemo(() => categoryTabs(sourceItems, categories), [categories, sourceItems]);
  const normalizedQuery = searchQuery.trim().toLocaleLowerCase();
  const visibleItems = sourceItems.filter((item) => {
    const categoryMatch = category === "all" || item.categoryKey === category;
    const queryMatch = !normalizedQuery
      || item.name.toLocaleLowerCase().includes(normalizedQuery)
      || (item.description || "").toLocaleLowerCase().includes(normalizedQuery);
    return categoryMatch && queryMatch;
  });
  const orderedVisibleItems = useMemo(() => {
    if (tab !== "mine" || category !== "all") return visibleItems;
    return [...visibleItems].sort((left, right) => Number("source" in left && left.source === "system") - Number("source" in right && right.source === "system"));
  }, [category, tab, visibleItems]);
  const pendingByTarget = useMemo(
    () => new Map(store.pendingOperations.map((operation) => [operation.targetKey, operation])),
    [store.pendingOperations],
  );

  useEffect(() => {
    if (!tabs.some((item) => item.key === category)) setCategory("all");
  }, [category, tabs]);

  const marketOperation = (item: ProfileSkillCatalogItem) => pendingByTarget.get(`asset:${item.assetId}`);
  const configuredTarget = (item: ProfileSkillConfiguredItem) =>
    item.catalogAssetId ? `asset:${item.catalogAssetId}` : `skill:${item.assetId}`;
  const configuredOperation = (item: ProfileSkillConfiguredItem) => pendingByTarget.get(configuredTarget(item));

  const panelContent = (
    <>
      {!chromeless && (
        <SkillPageHeader
          title="技能广场"
          subtitle="管理个人技能并从专家市场安装公开能力"
          uploadAllowed={uploadAllowed}
          uploadLabel={uploadLabel}
          onUploadOpen={() => setUploadOpen(true)}
        />
      )}
      <SkillToolbar
        tab={tab}
        onTabChange={(next) => { setTab(next); setCategory("all"); }}
        mineCount={mineItems.length}
        marketCount={store.marketItems.length}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
      />

      <SkillCategoryFilter tabs={tabs} category={category} onCategoryChange={setCategory} />

      {operationNotice && <Alert className="skillCatalogNoticeAlert" type="success" showIcon message={operationNotice} />}
      {(actionError || store.error) && <Alert className="skillCatalogErrorAlert" type="error" showIcon message={actionError || store.error} />}
      {store.pendingOperations.filter((operation) => operation.status === "failed").map((operation) => (
        <Alert className="skillCatalogErrorAlert" type="error" showIcon key={operation.operationId} message={operation.errorMessage || "技能操作失败，请重试。"} />
      ))}

      <SkillCardGrid
        showSkeleton={store.loading && sourceItems.length === 0}
        showEmpty={!store.loading && orderedVisibleItems.length === 0}
        emptyText={normalizedQuery ? "没有找到符合条件的技能。" : tab === "mine" ? "暂无个人技能，可以先上传一个技能。" : "暂无可用的市场技能。"}
        emptyAction={tab === "mine" && !normalizedQuery && uploadAllowed ? { label: uploadLabel, onClick: () => setUploadOpen(true) } : null}
      >
        {tab === "market"
          ? (orderedVisibleItems as ProfileSkillCatalogItem[]).map((item) => (
              <SkillCard
                key={item.assetId}
                kind="market"
                categories={categories}
                item={item}
                operation={marketOperation(item)}
                onOpen={setSelectedSkill}
                onRun={runInstall}
                onRemovalRequest={setRemoval}
              />
            ))
          : (orderedVisibleItems as Array<MineSkillViewItem | ProfileSkillConfiguredItem>).map((item) => (
              <SkillCard
                key={`${(item as MineSkillViewItem).source}:${item.assetId}`}
                kind="mine"
                categories={categories}
                item={item}
                operation={configuredOperation(item as ProfileSkillConfiguredItem)}
                onOpen={setSelectedSkill}
                onRun={runInstall}
                onRemovalRequest={setRemoval}
              />
            ))}
      </SkillCardGrid>

      <SkillUploadDialog
        open={uploadOpen}
        title={uploadTitle}
        description={uploadDescription}
        dialogLabel={uploadDialogLabel}
        categories={categories}
        allowCreateCategory={personalMode && Boolean(store.createPersonalCategory)}
        onSubmit={handleUploadSubmit}
        onCreateCategory={handleCreateCategory}
        onClose={() => setUploadOpen(false)}
      />

      <SkillRemovalDialog
        removal={removal}
        removing={removing}
        error={actionError}
        onCancel={() => setRemoval(null)}
        onConfirm={confirmRemoval}
      />

      <SkillDetailDialog
        selection={selectedSkill}
        hidden={removal !== null}
        categories={categories}
        installTargetLabel={installTargetLabel}
        canEdit={canEditSelection}
        onClose={() => setSelectedSkill(null)}
        onInstall={runInstall}
        onRemovalRequest={setRemoval}
        onSaveDetails={handleSaveDetails}
      />
    </>
  );

  // chromeless：内容直接嵌入外层大卡片（专家-技能组合页），不再自带灰底页与工作卡。
  return chromeless ? panelContent : (
    <section className="skillCatalogPage" aria-label="技能广场">
      <div className="skillWorkspace">{panelContent}</div>
    </section>
  );
}
