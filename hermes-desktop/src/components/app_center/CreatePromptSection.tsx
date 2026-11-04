import { ThunderboltOutlined } from "@ant-design/icons";

// 新建看板弹窗「创建方式一」：分段控制器 Tab + 快捷提示词 Tag（单选）。
// 数据为弹窗内置字典（2026-11-02-001 需求给定），不再读取 task-prompt-config；
// 主页 WelcomeTaskPanel 的配置驱动引导不受影响。

export type CreatePromptTabId = "ai-lead" | "opportunity" | "shipment" | "ticket";

interface TabOption {
  id: CreatePromptTabId;
  label: string;
}

const tabOptions: TabOption[] = [
  { id: "ai-lead", label: "AI线索平台" },
  { id: "opportunity", label: "商机统计" },
  { id: "shipment", label: "发货统计" },
  { id: "ticket", label: "工单统计" }
];

const tagData: Record<CreatePromptTabId, string[]> = {
  "ai-lead": ["制造业线索按地市统计", "AI线索处理情况统计", "超融合线索分类统计", "各渠道线索转化率", "线索跟进人员排行榜"],
  opportunity: ["商机阶段漏斗分析", "本月新增商机统计", "大客户商机赢单率", "商机预计成交金额"],
  shipment: ["近半年发货趋势", "各省份发货量统计", "超期未发货订单看板", "发货异常件数分析"],
  ticket: ["工单平均响应时长", "客户满意度评分分布", "各产品线工单量", "工单积压数量排行"]
};

interface Props {
  activeTabId: CreatePromptTabId;
  selectedPrompt: string | null;
  disabled: boolean;
  onTabChange: (tabId: CreatePromptTabId) => void;
  onSelectPrompt: (prompt: string) => void;
}

export function CreatePromptSection({
  activeTabId,
  selectedPrompt,
  disabled,
  onTabChange,
  onSelectPrompt
}: Props) {
  const tags = tagData[activeTabId];
  return (
    <section className="appCenterCreateCard">
      <h3 className="appCenterCreateCardTitle">创建方式一：通过提示词新建</h3>
      <div className="appCenterCreateSegmented" role="tablist" aria-label="提示词主题">
        {tabOptions.map((tab) => (
          <button
            type="button"
            role="tab"
            key={tab.id}
            aria-selected={tab.id === activeTabId}
            className={tab.id === activeTabId ? "appCenterCreateSegItem is-active" : "appCenterCreateSegItem"}
            disabled={disabled}
            onClick={() => onTabChange(tab.id)}
          >{tab.label}</button>
        ))}
      </div>
      <div className="appCenterCreateTagList" role="tabpanel" aria-label={`${tabOptions.find((tab) => tab.id === activeTabId)?.label ?? ""}快捷提示词`}>
        {tags.map((tag) => (
          <button
            type="button"
            key={tag}
            aria-pressed={selectedPrompt === tag}
            className={selectedPrompt === tag ? "appCenterCreateTag is-active" : "appCenterCreateTag"}
            disabled={disabled}
            onClick={() => onSelectPrompt(tag)}
          >
            <ThunderboltOutlined className="appCenterCreateTagIcon" aria-hidden />
            <span>{tag}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
