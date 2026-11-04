import { useState } from "react";
import { FileText, Info, MessageSquare, PenLine, Presentation, Search, Zap } from "lucide-react";
import type { TaskPromptTab } from "@hermes-cloud/schemas";
import type { LucideIcon } from "lucide-react";

// 空会话首页引导卡：Info 提示（tab.description）+ 下划线 Tab（topics）+
// 快捷提示词 Tag 微卡片（tasks，单选）。数据全部来自 task-prompt-config；
// 图标为装饰（aria-hidden）：看板类闪电、办公类按 topic 序轮换。
// 注意：本组件在主入口静态图，只可用 lucide-react，禁止引入 antd/@ant-design/icons。

const officeTagIcons: LucideIcon[] = [Presentation, Search, PenLine, MessageSquare];

interface Props {
  tab: TaskPromptTab;
  iconKind: "board" | "office";
  onPreview: (prompt: string | null) => void;
  onCommit: (prompt: string) => void;
}

function tagIcon(iconKind: "board" | "office", topicIndex: number): LucideIcon {
  if (iconKind === "board") return Zap;
  return officeTagIcons[topicIndex % officeTagIcons.length] ?? FileText;
}

export function WelcomeGuideCard({ tab, iconKind, onPreview, onCommit }: Props) {
  const [activeTopicTitle, setActiveTopicTitle] = useState<string | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<string | null>(null);
  const activeTopic = tab.topics.find((topic) => topic.title === activeTopicTitle) ?? tab.topics[0];
  const activeTopicIndex = activeTopic ? tab.topics.indexOf(activeTopic) : 0;
  const Icon = tagIcon(iconKind, Math.max(activeTopicIndex, 0));
  const tipBlue = iconKind === "board";

  const handleTopicChange = (title: string) => {
    onPreview(null);
    setActiveTopicTitle(title);
    // 任务书红线：切 Tab 无缝切换 Tag 列表并重置选中状态。
    setSelectedSummary(null);
  };

  return (
    <section className="chatHomeGuideCard">
      <p className={tipBlue ? "chatHomeTip chatHomeTipBlue" : "chatHomeTip"}>
        <Info size={16} aria-hidden="true" />
        <span>{tab.description}</span>
      </p>
      <div className="chatHomeTopicTabs" role="tablist" aria-label={`${tab.label}主题`}>
        {tab.topics.map((topic) => (
          <button
            type="button"
            role="tab"
            key={topic.title}
            aria-selected={topic.title === activeTopic?.title}
            className={topic.title === activeTopic?.title ? "is-active" : undefined}
            onClick={() => handleTopicChange(topic.title)}
          >{topic.title}</button>
        ))}
      </div>
      <div className="chatHomeTagList" role="tabpanel" aria-label={`${activeTopic?.title ?? tab.label}典型问题`}>
        {(activeTopic?.tasks ?? []).map((task) => (
          <button
            type="button"
            key={task.summary}
            aria-label={`任务示例 ${task.summary}`}
            aria-pressed={selectedSummary === task.summary}
            className={selectedSummary === task.summary ? "chatHomeTag is-active" : "chatHomeTag"}
            onMouseEnter={() => onPreview(task.detail)}
            onMouseLeave={() => onPreview(null)}
            onFocus={() => onPreview(task.detail)}
            onBlur={() => onPreview(null)}
            onClick={() => {
              onPreview(null);
              setSelectedSummary(task.summary);
              onCommit(task.detail);
            }}
          >
            <Icon size={14} aria-hidden="true" />
            <span>{task.summary}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
