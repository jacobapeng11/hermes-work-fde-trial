import type { ProfileMcpAssetDetail, ProfileMcpAssetItem } from "@hermes-cloud/schemas";
import { AntdModuleProviders } from "../../theme/antd-module";
import { McpWorkspace, UserPlazaMcpWorkspace } from "./McpWorkspace";
import type { McpWorkspaceAction } from "./McpWorkspace";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace / ApplicationAgentConfigurationPanel）的静态图不含 antd。

/** 个人工作区 MCP 视图：UserPlazaMcpWorkspace 是唯一轮询宿主。 */
export function McpPanelView(props: { runtimeWorkspaceId: number | null }) {
  return (
    <AntdModuleProviders>
      <UserPlazaMcpWorkspace {...props} />
    </AntdModuleProviders>
  );
}

/** 应用 Agent 配置面板宿主：透传外部 controller，Provider 包裹同上。 */
export function McpWorkspaceView(props: {
  items: ProfileMcpAssetItem[];
  busyKey?: string | null;
  activeOperations?: Readonly<Record<number, McpWorkspaceAction>>;
  statusMessage?: string | null;
  error?: string | null;
  onAction: (
    asset: ProfileMcpAssetItem,
    action: McpWorkspaceAction
  ) => Promise<unknown>;
  loadDetail: (
    asset: ProfileMcpAssetItem
  ) => Promise<ProfileMcpAssetDetail>;
}) {
  return (
    <AntdModuleProviders>
      <McpWorkspace {...props} />
    </AntdModuleProviders>
  );
}
