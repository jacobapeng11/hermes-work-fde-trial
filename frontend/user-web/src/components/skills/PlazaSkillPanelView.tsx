import { AntdModuleProviders } from "../../theme/antd-module";
import { PlazaSkillPanel } from "./PlazaSkillPanel";
import type { PlazaSkillPanelProps } from "./PlazaSkillPanel";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace / ApplicationAgentConfigurationPanel）的静态图不含 antd。
export function PlazaSkillPanelView(props: PlazaSkillPanelProps) {
  return (
    <AntdModuleProviders>
      <PlazaSkillPanel {...props} />
    </AntdModuleProviders>
  );
}
