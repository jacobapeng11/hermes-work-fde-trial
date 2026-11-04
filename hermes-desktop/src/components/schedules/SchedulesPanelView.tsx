import { AntdModuleProviders } from "../../theme/antd-module";
import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";
import { SchedulesPage } from "./SchedulesPage";

// 模块级 antd Provider 包裹：antd 代码全部留在本懒加载 chunk 内，
// 挂载点（AgentWorkspace）的静态图不含 antd。
export function SchedulesPanelView(props: {
  runtimeWorkspaceId: number | null;
  sessions: RuntimeSessionSummary[];
  onOpenSession: (runtimeSessionId: string, taskId?: number, acceptedAt?: string, taskName?: string, prompt?: string) => void;
  onRetryWorkspace?: () => void;
}) {
  return (
    <AntdModuleProviders>
      <SchedulesPage {...props} />
    </AntdModuleProviders>
  );
}
