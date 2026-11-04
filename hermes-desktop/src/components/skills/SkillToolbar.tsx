import { Input, Tabs } from "antd";
import { SearchOutlined } from "@ant-design/icons";

export interface SkillToolbarProps {
  tab: "mine" | "market";
  onTabChange: (tab: "mine" | "market") => void;
  mineCount: number;
  marketCount: number;
  searchQuery: string;
  onSearchChange: (value: string) => void;
}

// 主操作（上传）已上移页头（2026-10-24 视觉优化）；工具栏仅承载 Tabs 与搜索。
// 二级 Tab 样式（2026-11-03-017）：挂共用类 secondaryLaneTabs（styles.css），
// 计数为轻量角标 pill（laneTabCount，未选灰/选中浅蓝），常显含 0。
function TabCount({ count }: { count: number }) {
  return <span className="laneTabCount">{count}</span>;
}

export function SkillToolbar({
  tab,
  onTabChange,
  mineCount,
  marketCount,
  searchQuery,
  onSearchChange,
}: SkillToolbarProps) {
  return (
    <div className="skillCatalogToolbar">
      <div className="skillCatalogTabsWrap">
        <Tabs
          className="secondaryLaneTabs"
          activeKey={tab}
          onChange={(key) => onTabChange(key as "mine" | "market")}
          items={[
            { key: "mine", label: <>我的 <TabCount count={mineCount} /></> },
            { key: "market", label: <>专家市场 <TabCount count={marketCount} /></> },
          ]}
        />
      </div>
      <div className="skillCatalogToolbarActions">
        <Input
          className="skillCatalogSearch"
          prefix={<SearchOutlined aria-hidden />}
          value={searchQuery}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="搜索技能"
          aria-label="搜索技能"
        />
      </div>
    </div>
  );
}
