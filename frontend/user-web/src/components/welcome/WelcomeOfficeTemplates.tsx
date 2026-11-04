import { ClipboardList, FileText, Presentation } from "lucide-react";
import type { LucideIcon } from "lucide-react";

// 空会话首页「推荐办公模板」：模块内置静态三卡（2026-11-02-002 §3.5，
// 同新建看板弹窗预设模板先例），点击预填对应提示词草稿（不自动发送）。

interface OfficeTemplate {
  id: string;
  name: string;
  description: string;
  prompt: string;
  tone: "blue" | "green" | "orange";
  icon: LucideIcon;
}

const officeTemplates: OfficeTemplate[] = [
  {
    id: "quarterly-report",
    name: "季度总结报告",
    description: "根据数据自动生成排版精美的季度总结",
    prompt: "请根据我提供的数据，生成一份排版精美的季度总结报告。",
    tone: "orange",
    icon: Presentation
  },
  {
    id: "weekly-report",
    name: "周报生成器",
    description: "输入本周工作内容，一键润色生成周报",
    prompt: "请根据我本周的工作内容，润色生成一份结构清晰的周报。",
    tone: "blue",
    icon: FileText
  },
  {
    id: "meeting-notes",
    name: "会议纪要提炼",
    description: "粘贴会议记录，自动提取核心结论与待办",
    prompt: "请从以下会议记录中提炼核心结论与待办事项。",
    tone: "green",
    icon: ClipboardList
  }
];

interface Props {
  onCommit: (prompt: string) => void;
}

export function WelcomeOfficeTemplates({ onCommit }: Props) {
  return (
    <section className="chatHomeBoardSection">
      <div className="chatHomeSectionHeader">
        <h3>推荐办公模板</h3>
      </div>
      <div className="chatHomeOfficeGrid">
        {officeTemplates.map((template) => (
          <button
            type="button"
            className="chatHomeOfficeCard"
            key={template.id}
            aria-label={`使用办公模板 ${template.name}`}
            onClick={() => onCommit(template.prompt)}
          >
            <span className={`chatHomeOfficeIcon chatHomeTone-${template.tone}`}>
              <template.icon size={20} aria-hidden="true" />
            </span>
            <span className="chatHomeOfficeInfo">
              <strong>{template.name}</strong>
              <small>{template.description}</small>
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
