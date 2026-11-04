import { AntdModuleProviders } from "../../theme/antd-module";
import type { AppCenterListItem } from "../../api/client";
import { AppCenterPage } from "./AppCenterPage";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace）的静态图不含 antd。
export function AppCenterPanelView(props: {
  onEditApp: (app: AppCenterListItem) => void;
  onCreateAppSession: (prompt: string) => Promise<boolean>;
  onCreateFromTemplate: (app: AppCenterListItem) => Promise<boolean>;
}) {
  return (
    <AntdModuleProviders>
      <AppCenterPage {...props} />
    </AntdModuleProviders>
  );
}
