import { AntdModuleProviders } from "../../theme/antd-module";
import { ImChannelsPage } from "./ImChannelsPage";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace）的静态图不含 antd。
export function ImPanelView(props: {
  onChannelChanged?: () => void | Promise<void>;
}) {
  return (
    <AntdModuleProviders>
      <ImChannelsPage {...props} />
    </AntdModuleProviders>
  );
}
