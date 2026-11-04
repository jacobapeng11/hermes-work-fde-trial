import { AntdModuleProviders } from "../../theme/antd-module";
import type { RuntimeAgentProfileSummary } from "@hermes-cloud/schemas";
import { ExpertPage } from "./ExpertPage";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace）的静态图不含 antd。
export function ExpertPanelView(props: {
  runtimeWorkspaceId: number | null;
  onInstalledChange?: (profiles: RuntimeAgentProfileSummary[]) => Promise<void> | void;
  onStartChat: (profile: RuntimeAgentProfileSummary) => void;
  /** 透传 ExpertPage 嵌入模式（2026-11-03-016，专家-技能组合页使用）。 */
  embedded?: boolean;
  /** 透传创建信号（2026-11-03-017，组合页页头「创建专家」按钮驱动）。 */
  createTrigger?: number;
}) {
  return (
    <AntdModuleProviders>
      <ExpertPage {...props} />
    </AntdModuleProviders>
  );
}
