import type { AuthPrincipalSummary } from "@hermes-cloud/schemas";
import { AntdModuleProviders } from "../../theme/antd-module";
import type { UserProfile } from "../../profile";
import { ProfileDialog } from "./ProfileDialog";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace）的静态图不含 antd。
export function ProfileDialogView(props: {
  profile: UserProfile;
  principal: AuthPrincipalSummary | null;
  onSave: (profile: UserProfile) => void;
  onClose: () => void;
}) {
  return (
    <AntdModuleProviders>
      <ProfileDialog {...props} />
    </AntdModuleProviders>
  );
}
