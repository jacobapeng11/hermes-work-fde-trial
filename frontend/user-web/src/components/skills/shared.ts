import type {
  ProfileSkillCatalogItem,
  ProfileSkillConfiguredItem,
  SkillCategorySummary,
} from "@hermes-cloud/schemas";
import type {
  PersonalSkillViewItem,
  PlazaSkillStoreState,
} from "../../state/plaza-skill-store";

// 技能模块共享类型与渲染辅助（Task 5 收敛至此）。
// 颜色语义不在此层表达；组件视觉走 antd Token 或模块 CSS 变量。

export interface SkillWorkspaceController extends PlazaSkillStoreState {
  load: (runtimeWorkspaceId?: number) => Promise<void>;
  submitInstall: (assetId: number) => Promise<unknown>;
  submitUninstall: (skillId: number, catalogAssetId?: number | null, targetKey?: string) => Promise<unknown>;
  submitPersonalUpload?: (file: File, categoryKey: string, description?: string) => Promise<unknown>;
  submitPersonalInstall?: (skillId: number) => Promise<unknown>;
  submitPersonalUninstall?: (skillId: number) => Promise<unknown>;
  submitPersonalDelete?: (skillId: number) => Promise<unknown>;
  createPersonalCategory?: (displayName: string) => Promise<SkillCategorySummary>;
  submitUpload?: (file: File, categoryKey: string, description?: string) => Promise<unknown>;
  submitDescriptionUpdate?: (
    skillId: number,
    description: string | null,
    name?: string,
  ) => Promise<unknown>;
}

export type SkillSelection =
  | { kind: "market"; item: ProfileSkillCatalogItem }
  | { kind: "mine"; item: ProfileSkillConfiguredItem };

export type RemovalConfirmation =
  | { kind: "personal_uninstall"; item: PersonalSkillViewItem }
  | { kind: "personal_delete"; item: PersonalSkillViewItem }
  | { kind: "market"; item: ProfileSkillCatalogItem }
  | { kind: "application_delete"; item: ProfileSkillConfiguredItem }
  | { kind: "configured"; item: ProfileSkillConfiguredItem };

export function activeOperation(operation: { status: string } | undefined): boolean {
  return operation?.status === "queued" || operation?.status === "running";
}

export function operationLabel(operation: { action: string; status?: string } | undefined, fallback: string): string {
  if (!operation) return fallback;
  if (operation.status === "failed") return "重试";
  if (operation.action === "personal_upload") return "上传中";
  if (operation.action === "delete") return "删除中";
  if (operation.action === "uninstall") return "卸载中";
  return "安装中";
}

export function versionLabel(version: string): string {
  const raw = String(version || "1").replace(/^v/i, "");
  return `v${raw.split(".")[0] || "1"}`;
}

export function categoryLabel(categoryKey: string, categories: SkillCategorySummary[]): string {
  return categories.find((category) => category.categoryKey === categoryKey)?.displayName ?? categoryKey;
}

export function categoryTabs(items: Array<{ categoryKey: string }>, categories: SkillCategorySummary[]) {
  const counts = new Map<string, number>();
  items.forEach((item) => counts.set(item.categoryKey, (counts.get(item.categoryKey) ?? 0) + 1));
  return [
    { key: "all", label: "全部", count: items.length },
    ...[...counts.entries()].map(([key, count]) => ({ key, label: categoryLabel(key, categories), count })),
  ];
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export const fallbackCategories: SkillCategorySummary[] = [
  { categoryKey: "hermes-core", displayName: "Hermes 核心能力", isBuiltin: true },
  { categoryKey: "browser", displayName: "网络与浏览器", isBuiltin: true },
  { categoryKey: "productivity", displayName: "中文办公", isBuiltin: true },
  { categoryKey: "software-development", displayName: "软件开发", isBuiltin: true },
  { categoryKey: "data-science", displayName: "数据科学", isBuiltin: true },
  { categoryKey: "research", displayName: "研究", isBuiltin: true },
  { categoryKey: "creative", displayName: "创作", isBuiltin: true },
  { categoryKey: "other", displayName: "其他", isBuiltin: true },
];
