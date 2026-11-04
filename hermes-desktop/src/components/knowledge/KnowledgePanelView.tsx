import { AntdModuleProviders } from "../../theme/antd-module";
import { KnowledgePage } from "./KnowledgePage";
import type { KnowledgeWorkspaceAdapter } from "./KnowledgePage";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace / ApplicationAgentConfigurationPanel）的静态图不含 antd。
export function KnowledgePanelView(props: {
  runtimeWorkspaceId: number | null;
  adapter?: KnowledgeWorkspaceAdapter;
}) {
  return (
    <AntdModuleProviders>
      <KnowledgePage {...props} />
    </AntdModuleProviders>
  );
}
