import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";

// antd 5 的 DatePicker/TimePicker 面板文案由 dayjs 渲染，zhCN 只管 antd 组件层；
// 需同时把 dayjs 切到中文，面板中的月份/星期才不会显示英文。
dayjs.locale("zh-cn");
import type { ReactNode } from "react";
import { usePlatformTheme } from "./platform-theme";
import { antdThemeFor } from "./antd-theme";

// 设计 §6.6 备选方案：根级 ConfigProvider 实测使 entry gzip 增加 84KB（>30KB 门禁），
// 因此 antd Provider 下沉到各懒加载模块边界内嵌套（偏离 Skill「单一根」约定，理由记录在设计文档）。
// 后续每期模块在自己的懒加载入口用本组件包裹，antd 代码不进入聊天首屏 chunk。
export function AntdModuleProviders({ children }: { children: ReactNode }) {
  const { theme } = usePlatformTheme();
  return (
    <ConfigProvider theme={antdThemeFor(theme)} locale={zhCN}>
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  );
}
