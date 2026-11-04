import type { DefaultProfileAssetView } from "@hermes-cloud/schemas";
import { userApi } from "./client";

export const plazaApi = {
  getDefaultView: (runtimeWorkspaceId: number, signal?: AbortSignal): Promise<DefaultProfileAssetView> =>
    userApi.defaultProfileAssets(runtimeWorkspaceId, signal),
  install: (runtimeWorkspaceId: number, assetId: number) =>
    userApi.installDefaultProfileAsset(runtimeWorkspaceId, "skill", assetId),
  uninstall: (runtimeWorkspaceId: number, skillId: number) =>
    userApi.uninstallDefaultProfileAsset(runtimeWorkspaceId, "skill", skillId),
};
