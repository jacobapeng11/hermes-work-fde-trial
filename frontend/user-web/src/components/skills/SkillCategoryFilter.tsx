import { theme } from "antd";

export interface SkillCategoryFilterProps {
  tabs: Array<{ key: string; label: string; count: number }>;
  category: string;
  onCategoryChange: (key: string) => void;
}

// 分类 pill 无 antd 等价组件（Segmented 无计数语义），保留自定义按钮，
// 配色经 useToken 消费 antd Token（选中态：主色 + 主色浅底，见设计文档 §7）。
export function SkillCategoryFilter({ tabs, category, onCategoryChange }: SkillCategoryFilterProps) {
  const { token } = theme.useToken();
  return (
    <div className="skillCatalogCategories" role="group" aria-label="技能分类">
      {tabs.map((item) => {
        const active = category === item.key;
        return (
          <button
            key={item.key}
            type="button"
            aria-pressed={active}
            onClick={() => onCategoryChange(item.key)}
            style={{
              height: 32,
              padding: "0 15px",
              borderRadius: 4,
              cursor: "pointer",
              fontSize: token.fontSize,
              lineHeight: 1,
              border: `1px solid ${active ? token.colorPrimary : "var(--skill-pill-border, #dcdfe6)"}`,
              color: active ? "#fff" : token.colorTextSecondary,
              background: active ? token.colorPrimary : "var(--skill-pill-bg, #fff)",
              fontWeight: active ? 500 : 400,
              transition: "all 0.2s",
            }}
          >
            {item.label}<span style={{ marginLeft: 4, fontSize: token.fontSizeSM, opacity: 0.65 }}>{item.count}</span>
          </button>
        );
      })}
    </div>
  );
}
