import { type ReactNode } from "react";
import { Card, Tag } from "antd";

export interface CapabilitySelectedItem {
  key: string;
  name: string;
}

interface Props {
  title: string;
  desc: string;
  icon: ReactNode;
  actionTitle: string;
  actionDesc: string;
  selectedTitle: string;
  selectedItems: CapabilitySelectedItem[];
  disabled?: boolean;
  onAddClick: () => void;
  onRemoveItem: (key: string) => void;
  onClear: () => void;
  secondaryAction?: ReactNode;
}

// 能力选择卡（按用户参考定版）：顶部双列虚线入口（市场选择 + 上传，与已选区
// 彻底分离）；已选区 #fafafa 底 + 「已选择X (N) / 清空」头行 + antd Tag
// closable 浅蓝标签流（closeIcon 带 role/aria-label 保留移除确认链路契约）。
export function CapabilitySelectCard({
  title,
  desc,
  icon,
  actionTitle,
  actionDesc,
  selectedTitle,
  selectedItems,
  disabled = false,
  onAddClick,
  onRemoveItem,
  onClear,
  secondaryAction
}: Props) {
  return (
    <Card className="expert-block-card" title={<h3 className="expert-block-title">{title}</h3>} bordered={false}>
      <p className="expert-block-desc">{desc}</p>
      <div className="expert-dashed-row">
        <div
          className={`expert-dashed-box${disabled ? " is-disabled" : ""}`}
          role="button"
          tabIndex={disabled ? -1 : 0}
          aria-label={actionTitle}
          onClick={() => !disabled && onAddClick()}
          onKeyDown={(event) => {
            if (!disabled && (event.key === "Enter" || event.key === " ")) {
              event.preventDefault();
              onAddClick();
            }
          }}
        >
          <span className="expert-dashed-icon">{icon}</span>
          <span className="expert-dashed-text">
            <span className="expert-dashed-title">{actionTitle}</span>
            <span className="expert-dashed-desc">{actionDesc}</span>
          </span>
        </div>
        {secondaryAction}
      </div>
      <div className="expert-selected-box">
        <div className="expert-selected-head">
          <p className="expert-selected-title">
            {selectedTitle} <span className="expert-selected-count">({selectedItems.length})</span>
          </p>
          {selectedItems.length > 0 && (
            <a className="expert-selected-clear" role="button" tabIndex={0} onClick={onClear} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onClear(); }}>清空</a>
          )}
        </div>
        {selectedItems.length === 0
          ? <p className="expert-selected-empty">尚未选择，可稍后继续配置。</p>
          : <div className="expert-selected-tags">
              {selectedItems.map((item) => (
                <Tag
                  key={item.key}
                  closable
                  color="blue"
                  className="expert-selected-tag"
                  onClose={(event) => { event.preventDefault(); onRemoveItem(item.key); }}
                  closeIcon={<span role="button" aria-label={`移除 ${item.name}`} className="expert-tag-close" aria-hidden={false}>×</span>}
                >{item.name}</Tag>
              ))}
            </div>}
      </div>
    </Card>
  );
}
