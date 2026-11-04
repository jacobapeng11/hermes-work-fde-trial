import { useState } from "react";
import type { SkillCategorySummary } from "@hermes-cloud/schemas";
import type { PersonalSkillRemovalResult } from "../../state/plaza-skill-store";
import type { RemovalConfirmation, SkillSelection, SkillWorkspaceController } from "./shared";
import { errorMessage } from "./shared";

interface UseSkillPanelActionsOptions {
  store: SkillWorkspaceController;
  uploadHandler: ((file: File, categoryKey: string, description?: string) => Promise<unknown>) | undefined;
  // 上传提交成功后的视图侧动作（关闭弹窗由本 hook 处理，这里只回调切回「我的」）
  onUploadSuccess: () => void;
}

// 技能面板跨区状态与写操作（Task 5 从组装根抽出；行为与迁移前逐项一致）。
export function useSkillPanelActions({ store, uploadHandler, onUploadSuccess }: UseSkillPanelActionsOptions) {
  const [selectedSkill, setSelectedSkill] = useState<SkillSelection | null>(null);
  const [removal, setRemoval] = useState<RemovalConfirmation | null>(null);
  const [removing, setRemoving] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [createdCategories, setCreatedCategories] = useState<SkillCategorySummary[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);

  const handleUploadSubmit = async (file: File, categoryKey: string, description?: string) => {
    if (!uploadHandler) return;
    if (description !== undefined) {
      await uploadHandler(file, categoryKey, description);
    } else {
      await uploadHandler(file, categoryKey);
    }
    setOperationNotice(`“${file.name.replace(/\.zip$/i, "")}”已提交上传，完成后列表会自动刷新。`);
    setUploadOpen(false);
    onUploadSuccess();
  };

  const handleCreateCategory = async (displayName: string) => {
    if (!store.createPersonalCategory) throw new Error("当前页面不支持创建个人分类");
    const created = await store.createPersonalCategory(displayName);
    setCreatedCategories((current) => [...current, created]);
    return created;
  };

  const handleSaveDetails = async (skillId: number, name: string, description: string): Promise<string | null> => {
    if (!selectedSkill || selectedSkill.kind !== "mine" || !store.submitDescriptionUpdate) {
      return "技能信息保存失败，请重试。";
    }
    const item = selectedSkill.item;
    const normalizedName = name.trim().toLocaleLowerCase().replace(/[\s_]+/g, "-");
    const reservedSystemNames = new Set(
      store.installedItems
        .filter((installed) => installed.source === "system")
        .flatMap((installed) => [installed.name])
        .map((value) => value.trim().toLocaleLowerCase().replace(/[\s_]+/g, "-")),
    );
    if (reservedSystemNames.has(normalizedName)) {
      return "该名称属于系统保留技能名称，请换一个名称。";
    }
    try {
      const result = await store.submitDescriptionUpdate(skillId, description, name);
      const returnedDescription = (
        result
        && typeof result === "object"
        && "description" in result
        && (typeof result.description === "string" || result.description === null)
      ) ? result.description : description.trim() || null;
      const returnedName = (
        result
        && typeof result === "object"
        && "name" in result
        && typeof result.name === "string"
      ) ? result.name : name;
      setSelectedSkill({
        kind: "mine",
        item: { ...item, name: returnedName, description: returnedDescription },
      });
      setOperationNotice("技能信息已保存，清空描述会恢复技能包默认说明。");
      return null;
    } catch (error: unknown) {
      return errorMessage(error, "技能信息保存失败，请重试。");
    }
  };

  const runInstall = async (selection: SkillSelection) => {
    setActionError(null);
    try {
      if (selection.kind === "market") {
        await store.submitInstall(selection.item.assetId);
      } else if (selection.item.source === "personal" && "desired" in selection.item) {
        await store.submitPersonalInstall?.(selection.item.assetId);
      }
      setSelectedSkill(null);
    } catch (error: unknown) {
        setActionError(errorMessage(error, "技能安装提交失败，请重试。"));
    }
  };

  const confirmRemoval = async () => {
    if (!removal || removing) return;
    setRemoving(true);
    setActionError(null);
    try {
      if (removal.kind === "personal_delete") {
        if (!store.submitPersonalDelete) throw new Error("当前页面不支持删除个人技能");
        const result = await store.submitPersonalDelete(removal.item.assetId) as PersonalSkillRemovalResult | undefined;
        const affected = result?.affectedProfileIds?.length ?? 0;
        setOperationNotice(affected > 0
          ? `个人技能已提交删除，将从 ${affected} 个 Profile 自动移除。`
          : "个人技能已删除。当前没有绑定该能力的 Profile。");
      } else if (removal.kind === "personal_uninstall") {
        if (!store.submitPersonalUninstall) throw new Error("当前页面不支持卸载个人技能");
        await store.submitPersonalUninstall(removal.item.assetId);
        setOperationNotice(`“${removal.item.name}”已从默认 Profile 提交卸载，个人技能资产仍保留。`);
      } else if (removal.kind === "market") {
        await store.submitUninstall(
          removal.item.configuredAssetId ?? removal.item.assetId,
          removal.item.assetId,
          `asset:${removal.item.assetId}`,
        );
        setOperationNotice(`“${removal.item.name}”已提交卸载，仍可在专家市场重新安装。`);
      } else if (removal.kind === "application_delete") {
        await store.submitUninstall(
          removal.item.assetId,
          removal.item.catalogAssetId,
        );
        setOperationNotice(`“${removal.item.name}”已删除。`);
      } else {
        const target = removal.item.catalogAssetId ? `asset:${removal.item.catalogAssetId}` : `skill:${removal.item.assetId}`;
        await store.submitUninstall(removal.item.assetId, removal.item.catalogAssetId, target);
        setOperationNotice(`“${removal.item.name}”已提交卸载。`);
      }
      setRemoval(null);
      setSelectedSkill(null);
    } catch (error: unknown) {
      setActionError(errorMessage(
        error,
        removal.kind === "personal_delete" || removal.kind === "application_delete"
          ? "技能删除提交失败，请重试。"
          : "技能卸载提交失败，请重试。",
      ));
    } finally {
      setRemoving(false);
    }
  };

  const canEditSelection = selectedSkill !== null
    && selectedSkill.kind === "mine"
    && (selectedSkill.item.source === "personal" || selectedSkill.item.source === "application")
    && Boolean(store.submitDescriptionUpdate);

  return {
    selectedSkill,
    removal,
    removing,
    uploadOpen,
    createdCategories,
    actionError,
    operationNotice,
    setUploadOpen,
    setSelectedSkill,
    setRemoval,
    runInstall,
    confirmRemoval,
    handleUploadSubmit,
    handleCreateCategory,
    handleSaveDetails,
    canEditSelection,
  };
}
