import { Button, Empty, Skeleton } from "antd";
import type { ReactNode } from "react";

export interface SkillCardGridProps {
  showSkeleton: boolean;
  showEmpty: boolean;
  emptyText: string | null;
  // 空态引导主操作（仅「我的」空且可上传时提供，设计 §4.5「提供邀请」）
  emptyAction: { label: string; onClick: () => void } | null;
  children: ReactNode;
}

const skeletonStyle = { height: 214, borderRadius: 8 } as const;

export function SkillCardGrid({ showSkeleton, showEmpty, emptyText, emptyAction, children }: SkillCardGridProps) {
  if (showSkeleton) {
    return (
      <div className="skillCatalogGrid" aria-label="正在加载技能">
        <Skeleton.Button active block style={skeletonStyle} />
        <Skeleton.Button active block style={skeletonStyle} />
        <Skeleton.Button active block style={skeletonStyle} />
      </div>
    );
  }
  return (
    <>
      <div className="skillCatalogGrid">{children}</div>
      {showEmpty && (
        <div className="skillCatalogEmptyWrap">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />
          {emptyAction && (
            <Button type="primary" autoInsertSpace={false} onClick={emptyAction.onClick}>{emptyAction.label}</Button>
          )}
        </div>
      )}
    </>
  );
}
